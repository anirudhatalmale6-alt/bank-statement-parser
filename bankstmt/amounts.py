"""Money parsing.

Bank statements write the same number a dozen different ways.  This module
turns any of those spellings into a Python ``Decimal`` (exact, no float
rounding) plus a flag saying whether the text itself carried a sign.

Handled spellings::

    1,234.56        ->  1234.56
    $1,234.56       ->  1234.56      (currency symbol stripped)
    -1.234,56       -> -1234.56      (European separators, auto-detected)
    (1,234.56)      -> -1234.56      (accounting parentheses)
    1,234.56-       -> -1234.56      (trailing minus, common on IBM-era output)
    1,234.56 CR     ->  1234.56      (explicit credit marker)
    1,234.56DR      -> -1234.56      (explicit debit marker)
    1 234,56        ->  1234.56      (space as thousands separator)

The parser never guesses a sign from the *description*; sign comes from the
text, from which column the number sat in, or from the running balance.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import NamedTuple, Optional

# Currency symbols we strip before parsing.  Add to taste; unknown symbols are
# also removed by the generic "drop anything that is not a digit/sep/sign" pass.
CURRENCY_SYMBOLS = "$€£¥₹₽₩₪₦R "

# Credit / debit markers that some statements append to the number.
CREDIT_MARKERS = {"CR", "C", "CRD", "CREDIT", "+"}
DEBIT_MARKERS = {"DR", "D", "DB", "DBT", "DEBIT", "-"}

# A token that *could* be money.  Deliberately loose: validation happens in
# parse_amount().  Requires at least one digit and allows the usual decorations.
MONEY_TOKEN_RE = re.compile(
    r"""
    (?<![\w/.])                     # not glued to a word, date or ratio
    (?P<body>
        [\(\-\+]?                   # leading sign / open paren
        [%s]?\s?                    # optional currency symbol
        \d{1,3}(?:[ ,. ']\d{3})*(?:[.,]\d{1,2})?   # grouped digits
        |
        [\(\-\+]?[%s]?\s?\d+(?:[.,]\d{1,2})?            # plain digits
    )
    (?P<trail>\s?\)|\s?-|\s?\+)?    # close paren / trailing sign
    (?:\s?(?P<marker>CR|DR|C|D)\b)?  # credit/debit marker
    (?![\w/])                       # not glued to what follows
    """ % (CURRENCY_SYMBOLS, CURRENCY_SYMBOLS),
    re.VERBOSE | re.IGNORECASE,
)


class Amount(NamedTuple):
    """A parsed money value."""

    value: Decimal
    #: True when the *text* said negative (minus, parentheses, DR marker).
    explicit_negative: bool
    #: True when the text said positive (CR marker or leading +).
    explicit_positive: bool
    #: The original substring, kept for the audit report.
    raw: str


def _strip_decorations(text: str) -> tuple[str, bool, bool]:
    """Remove symbols/markers, returning (digits_and_seps, neg, pos)."""
    s = text.strip()
    neg = False
    pos = False

    # Accounting parentheses: (123.45)
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()

    # Trailing CR/DR style markers.
    m = re.search(r"([A-Za-z]{1,6})\s*$", s)
    if m:
        marker = m.group(1).upper()
        if marker in CREDIT_MARKERS:
            pos = True
            s = s[: m.start()].strip()
        elif marker in DEBIT_MARKERS:
            neg = True
            s = s[: m.start()].strip()

    # Currency symbols, anywhere.
    s = re.sub(r"[%s]" % re.escape(CURRENCY_SYMBOLS), "", s).strip()

    # Leading / trailing bare signs.
    if s.startswith("-") or s.startswith("−"):  # ASCII and unicode minus
        neg = True
        s = s[1:].strip()
    elif s.startswith("+"):
        pos = True
        s = s[1:].strip()
    if s.endswith("-"):
        neg = True
        s = s[:-1].strip()
    elif s.endswith("+"):
        pos = True
        s = s[:-1].strip()

    return s, neg, pos


def _normalise_separators(s: str) -> str:
    """Collapse thousands separators and settle on '.' as the decimal point.

    The rule is "the *last* separator that leaves 1-2 trailing digits is the
    decimal point"; everything else is a grouping separator.  ``1.234,56`` and
    ``1,234.56`` both come out as ``1234.56``.  ``1.234`` (no 2-digit tail
    after a dot used as a grouper) is ambiguous — we treat a lone separator
    followed by exactly three digits as a thousands separator, which is the
    safer reading for statement amounts.
    """
    s = s.replace(" ", "").replace(" ", "").replace("'", "")
    if not s:
        return s

    last_dot = s.rfind(".")
    last_comma = s.rfind(",")
    dec_pos = max(last_dot, last_comma)

    if dec_pos == -1:
        return s

    tail = s[dec_pos + 1 :]
    if len(tail) == 3 and s.count(".") + s.count(",") == 1:
        # Exactly one separator with three digits after it -> grouping.
        return s.replace(".", "").replace(",", "")
    if len(tail) not in (1, 2):
        # Not a plausible decimal tail; treat every separator as grouping.
        return re.sub(r"[.,]", "", s)

    whole = re.sub(r"[.,]", "", s[:dec_pos])
    return f"{whole}.{tail}"


def parse_amount(text: str) -> Optional[Amount]:
    """Parse one money string.  Returns ``None`` if it is not a number."""
    if text is None:
        return None
    raw = str(text)
    body, neg, pos = _strip_decorations(raw)
    body = _normalise_separators(body)
    if not body or not re.fullmatch(r"\d+(?:\.\d+)?", body):
        return None
    try:
        value = Decimal(body)
    except InvalidOperation:
        return None
    if neg:
        value = -value
    return Amount(value=value, explicit_negative=neg, explicit_positive=pos, raw=raw.strip())


def find_amounts(text: str) -> list[tuple[int, int, Amount]]:
    """Find every money-looking token in a line.

    Returns ``(start, end, Amount)`` triples in reading order so the caller can
    both use the values *and* cut them out of the description.
    """
    out: list[tuple[int, int, Amount]] = []
    for m in MONEY_TOKEN_RE.finditer(text):
        amt = parse_amount(m.group(0))
        if amt is not None:
            out.append((m.start(), m.end(), amt))
    return out


def to_float(value: Decimal) -> float:
    """Store as a real number, rounded to cents to kill float noise."""
    return float(round(value, 2))
