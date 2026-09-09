# Statement import report

- Files processed: **3**
- Transactions parsed: **29**
- Rows inserted into `samples/sample.db`: **29**
- Rows skipped as already present: **0**

## Coverage (every dated line accounted for)

| File | Dated lines found | Imported | Deliberately skipped | Unaccounted for |
| --- | ---: | ---: | ---: | ---: |
| `sample_plain_no_header.pdf` | 8 | 8 | 0 | 0 |
| `sample_uk_debit_credit.pdf` | 13 | 12 | 1 | 0 |
| `sample_us_signed.pdf` | 10 | 9 | 1 | 0 |

✅ Every line that begins with a date was either imported or logged below with a reason. Nothing disappeared quietly.

## Totals

- Money in: **8,963.96** across 7 rows
- Money out: **6,071.38** across 22 rows
- Net movement: **2,892.58**
- Date range: **2025-03-02** to **2025-04-30**

## Reconciliation

Does the sum of the extracted amounts explain the movement in the statement's own balance column? If it does, nothing was missed or misread.

| File | First balance | Last balance | Balance moved by | Sum of amounts | Check |
| --- | ---: | ---: | ---: | ---: | --- |
| `sample_plain_no_header.pdf` | 3,020.11 | 2,727.66 | -292.45 | -292.45 | ✅ agrees |
| `sample_uk_debit_credit.pdf` | 2,407.40 | 2,084.49 | -322.91 | -322.91 | ✅ agrees |
| `sample_us_signed.pdf` | 5,033.25 | 5,920.98 | 887.73 | 887.73 | ✅ agrees |

## How the sign of each amount was decided

- 13 row(s): from a minus sign, brackets or a DR/CR marker
- 12 row(s): from the Debit/Credit column it sat in (most reliable)
- 3 row(s): from the movement of the running balance
- 1 row(s): could not be determined — see `sign_unknown` below

## Rows to look at

1 line(s) need a human eye.

| Severity | Category | Page:Line | What happened | Line as printed |
| --- | --- | --- | --- | --- |
| warning | `sign_unknown` | 1:3 | No debit/credit marker, column or balance movement to decide the sign; stored exactly as printed | `02 Apr 2025 SALARY CREDIT NORTHWIND LTD 2,750.00 3,020.11` |

### What the categories mean

- `opening_balance` (2): opening balance detected and used to seed the checks
- `summary_line` (2): a totals / closing line, ignored on purpose
- `sign_unknown` (1): nothing on the line said whether it was money in or out

---

Query the results with, for example:

```sql
SELECT txn_date, description, amount, balance FROM transactions ORDER BY txn_date;
SELECT * FROM transactions WHERE flags <> '';   -- only the doubtful rows
SELECT SUM(amount) FROM clean_transactions;     -- only the fully trusted rows
```
