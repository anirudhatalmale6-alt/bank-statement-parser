"""SQLite storage.

Everything you actually need lives in one table, ``transactions`` — the
client asked for a single table and that is what queries should hit.  Two
small companion tables (``import_runs``, ``issues``) exist so an import is
auditable later; ignore them freely.

Numeric columns are declared ``REAL`` and written as real numbers, never as
text.  Values are rounded to 2 decimal places on the way in so that
``SUM(amount)`` behaves.

De-duplication is enforced by the database itself: ``txn_hash`` is UNIQUE and
inserts use ``INSERT OR IGNORE``.  Re-running the same statement is therefore
free and idempotent.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Iterable

from .amounts import to_float
from .parser import Issue, Transaction

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_hash      TEXT    NOT NULL UNIQUE,   -- de-duplication key
    txn_date      TEXT,                      -- ISO 'YYYY-MM-DD' (NULL if unreadable)
    date_raw      TEXT,                      -- exactly as printed on the statement
    description   TEXT    NOT NULL,
    amount        REAL,                      -- signed: negative = money out
    debit         REAL,                      -- unsigned, only if the PDF had a debit column
    credit        REAL,                      -- unsigned, only if the PDF had a credit column
    balance       REAL,                      -- running balance, if the PDF shows one
    direction     TEXT CHECK (direction IN ('debit', 'credit') OR direction IS NULL),
    currency      TEXT,
    sign_source   TEXT,                      -- how the sign was decided (see parser docs)
    flags         TEXT,                      -- comma-separated quality flags, '' if clean
    source_file   TEXT    NOT NULL,
    source_sha1   TEXT,
    page_no       INTEGER,
    line_no       INTEGER,
    raw_line      TEXT,                      -- the original line, for auditing
    imported_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_txn_date ON transactions (txn_date);
CREATE INDEX IF NOT EXISTS ix_txn_source ON transactions (source_file);
CREATE INDEX IF NOT EXISTS ix_txn_amount ON transactions (amount);

-- Rows the parser is confident about: readable date, known sign, arithmetic OK.
CREATE VIEW IF NOT EXISTS clean_transactions AS
    SELECT * FROM transactions
    WHERE txn_date IS NOT NULL
      AND amount IS NOT NULL
      AND COALESCE(flags, '') = '';

CREATE TABLE IF NOT EXISTS import_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file    TEXT NOT NULL,
    source_sha1    TEXT,
    started_at     TEXT NOT NULL,
    rows_parsed    INTEGER NOT NULL,
    rows_inserted  INTEGER NOT NULL,
    rows_duplicate INTEGER NOT NULL,
    issues         INTEGER NOT NULL,
    tool_version   TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER REFERENCES import_runs (id),
    source_file TEXT,
    page_no     INTEGER,
    line_no     INTEGER,
    severity    TEXT,
    category    TEXT,
    message     TEXT,
    raw_line    TEXT
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def insert_transactions(
    conn: sqlite3.Connection,
    txns: Iterable[Transaction],
    source_sha1: str,
    currency: str | None,
) -> tuple[int, int]:
    """Insert rows, skipping ones already present.  Returns (inserted, skipped)."""
    now = _dt.datetime.now().isoformat(timespec="seconds")
    inserted = 0
    total = 0
    cur = conn.cursor()
    for t in txns:
        total += 1
        cur.execute(
            """
            INSERT OR IGNORE INTO transactions
                (txn_hash, txn_date, date_raw, description, amount, debit, credit,
                 balance, direction, currency, sign_source, flags, source_file,
                 source_sha1, page_no, line_no, raw_line, imported_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                t.txn_hash,
                t.txn_date,
                t.date_raw,
                t.description,
                to_float(t.amount) if t.amount is not None else None,
                to_float(t.debit) if t.debit is not None else None,
                to_float(t.credit) if t.credit is not None else None,
                to_float(t.balance) if t.balance is not None else None,
                t.direction,
                currency,
                t.sign_source,
                ",".join(t.flags),
                t.source_file,
                source_sha1,
                t.page_no,
                t.line_no,
                t.raw_line,
                now,
            ),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted, total - inserted


def record_run(
    conn: sqlite3.Connection,
    source_file: str,
    source_sha1: str,
    rows_parsed: int,
    rows_inserted: int,
    rows_duplicate: int,
    issues: list[Issue],
    tool_version: str,
) -> int:
    now = _dt.datetime.now().isoformat(timespec="seconds")
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO import_runs
            (source_file, source_sha1, started_at, rows_parsed, rows_inserted,
             rows_duplicate, issues, tool_version)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (source_file, source_sha1, now, rows_parsed, rows_inserted,
         rows_duplicate, len(issues), tool_version),
    )
    run_id = cur.lastrowid
    cur.executemany(
        """
        INSERT INTO issues
            (run_id, source_file, page_no, line_no, severity, category, message, raw_line)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        [
            (run_id, i.source_file, i.page_no, i.line_no, i.severity,
             i.category, i.message, i.raw_line)
            for i in issues
        ],
    )
    conn.commit()
    return run_id
