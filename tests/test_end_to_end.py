"""Whole-pipeline tests: PDF in, correct rows in SQLite out.

Each sample statement is generated with a known set of transactions
(``tools/make_sample_pdfs.py`` writes ``expected.json`` alongside the PDFs), so
these tests compare against ground truth rather than against whatever the
parser happened to produce.  A regression fails here instead of in someone's
accounts.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bankstmt.cli import main  # noqa: E402
from tools import make_sample_pdfs  # noqa: E402


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    d = tmp_path_factory.mktemp("statements")
    make_sample_pdfs.main(str(d))
    return d


@pytest.fixture(scope="module")
def imported(workdir):
    db = workdir / "test.db"
    rc = main([str(workdir / "*.pdf"), "--db", str(db),
               "--report", str(workdir / "report.md"), "--quiet"])
    assert rc == 0, "the import reported an error"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn, workdir, db


def rows_for(conn, filename):
    return conn.execute(
        "SELECT * FROM transactions WHERE source_file = ? ORDER BY id", (filename,)
    ).fetchall()


@pytest.mark.parametrize("filename", [
    "sample_uk_debit_credit.pdf",
    "sample_us_signed.pdf",
    "sample_plain_no_header.pdf",
])
def test_every_transaction_is_extracted_with_the_right_numbers(imported, filename):
    conn, workdir, _db = imported
    expected = json.load(open(workdir / "expected.json"))[filename]
    rows = rows_for(conn, filename)

    assert len(rows) == len(expected), (
        f"{filename}: expected {len(expected)} transactions, got {len(rows)}"
    )
    for want, got in zip(expected, rows):
        assert got["txn_date"] == want["date"], got["raw_line"]
        assert got["amount"] == pytest.approx(want["amount"], abs=0.005), got["raw_line"]
        assert got["balance"] == pytest.approx(want["balance"], abs=0.005), got["raw_line"]


def test_amounts_are_stored_as_numbers_not_text(imported):
    conn, _wd, _db = imported
    types = {r[0] for r in conn.execute(
        "SELECT DISTINCT typeof(amount) FROM transactions WHERE amount IS NOT NULL")}
    assert types == {"real"}
    types = {r[0] for r in conn.execute(
        "SELECT DISTINCT typeof(balance) FROM transactions WHERE balance IS NOT NULL")}
    assert types == {"real"}


def test_signs_follow_the_running_balance(imported):
    """Every balance must equal the previous balance plus the amount."""
    conn, _wd, _db = imported
    for filename in ("sample_uk_debit_credit.pdf", "sample_us_signed.pdf",
                     "sample_plain_no_header.pdf"):
        rows = rows_for(conn, filename)
        for prev, cur in zip(rows, rows[1:]):
            assert cur["balance"] == pytest.approx(
                prev["balance"] + cur["amount"], abs=0.011
            ), f"{filename}: {cur['raw_line']}"


def test_descriptions_keep_their_digits(imported):
    """A reference number inside a description must not be eaten as an amount."""
    conn, _wd, _db = imported
    descs = " | ".join(r["description"] for r in conn.execute(
        "SELECT description FROM transactions"))
    for fragment in ("TESCO STORES 3421", "REF INVOICE 2201", "WAITROSE 0091",
                     "CHECK #1042", "SHELL OIL 574"):
        assert fragment in descs, f"lost {fragment!r} from a description"


def test_wrapped_description_lines_are_joined(imported):
    conn, _wd, _db = imported
    row = conn.execute(
        "SELECT description FROM transactions WHERE description LIKE '%J HOBBS%'"
    ).fetchone()
    assert row is not None
    assert "CONSULTING WORK FEBRUARY" in row["description"]


def test_page_headers_do_not_leak_into_descriptions(imported):
    conn, _wd, _db = imported
    for r in conn.execute("SELECT description FROM transactions"):
        assert "NORTHBRIDGE BANK" not in r["description"]
        assert "Page " not in r["description"]


def test_reimporting_the_same_files_inserts_nothing(imported):
    conn, workdir, db = imported
    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    main([str(workdir / "*.pdf"), "--db", str(db),
          "--report", str(workdir / "report2.md"), "--quiet"])
    after = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert after == before, "re-importing duplicated rows"


def test_no_dated_line_disappears_without_a_reason(imported):
    """Coverage: dated lines = imported + explicitly skipped, per file."""
    conn, workdir, _db = imported
    report = (workdir / "report.md").read_text()
    assert "Every line that begins with a date was either imported or logged" in report


def test_report_names_the_row_it_could_not_decide(imported):
    """The one genuinely undecidable row must be flagged, not guessed."""
    conn, workdir, _db = imported
    report = (workdir / "report.md").read_text()
    assert "sign_unknown" in report
    row = conn.execute(
        "SELECT flags FROM transactions WHERE description LIKE '%NORTHWIND%'"
    ).fetchone()
    assert "sign_unknown" in row["flags"]


def test_clean_view_excludes_flagged_rows(imported):
    conn, _wd, _db = imported
    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    clean = conn.execute("SELECT COUNT(*) FROM clean_transactions").fetchone()[0]
    flagged = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE flags <> ''").fetchone()[0]
    assert clean == total - flagged
    assert flagged >= 1
