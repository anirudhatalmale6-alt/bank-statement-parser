# bankstmt — PDF bank statements into SQLite

A small cross-platform command-line tool that reads bank statement PDFs, pulls
out every transaction, and writes them to a SQLite database with **real numeric
amounts** (never text), plus a report of anything it was not sure about.

Runs on macOS, Linux and Windows. Python 3.9+.

---

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Use it

```bash
# one statement
python -m bankstmt statements/march.pdf --db bank.db

# a whole folder, in one go
python -m bankstmt statements/ --db bank.db --report report.md

# try a new layout without touching the database
python -m bankstmt statements/new-bank.pdf --dry-run --csv preview.csv
```

That is the whole workflow. Re-running over the same folder is safe: rows
already in the database are skipped, so you can just drop new statements in and
run it again.

### Options

| Option | What it does |
| --- | --- |
| `--db PATH` | SQLite file to write. Created if missing. Default `statements.db` |
| `--report PATH` | Where to write the Markdown report. Default `<db>.report.md` |
| `--csv PATH` | Also dump every parsed transaction to CSV, for eyeballing against the PDF |
| `--issues-csv PATH` | Dump every flagged line to CSV |
| `--dry-run` | Parse and report, write nothing to the database |
| `--monthfirst` | Read ambiguous dates as MM/DD/YYYY (only used when the file itself is ambiguous) |
| `--year 2025` | Year to assume for statements whose lines omit it |
| `--currency GBP` | Stamp a currency label on the rows |
| `--password ...` | For password-protected PDFs |
| `--quiet` | Only print the summary |

---

## What you get

### The database

One table, `transactions`:

| Column | Type | Notes |
| --- | --- | --- |
| `id` | INTEGER | primary key |
| `txn_hash` | TEXT | unique — this is what makes re-imports safe |
| `txn_date` | TEXT | ISO `YYYY-MM-DD`, so it sorts and range-queries correctly |
| `date_raw` | TEXT | exactly as printed on the statement |
| `description` | TEXT | cleaned: trimmed, whitespace collapsed, wrapped lines joined |
| `amount` | **REAL** | signed — negative is money out |
| `debit` | REAL | unsigned, only when the PDF had a separate debit column |
| `credit` | REAL | unsigned, only when the PDF had a separate credit column |
| `balance` | REAL | running balance where the statement shows one |
| `direction` | TEXT | `debit` or `credit` |
| `currency` | TEXT | whatever you passed to `--currency` |
| `sign_source` | TEXT | how the sign was decided — see below |
| `flags` | TEXT | quality flags, empty string when the row is clean |
| `source_file`, `source_sha1`, `page_no`, `line_no`, `raw_line` | | full audit trail back to the exact line in the PDF |
| `imported_at` | TEXT | |

Plus a view, `clean_transactions`, holding only the rows with a readable date,
a known amount and no flags — use it when you want to be certain.

Two small extra tables, `import_runs` and `issues`, keep the history of every
import. Ignore them if you do not need them.

```sql
SELECT txn_date, description, amount, balance FROM transactions ORDER BY txn_date;
SELECT * FROM transactions WHERE flags <> '';    -- just the doubtful rows
SELECT SUM(amount) FROM clean_transactions;      -- only fully trusted rows
```

### The report

A Markdown file that opens with the two questions that actually matter:

1. **Coverage** — every line in the PDF that starts with a date is either
   imported or listed with a reason. If the numbers do not add up, the report
   says so at the top. A row can never disappear quietly.
2. **Reconciliation** — per file, the sum of the extracted amounts is compared
   against the movement in the statement's own balance column. If those two
   agree, nothing was missed or misread.

Then: totals, how the sign of every amount was decided, and a table of every
line that needs a human eye, quoted verbatim with its page and line number.

---

## How the numbers are handled

**Amounts.** Currency symbols stripped, thousands separators removed, both
`1,234.56` and `1.234,56` understood, and all of these read as negative:
`-1,234.56`, `(1,234.56)`, `1,234.56-`, `1,234.56 DR`. Parsed as `Decimal` so
there is no floating-point drift, then stored as SQLite `REAL` rounded to cents.

**Sign.** Getting a debit stored as a credit is the error that silently ruins a
total, so the sign is established in this order, most trustworthy first:

1. the column the number sat in — Debit or Credit;
2. an explicit marker in the text — a minus, brackets, `DR`/`CR`;
3. the movement of the running balance;
4. nothing. The amount is stored exactly as printed and the row is flagged
   `sign_unknown` in the report. **The tool never guesses.**

Where a balance column exists, rule 3 also *checks* rules 1 and 2. If the
arithmetic does not close, the row is flagged `balance_mismatch` rather than
quietly accepted.

**Columns.** Heading rows help but are not trusted on their own — headings are
often left-aligned while the numbers under them are right-aligned, so a long
description can drift under the "Debit" heading. Instead the parser collects the
right-hand edge of every number on every dated line and clusters them: a real
money column shows up as a tight, well-supported cluster, while a reference
number inside a description does not. That is why `CARD PAYMENT TO TESCO STORES
3421` keeps its `3421` instead of importing £3,421.

**Dates.** `03/04/2025` is resolved per *document*, not per line: if any date in
the file is unambiguous (a first component above 12), that settles the
convention for the whole file. Statements that omit the year take it from the
statement header. Anything impossible — `31/02/2025` — is reported, never
rounded into a nearby valid date.

**Cleaning.** Descriptions are trimmed and whitespace-collapsed, wrapped
description lines are joined onto their transaction, page headers and footers
are dropped, and duplicates are prevented by a unique key on the row's content
(date + description + amount + balance + occurrence), so a genuine pair of
identical same-day charges both survive while a re-import inserts nothing.

---

## Samples

`samples/` holds three synthetic statements in deliberately different layouts —
a UK debit/credit/balance table across two pages, a US signed-amount statement
with `($87.19)` style debits, and one with no heading row at all and
trailing-minus amounts. Every name and number in them is invented.

Regenerate them and the sample database with:

```bash
python tools/make_sample_pdfs.py samples/
python -m bankstmt samples/*.pdf --db samples/sample.db --report samples/report.md
```

`samples/sample.db` and `samples/report.md` in this repo were produced by
exactly that command.

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

The end-to-end tests compare the parsed output against the known contents of
the sample statements (`samples/expected.json`), so a regression fails a test
rather than someone's accounts. They also assert that amounts are stored with
SQLite type `real` and not text, that every balance equals the previous balance
plus the amount, that digits inside descriptions are not eaten, and that a
re-import inserts nothing.

## Scanned statements

If a PDF has no text layer (a photo or a scan), the tool says so and stops
rather than importing nothing and calling it success. Run it through OCR first:

```bash
ocrmypdf input.pdf output.pdf
```

## Layout

```
bankstmt/
  extract.py   PDF -> positioned text lines + column geometry
  dates.py     date recognition and ISO conversion
  amounts.py   money parsing (symbols, separators, CR/DR, brackets)
  parser.py    lines -> transactions + quality flags
  db.py        SQLite schema and idempotent inserts
  report.py    the Markdown / CSV report
  cli.py       the command line
tools/make_sample_pdfs.py   generates the synthetic samples
tests/                      unit + end-to-end tests
```
