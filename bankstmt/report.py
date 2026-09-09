"""The "what should I look at?" report.

Two outputs, both optional:

* a Markdown summary meant to be read by a human in 30 seconds;
* a CSV of every issue, meant to be opened in Excel next to the PDF.

The report leads with the checks that would catch a *silent* error — rows
whose sign could not be established, balances that do not reconcile, dates
that did not parse — because those are the ones that quietly corrupt a total.
"""

from __future__ import annotations

import csv
from collections import Counter
from decimal import Decimal
from typing import Iterable, Optional

from .parser import Issue, Transaction

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

CATEGORY_HELP = {
    "bad_date": "the date text could not be turned into a real calendar date",
    "suspicious_date": "date parsed, but is in the future or absurdly old",
    "balance_mismatch": "amount does not explain the change in the running balance",
    "sign_conflict": "the printed sign and the balance movement disagree",
    "sign_unknown": "nothing on the line said whether it was money in or out",
    "both_columns": "debit and credit columns both held a value on one line",
    "no_amount": "a dated line with no number on it",
    "orphan_amount": "a number with no transaction to attach it to",
    "empty_description": "the transaction has no description text",
    "dropped_row": "a dated line that produced no usable data and was not imported",
    "repeated_row": "an exact duplicate of an earlier row in the same PDF",
    "opening_balance": "opening balance detected and used to seed the checks",
    "summary_line": "a totals / closing line, ignored on purpose",
}


def write_issue_csv(path: str, issues: Iterable[Issue]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["severity", "category", "source_file", "page", "line",
                    "message", "raw_line"])
        for i in sorted(issues, key=lambda i: (SEVERITY_ORDER.get(i.severity, 9),
                                               i.source_file, i.page_no or 0,
                                               i.line_no or 0)):
            w.writerow([i.severity, i.category, i.source_file, i.page_no,
                        i.line_no, i.message, i.raw_line])


