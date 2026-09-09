"""Command-line interface.

    python -m bankstmt statements/*.pdf --db bank.db --report report.md

Design notes:

* Every run is idempotent — importing the same PDF twice inserts nothing the
  second time, so you can safely re-run over a whole folder.
* ``--dry-run`` parses and reports without touching the database, which is the
  right first move on a statement layout you have not tried before.
* ``--csv`` dumps what was parsed so you can eyeball it against the PDF.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import os
import sys
from decimal import Decimal

from . import __version__
from .amounts import to_float
from .db import connect, insert_transactions, record_run
from .extract import extract_pages
from .parser import Issue, StatementParser, Transaction
from .report import build_markdown, write_issue_csv, write_markdown


def sha1_of(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def expand_inputs(patterns: list[str]) -> list[str]:
    """Accept files, globs and directories; return a sorted list of PDFs."""
    out: list[str] = []
    for p in patterns:
        if os.path.isdir(p):
            out.extend(sorted(glob.glob(os.path.join(p, "**", "*.pdf"), recursive=True)))
        elif any(ch in p for ch in "*?["):
            out.extend(sorted(glob.glob(p, recursive=True)))
        else:
            out.append(p)
    seen, uniq = set(), []
    for p in out:
        rp = os.path.abspath(p)
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq


def write_txn_csv(path: str, txns: list[Transaction]) -> None:
    import csv

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["txn_date", "description", "amount", "debit", "credit",
                    "balance", "direction", "flags", "source_file", "page", "line",
                    "raw_line"])
        for t in txns:
            w.writerow([
                t.txn_date, t.description,
                to_float(t.amount) if t.amount is not None else "",
                to_float(t.debit) if t.debit is not None else "",
                to_float(t.credit) if t.credit is not None else "",
                to_float(t.balance) if t.balance is not None else "",
                t.direction or "", ",".join(t.flags), t.source_file,
                t.page_no, t.line_no, t.raw_line,
            ])


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bankstmt",
        description="Extract transactions from PDF bank statements into SQLite.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("inputs", nargs="+",
                   help="PDF files, glob patterns, or a folder of PDFs")
    p.add_argument("--db", default="statements.db",
                   help="SQLite file to write (created if missing). Default: statements.db")
    p.add_argument("--report", default=None,
                   help="Write the Markdown report here (default: <db>.report.md)")
    p.add_argument("--issues-csv", default=None,
                   help="Also write every flagged line to this CSV")
    p.add_argument("--csv", dest="txn_csv", default=None,
                   help="Also write every parsed transaction to this CSV")
    p.add_argument("--currency", default=None,
                   help="Currency code to stamp on the rows, e.g. USD. Purely a label.")
    p.add_argument("--monthfirst", action="store_true",
                   help="Read ambiguous numeric dates as MM/DD/YYYY. "
                        "Only used when the document itself is ambiguous "
                        "(default: DD/MM/YYYY)")
    p.add_argument("--year", type=int, default=None,
                   help="Year to assume for statements whose lines omit it "
                        "(otherwise taken from the statement header)")
    p.add_argument("--password", default=None,
                   help="Password for encrypted PDFs")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse and report, but write nothing to the database")
    p.add_argument("--quiet", action="store_true", help="Only print warnings and errors")
    p.add_argument("--version", action="version", version=f"bankstmt {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    files = expand_inputs(args.inputs)
    missing = [f for f in files if not os.path.isfile(f)]
    for f in missing:
        print(f"error: no such file: {f}", file=sys.stderr)
    files = [f for f in files if f not in missing]
    if not files:
        print("error: nothing to do — no readable PDFs matched.", file=sys.stderr)
        return 2

    all_txns: list[Transaction] = []
    all_issues: list[Issue] = []
    coverage: list[dict] = []
    total_inserted = total_dupes = 0
    conn = None if args.dry_run else connect(args.db)

    for path in files:
        name = os.path.basename(path)
        if not args.quiet:
            print(f"→ {name}")
        try:
            pages = extract_pages(path, password=args.password)
        except Exception as exc:  # unreadable / encrypted / scanned
            msg = str(exc)
            print(f"  ! could not read: {msg}", file=sys.stderr)
            all_issues.append(Issue(source_file=name, page_no=None, line_no=None,
                                    severity="error", category="unreadable_pdf",
                                    message=msg))
            continue

        parser = StatementParser(
            source_file=name,
            dayfirst_default=not args.monthfirst,
            fallback_year=args.year,
        )
        txns = parser.parse(pages)
        all_txns.extend(txns)
        all_issues.extend(parser.issues)
        coverage.append({
            "file": name,
            "dated": parser.dated_line_count,
            "imported": len(txns),
            "skipped": parser.skipped_dated_lines,
        })

        if not args.quiet:
            cols = ", ".join(f"{r}" for r in parser.value_columns) or "none (positional mode)"
            print(f"  money columns: {cols}")
            print(f"  dates read as {'DD/MM' if parser.dayfirst else 'MM/DD'}")
            print(f"  {parser.dated_line_count} dated line(s) -> "
                  f"{len(txns)} transaction(s), "
                  f"{parser.skipped_dated_lines} skipped with a reason, "
                  f"{sum(1 for i in parser.issues if i.severity != 'info')} flagged")

        if conn is not None:
            ins, dup = insert_transactions(conn, txns, sha1_of(path), args.currency)
            record_run(conn, name, sha1_of(path), len(txns), ins, dup,
                       parser.issues, __version__)
            total_inserted += ins
            total_dupes += dup
            if not args.quiet:
                print(f"  inserted {ins}, skipped {dup} already present")

    report_path = args.report or (
        os.path.splitext(args.db)[0] + ".report.md" if not args.dry_run else "report.md"
    )
    md = build_markdown(all_txns, all_issues, [os.path.basename(f) for f in files],
                        total_inserted, total_dupes, args.db, coverage=coverage)
    write_markdown(report_path, md)
    if args.issues_csv:
        write_issue_csv(args.issues_csv, all_issues)
    if args.txn_csv:
        write_txn_csv(args.txn_csv, all_txns)

    errors = [i for i in all_issues if i.severity == "error"]
    warnings = [i for i in all_issues if i.severity == "warning"]
    print()
    print(f"Parsed {len(all_txns)} transaction(s) from {len(files)} file(s).")
    if not args.dry_run:
        print(f"Database: {args.db}  (+{total_inserted} rows, {total_dupes} duplicates skipped)")
    else:
        print("Dry run — database not written.")
    print(f"Report:   {report_path}")
    if errors or warnings:
        print(f"Flagged:  {len(errors)} error(s), {len(warnings)} warning(s) — see the report.")
    else:
        print("Flagged:  nothing. Every line parsed cleanly.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
