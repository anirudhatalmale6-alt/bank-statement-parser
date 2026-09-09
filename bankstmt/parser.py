"""Statement line -> transaction.

How a column is found
---------------------
Heading rows ("Date  Description  Debit  Credit  Balance") are useful but not
sufficient: headings are often left-aligned while the numbers under them are
right-aligned, so a long description can drift under the Debit heading and a
reference number like ``REF INVOICE 2201`` gets misread as money.

So the parser makes two passes.  The first pass collects the right-hand edge of
every money-looking token on every dated line and clusters them.  Real amount
columns are right-aligned, so they show up as tight clusters supported by many
rows; a stray digit inside a description does not.  Only tokens sitting in a
supported cluster are treated as money — everything else stays in the
description, where it belongs.  The heading row, when present, is then used to
*name* those clusters (debit / credit / balance / amount).

The line state machine
----------------------
* a line that **starts with a date** opens a new transaction;
* a line that does not, sits directly under an open transaction, is on the same
  page and carries no column money, is a **wrapped description** and is
  appended;
* an **opening balance** line seeds the running balance without emitting a row;
* anything that looked like it should have been a transaction but was not
  usable goes to the issue log rather than being silently dropped.

How the sign is decided
-----------------------
Most trustworthy first:

1. the column the number sat in (Debit -> negative, Credit -> positive);
2. an explicit marker in the text (``-``, ``(123.45)``, ``DR`` / ``CR``);
3. the movement of the running balance (``balance - previous_balance``);
4. nothing — the row is stored exactly as printed and flagged ``sign_unknown``.

Rule 3 also *checks* rules 1 and 2: where a balance column exists and the
arithmetic does not close, the row is flagged ``balance_mismatch`` instead of
quietly poisoning the totals.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from . import amounts as A
from . import dates as D
from .extract import ColumnMap, ExtractedPage, Line, Word, detect_columns

# Lines that are page furniture rather than data.
NOISE_RE = re.compile(
    r"""^\s*(
        page\s+\d+\s*(of\s*\d+)?
      | continued(\s+on\s+next\s+page)?
      | statement\s+(of\s+account|period|date|for)\b.*
      | account\s+(number|name|summary)\b.*
      | sort\s+code\b.*
      | (iban|swift|bic)\b.*
      | www\.\S+
      | .{0,3}
    )\s*$""",
    re.I | re.VERBOSE,
)

OPENING_RE = re.compile(
    r"\b(opening\s+balance|opening\s+bal|beginning\s+balance|balance\s+(b/?f|brought\s+forward|forward)"
    r"|b/?f\b|brought\s+forward|previous\s+balance|starting\s+balance|balance\s+at\s+\d)\b",
    re.I,
)

CLOSING_RE = re.compile(
    r"\b(closing\s+balance|ending\s+balance|end\s+balance|balance\s+c/?f|carried\s+forward"
    r"|totals?\b|subtotal|end\s+of\s+statement)\b",
    re.I,
)

TOLERANCE = Decimal("0.011")  # a cent of slack for rounding in the source PDF

#: How close two right edges must be (points) to count as the same column.
CLUSTER_TOLERANCE = 12.0

#: Vertical gap (points) beyond which a line is no longer a description wrap.
WRAP_MAX_GAP = 26.0


@dataclass
class Transaction:
    source_file: str
    page_no: int
    line_no: int
    txn_date: Optional[str]
    date_raw: str
    description: str
    amount: Optional[Decimal]
    debit: Optional[Decimal]
    credit: Optional[Decimal]
    balance: Optional[Decimal]
    direction: Optional[str]           # 'debit' | 'credit' | None
    sign_source: str                   # how the sign was decided
    raw_line: str
    flags: list[str] = field(default_factory=list)
    txn_hash: str = ""


@dataclass
class Issue:
    source_file: str
    page_no: Optional[int]
    line_no: Optional[int]
    severity: str          # 'error' | 'warning' | 'info'
    category: str
    message: str
    raw_line: str = ""


@dataclass
class MoneyToken:
    """A money value found on a line, with the words it came from."""

    amount: A.Amount
    first: int      # index of the first word
    last: int       # index of the last word
    x0: float
    x1: float


def _norm_desc(text: str) -> str:
    """Trim, collapse internal whitespace, drop stray column pipes."""
    return re.sub(r"\s+", " ", text.replace("|", " ")).strip(" .,-–—:")


def _hash(date: Optional[str], desc: str, amount: Optional[Decimal],
          balance: Optional[Decimal], occurrence: int) -> str:
    """Stable identity for a transaction, used for de-duplication.

    Includes an *occurrence index* so a genuine pair of identical same-day
    transactions both survive, while re-importing the same PDF does not create
    copies.  Deliberately excludes the file name and page/line numbers: the
    same transaction re-issued in a corrected statement is still the same
    transaction.
    """
    key = "|".join(
        [
            date or "",
            re.sub(r"\s+", " ", desc.lower()).strip(),
            f"{amount:.2f}" if amount is not None else "",
            f"{balance:.2f}" if balance is not None else "",
            str(occurrence),
        ]
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def date_word_indices(line: Line, date_hit: D.DateHit) -> set[int]:
    """Indices of the words that make up the leading date on a line."""
    return {
        i for i, (_s, e, _w) in enumerate(line.spans()) if e <= date_hit.end
    }


def line_money_tokens(line: Line) -> list[MoneyToken]:
    """Every money value on a line, word-aligned so we keep its x position.

    Words are examined one at a time, with two merges that PDFs commonly need:
    a lone currency symbol glued to the following number (``$`` ``1,234.56``)
    and a trailing marker (``1,234.56`` ``CR``).
    """
    out: list[MoneyToken] = []
    words = line.words
    i = 0
    while i < len(words):
        w = words[i]
        text = w.text
        first = last = i

        # "$" followed by the number.
        if re.fullmatch(r"[%s]+" % re.escape(A.CURRENCY_SYMBOLS.strip()), text) and i + 1 < len(words):
            text = text + words[i + 1].text
            last = i + 1

        amt = A.parse_amount(text)
        if amt is not None:
            # A trailing CR / DR marker as its own word.
            if last + 1 < len(words) and words[last + 1].text.upper().strip(".") in (
                A.CREDIT_MARKERS | A.DEBIT_MARKERS
            ):
                merged = A.parse_amount(text + " " + words[last + 1].text)
                if merged is not None:
                    amt = merged
                    last = last + 1
            out.append(
                MoneyToken(amount=amt, first=first, last=last,
                           x0=words[first].x0, x1=words[last].x1)
            )
            i = last + 1
            continue
        i += 1
    return out


def cluster_value_columns(
    edges: list[float], min_support: int
) -> list[tuple[float, float, int]]:
    """Group right-hand edges into columns.

    Returns ``(low, high, support)`` per cluster, left to right, keeping only
    clusters seen on at least ``min_support`` lines.
    """
    if not edges:
        return []
    edges = sorted(edges)
    groups: list[list[float]] = [[edges[0]]]
    for e in edges[1:]:
        if e - groups[-1][-1] <= CLUSTER_TOLERANCE:
            groups[-1].append(e)
        else:
            groups.append([e])
    return [
        (g[0] - CLUSTER_TOLERANCE, g[-1] + CLUSTER_TOLERANCE, len(g))
        for g in groups
        if len(g) >= min_support
    ]


class StatementParser:
    """Parses one PDF's worth of extracted pages."""

    def __init__(
        self,
        source_file: str,
        dayfirst_default: bool = True,
        fallback_year: Optional[int] = None,
    ) -> None:
        self.source_file = source_file
        self.dayfirst_default = dayfirst_default
        self.dayfirst = dayfirst_default
        self.fallback_year = fallback_year
        self.columns = ColumnMap()
        #: role -> (x_low, x_high) for the *values*, derived from clustering.
        self.value_columns: dict[str, tuple[float, float]] = {}
        self.issues: list[Issue] = []
        self.transactions: list[Transaction] = []
        self._prev_balance: Optional[Decimal] = None
        self._occurrences: dict[str, int] = {}
        self._seen_any_txn = False
        #: How many lines started with a date — the denominator for coverage.
        self.dated_line_count = 0
        #: Dated lines deliberately not imported (opening/closing/notes).
        self.skipped_dated_lines = 0

    # ------------------------------------------------------------------ util

    def _issue(self, severity: str, category: str, message: str,
               line: Optional[Line] = None) -> None:
        self.issues.append(
            Issue(
                source_file=self.source_file,
                page_no=line.page_no if line else None,
                line_no=line.line_no if line else None,
                severity=severity,
                category=category,
                message=message,
                raw_line=line.text if line else "",
            )
        )

    # ------------------------------------------------------- geometry passes

    def _learn_geometry(self, pages: list[ExtractedPage]) -> None:
        """Find the heading row (if any) and the right-aligned value columns."""
        dated: list[tuple[Line, D.DateHit]] = []
        for page in pages:
            for line in page.lines:
                cmap = detect_columns(line)
                if cmap and not self.columns:
                    self.columns = cmap
                hit = D.find_leading_date(line.text)
                if hit:
                    dated.append((line, hit))
        self.dated_line_count = len(dated)

        # Collect the right-hand edge of every money token *except* the date
        # itself — '02 Apr 2025' contains two perfectly good-looking numbers.
        edges: list[float] = []
        for line, hit in dated:
            skip = date_word_indices(line, hit)
            for tok in line_money_tokens(line):
                if tok.first in skip:
                    continue
                edges.append(tok.x1)

        clusters = cluster_value_columns(edges, min_support=2)
        if not clusters:
            return
        max_support = max(c[2] for c in clusters)
        numeric_roles = ("debit", "credit", "balance", "amount")

        named: dict[str, tuple[float, float]] = {}
        if self.columns:
            # Headings tell us what each cluster *is*, so a column used by only
            # a handful of rows (a Credit column on a month of spending) still
            # counts.  Clusters that match no heading are description noise.
            for lo, hi, _support in clusters:
                role = self.columns.role_at((lo + hi) / 2.0)
                if role in numeric_roles and role not in named:
                    named[role] = (lo, hi)

        if not named:
            # No headings: keep the well-supported clusters and read them
            # left-to-right.  The balance check later will catch a bad guess.
            strong = [c for c in clusters if c[2] >= max(2, max_support * 0.4)][-4:]
            fallback = {1: ["amount"], 2: ["amount", "balance"],
                        3: ["debit", "credit", "balance"],
                        4: ["debit", "credit", "amount", "balance"]}
            for role, (lo, hi, _s) in zip(fallback.get(len(strong), []), strong):
                named.setdefault(role, (lo, hi))
        self.value_columns = named

    def _role_for(self, tok: MoneyToken) -> Optional[str]:
        for role, (lo, hi) in self.value_columns.items():
            if lo <= tok.x1 <= hi:
                return role
        return None

    # --------------------------------------------------------------- parsing

    def parse(self, pages: list[ExtractedPage]) -> list[Transaction]:
        # Pass 1: settle the day/month convention for the whole document.
        hits = []
        for p in pages:
            for ln in p.lines:
                h = D.find_leading_date(ln.text)
                if h:
                    hits.append(h)
        self.dayfirst = D.infer_dayfirst(hits, default=self.dayfirst_default)
        if self.fallback_year is None:
            self.fallback_year = D.find_statement_year(p.raw_text for p in pages)

        # Pass 2: work out where the money columns actually are.
        self._learn_geometry(pages)

        # Pass 3: walk the lines.
        current: Optional[Transaction] = None
        for page in pages:
            prev_line: Optional[Line] = None
            for line in page.lines:
                current = self._handle_line(line, current, prev_line)
                prev_line = line
            # A wrapped description never survives a page break.
            self._commit(current)
            current = None
        return self.transactions

    def _handle_line(self, line: Line, current: Optional[Transaction],
                     prev_line: Optional[Line]) -> Optional[Transaction]:
        text = line.text.strip()
        if not text:
            return current

        if detect_columns(line):
            self._commit(current)
            return None

        if NOISE_RE.match(text):
            return current

        tokens = line_money_tokens(line)
        column_tokens = [t for t in tokens if self._role_for(t)]
        date_hit = D.find_leading_date(text)

        # Opening / brought-forward balance: seeds the running balance.
        if OPENING_RE.search(text) and tokens:
            self._prev_balance = tokens[-1].amount.value
            if date_hit:
                self.skipped_dated_lines += 1
            self._issue("info", "opening_balance",
                        f"Opening balance taken as {tokens[-1].amount.value}", line)
            self._commit(current)
            return None

        if CLOSING_RE.search(text):
            if date_hit:
                self.skipped_dated_lines += 1
            self._commit(current)
            self._issue("info", "summary_line", "Ignored totals/closing line", line)
            return None

        if date_hit is None:
            # No date: a wrapped description, or something we cannot place.
            if current is not None and not column_tokens and self._is_wrap(line, prev_line):
                extra = _norm_desc(text)
                if extra:
                    current.description = _norm_desc(current.description + " " + extra)
                    current.raw_line += " ↵ " + text
                return current
            if column_tokens and self._seen_any_txn:
                self._issue(
                    "warning", "orphan_amount",
                    "Line carries a column amount but no date and follows no transaction",
                    line,
                )
            self._commit(current)
            return None

        # A new transaction starts here.
        self._commit(current)
        return self._start_transaction(line, date_hit, tokens)

    def _is_wrap(self, line: Line, prev_line: Optional[Line]) -> bool:
        """Is this line a continuation of the row above, or unrelated text?"""
        if prev_line is None or prev_line.page_no != line.page_no:
            return False
        if line.top - prev_line.top > WRAP_MAX_GAP:
            return False
        # When the layout is known, a wrap starts in the description column,
        # never back in the date column.
        if self.columns and "description" in self.columns.ranges:
            dx0 = self.columns.ranges["description"][0]
            if line.words and line.words[0].x0 < dx0:
                return False
        return True

    # ------------------------------------------------------------ one record

    def _start_transaction(self, line: Line, date_hit: D.DateHit,
                           tokens: list[MoneyToken]) -> Optional[Transaction]:
        text = line.text
        iso, err = D.to_iso(date_hit, self.dayfirst, self.fallback_year)
        flags: list[str] = []
        if err:
            self._issue("error", "bad_date", f"Could not read date: {err}", line)
            flags.append("malformed_date")
        elif (why := D.is_suspicious(iso)):
            self._issue("warning", "suspicious_date", why, line)
            flags.append("suspicious_date")

        debit = credit = balance = amount = None
        direction: Optional[str] = None
        sign_source = "none"
        consumed: set[int] = set()

        # Which words hold the date, so we can strip them from the description.
        date_words = date_word_indices(line, date_hit)

        by_role: dict[str, MoneyToken] = {}
        for tok in tokens:
            role = self._role_for(tok)
            if role is None:
                continue
            if tok.first in date_words:      # never eat the date itself
                continue
            by_role[role] = tok              # rightmost wins per column
            consumed.update(range(tok.first, tok.last + 1))

        if by_role:
            debit = by_role["debit"].amount.value if "debit" in by_role else None
            credit = by_role["credit"].amount.value if "credit" in by_role else None
            balance = by_role["balance"].amount.value if "balance" in by_role else None
            if "amount" in by_role:
                a = by_role["amount"].amount
                amount = a.value
                if a.explicit_negative:
                    direction, sign_source = "debit", "explicit_sign"
                elif a.explicit_positive:
                    direction, sign_source = "credit", "explicit_sign"
            if debit is not None and credit is None:
                amount, direction, sign_source = -abs(debit), "debit", "column"
            elif credit is not None and debit is None:
                amount, direction, sign_source = abs(credit), "credit", "column"
            elif debit is not None and credit is not None:
                self._issue("warning", "both_columns",
                            "Both debit and credit columns hold a value", line)
                flags.append("both_columns")
                amount = abs(credit) - abs(debit)
                direction = "credit" if amount >= 0 else "debit"
                sign_source = "column"
        else:
            # No usable column geometry: fall back to trailing numbers.
            trailing = [t for t in tokens if t.first not in date_words]
            if not trailing:
                self._issue("warning", "no_amount",
                            "Dated line with no amount — treated as a note", line)
                flags.append("no_amount")
            else:
                if len(trailing) >= 2:
                    balance = trailing[-1].amount.value
                    a = trailing[-2].amount
                    consumed.update(range(trailing[-2].first, trailing[-1].last + 1))
                else:
                    a = trailing[-1].amount
                    consumed.update(range(trailing[-1].first, trailing[-1].last + 1))
                amount = a.value
                if a.explicit_negative:
                    direction, sign_source = "debit", "explicit_sign"
                elif a.explicit_positive:
                    direction, sign_source = "credit", "explicit_sign"

        # --- description: everything that was not the date or a column value -
        description = _norm_desc(
            " ".join(w.text for i, w in enumerate(line.words)
                     if i not in date_words and i not in consumed)
        )
        if not description:
            self._issue("warning", "empty_description",
                        "Transaction has no description text", line)
            flags.append("empty_description")

        # --- sign from the running balance ---------------------------------
        if amount is not None and balance is not None and self._prev_balance is not None:
            delta = balance - self._prev_balance
            mag = abs(amount)
            if abs(delta - mag) <= TOLERANCE:
                inferred = "credit"
            elif abs(delta + mag) <= TOLERANCE:
                inferred = "debit"
            else:
                inferred = None
            if inferred is None:
                self._issue(
                    "warning", "balance_mismatch",
                    f"Balance moved by {delta} but the amount is {mag} "
                    f"(previous balance {self._prev_balance})", line,
                )
                flags.append("balance_mismatch")
            else:
                if direction and direction != inferred:
                    self._issue(
                        "warning", "sign_conflict",
                        f"Line reads as a {direction} but the balance moved like a "
                        f"{inferred}; kept the balance's reading", line,
                    )
                    flags.append("sign_conflict")
                if sign_source == "none":
                    sign_source = "balance_delta"
                direction = inferred
                amount = mag if inferred == "credit" else -mag

        if amount is not None and direction is None:
            self._issue(
                "warning", "sign_unknown",
                "No debit/credit marker, column or balance movement to decide the "
                "sign; stored exactly as printed", line,
            )
            flags.append("sign_unknown")
        elif amount is not None:
            amount = abs(amount) if direction == "credit" else -abs(amount)

        if balance is not None:
            self._prev_balance = balance

        return Transaction(
            source_file=self.source_file,
            page_no=line.page_no,
            line_no=line.line_no,
            txn_date=iso,
            date_raw=date_hit.raw,
            description=description,
            amount=amount,
            debit=abs(debit) if debit is not None else None,
            credit=abs(credit) if credit is not None else None,
            balance=balance,
            direction=direction,
            sign_source=sign_source,
            raw_line=text,
            flags=flags,
        )

    def _commit(self, txn: Optional[Transaction]) -> None:
        if txn is None:
            return
        if txn.amount is None and txn.balance is None:
            self.skipped_dated_lines += 1
            self.issues.append(
                Issue(source_file=self.source_file, page_no=txn.page_no,
                      line_no=txn.line_no, severity="warning", category="dropped_row",
                      message="Dated line carried no usable numbers; not imported",
                      raw_line=txn.raw_line)
            )
            return
        # A dated line whose only number is the balance is a balance marker,
        # not a transaction (e.g. "01/03/2025 Balance brought forward 2,450.00").
        # Before the first real transaction that is an opening balance and is
        # expected; afterwards it means an amount went missing, which is a
        # warning — a row that vanishes without a word is the worst outcome.
        if txn.amount is None and txn.balance is not None:
            self._prev_balance = txn.balance
            expected = not self._seen_any_txn or OPENING_RE.search(txn.raw_line) \
                or CLOSING_RE.search(txn.raw_line)
            self.skipped_dated_lines += 1
            self.issues.append(
                Issue(source_file=self.source_file, page_no=txn.page_no,
                      line_no=txn.line_no,
                      severity="info" if expected else "warning",
                      category="balance_only_row",
                      message=f"Row has a balance ({txn.balance}) but no amount; "
                              + ("treated as an opening/closing balance marker, not imported"
                                 if expected else
                                 "NOT imported — check whether an amount was missed"),
                      raw_line=txn.raw_line)
            )
            return
        self._seen_any_txn = True
        key = f"{txn.txn_date}|{txn.description.lower()}|{txn.amount}|{txn.balance}"
        n = self._occurrences.get(key, 0)
        self._occurrences[key] = n + 1
        txn.txn_hash = _hash(txn.txn_date, txn.description, txn.amount, txn.balance, n)
        if n:
            self.issues.append(
                Issue(
                    source_file=self.source_file,
                    page_no=txn.page_no,
                    line_no=txn.line_no,
                    severity="info",
                    category="repeated_row",
                    message=f"Identical to an earlier row in the same file (#{n + 1}); "
                            "kept, since banks do post the same charge twice",
                    raw_line=txn.raw_line,
                )
            )
        self.transactions.append(txn)