def build_markdown(
    txns: list[Transaction],
    issues: list[Issue],
    files: list[str],
    inserted: int,
    duplicates: int,
    db_path: str,
    coverage: Optional[list[dict]] = None,
) -> str:
    lines: list[str] = []
    add = lines.append

    add("# Statement import report")
    add("")
    add(f"- Files processed: **{len(files)}**")
    add(f"- Transactions parsed: **{len(txns)}**")
    add(f"- Rows inserted into `{db_path}`: **{inserted}**")
    add(f"- Rows skipped as already present: **{duplicates}**")
    add("")

    # ---- coverage: did every dated line end up somewhere? -----------------
    # This is the check that catches the dangerous failure — a row silently
    # disappearing — which a count of "rows imported" on its own cannot.
    if coverage:
        add("## Coverage (every dated line accounted for)")
        add("")
        add("| File | Dated lines found | Imported | Deliberately skipped | Unaccounted for |")
        add("| --- | ---: | ---: | ---: | ---: |")
        total_gap = 0
        for c in coverage:
            gap = c["dated"] - c["imported"] - c["skipped"]
            total_gap += max(gap, 0)
            mark = "" if gap == 0 else f" ⚠️ {gap}"
            add(f"| `{c['file']}` | {c['dated']} | {c['imported']} | {c['skipped']} "
                f"| {gap}{mark} |")
        add("")
        if total_gap == 0:
            add("✅ Every line that begins with a date was either imported or logged "
                "below with a reason. Nothing disappeared quietly.")
        else:
            add(f"⚠️ **{total_gap} dated line(s) are unaccounted for.** "
                "Send me the PDF page they are on — this is a parser gap, not your data.")
        add("")

    # ---- money sanity ----------------------------------------------------
    with_amount = [t for t in txns if t.amount is not None]
    debits = [t for t in with_amount if t.amount < 0]
    credits = [t for t in with_amount if t.amount > 0]
    total_in = sum((t.amount for t in credits), Decimal("0"))
    total_out = sum((-t.amount for t in debits), Decimal("0"))
    add("## Totals")
    add("")
    add(f"- Money in: **{total_in:,.2f}** across {len(credits)} rows")
    add(f"- Money out: **{total_out:,.2f}** across {len(debits)} rows")
    add(f"- Net movement: **{total_in - total_out:,.2f}**")
    dated = [t.txn_date for t in txns if t.txn_date]
    if dated:
        add(f"- Date range: **{min(dated)}** to **{max(dated)}**")
    add("")

    # ---- reconciliation --------------------------------------------------
    # Each statement is its own ledger, so this must be done per file — never
    # across files, which would compare one account's balance to another's.
    by_file: dict[str, list[Transaction]] = {}
    for t in txns:
        by_file.setdefault(t.source_file, []).append(t)

    recon_rows: list[str] = []
    for filename, rows in by_file.items():
        balance_idx = [n for n, t in enumerate(rows) if t.balance is not None]
        if len(balance_idx) < 2:
            recon_rows.append(
                f"| `{filename}` | – | – | – | – | no balance column to check against |"
            )
            continue
        first_i, last_i = balance_idx[0], balance_idx[-1]
        first, last = rows[first_i], rows[last_i]
        moved = last.balance - first.balance
        # Everything posted *after* the first balance we saw, up to the last.
        explained = sum(
            (t.amount for t in rows[first_i + 1 : last_i + 1] if t.amount is not None),
            Decimal("0"),
        )
        gap = moved - explained
        verdict = ("✅ agrees" if abs(gap) <= Decimal("0.02")
                   else f"⚠️ **off by {gap:,.2f}**")
        recon_rows.append(
            f"| `{filename}` | {first.balance:,.2f} | {last.balance:,.2f} | "
            f"{moved:,.2f} | {explained:,.2f} | {verdict} |"
        )

    if recon_rows:
        add("## Reconciliation")
        add("")
        add("Does the sum of the extracted amounts explain the movement in the "
            "statement's own balance column? If it does, nothing was missed or "
            "misread.")
        add("")
        add("| File | First balance | Last balance | Balance moved by | "
            "Sum of amounts | Check |")
        add("| --- | ---: | ---: | ---: | ---: | --- |")
        recon_rows.sort()
        for r in recon_rows:
            add(r)
        add("")

    # ---- how signs were decided -----------------------------------------
    add("## How the sign of each amount was decided")
    add("")
    for src, n in Counter(t.sign_source for t in txns).most_common():
        label = {
            "column": "from the Debit/Credit column it sat in (most reliable)",
            "explicit_sign": "from a minus sign, brackets or a DR/CR marker",
            "balance_delta": "from the movement of the running balance",
            "none": "could not be determined — see `sign_unknown` below",
        }.get(src, src)
        add(f"- {n} row(s): {label}")
    add("")

    # ---- issues ----------------------------------------------------------
    by_cat = Counter(i.category for i in issues)
    problems = [i for i in issues if i.severity in ("error", "warning")]
    add("## Rows to look at")
    add("")
    if not problems:
        add("Nothing flagged — every line parsed cleanly and the balances reconcile.")
        add("")
    else:
        add(f"{len(problems)} line(s) need a human eye.")
        add("")
        add("| Severity | Category | Page:Line | What happened | Line as printed |")
        add("| --- | --- | --- | --- | --- |")
        for i in sorted(problems, key=lambda i: (SEVERITY_ORDER.get(i.severity, 9),
                                                 i.page_no or 0, i.line_no or 0)):
            raw = (i.raw_line or "").replace("|", "\\|")
            if len(raw) > 90:
                raw = raw[:87] + "..."
            add(f"| {i.severity} | `{i.category}` | {i.page_no}:{i.line_no} | "
                f"{i.message.replace('|', '/')} | `{raw}` |")
        add("")

    if by_cat:
        add("### What the categories mean")
        add("")
        for cat, n in by_cat.most_common():
            add(f"- `{cat}` ({n}): {CATEGORY_HELP.get(cat, 'see the parser docs')}")
        add("")

    add("---")
    add("")
    add("Query the results with, for example:")
    add("")
    add("```sql")
    add("SELECT txn_date, description, amount, balance FROM transactions ORDER BY txn_date;")
    add("SELECT * FROM transactions WHERE flags <> '';   -- only the doubtful rows")
    add("SELECT SUM(amount) FROM clean_transactions;     -- only the fully trusted rows")
    add("```")
    return "\n".join(lines) + "\n"


def write_markdown(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
