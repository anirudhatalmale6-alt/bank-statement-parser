"""Date parsing for statement lines.

Two jobs:

1. Recognise a date at the *start* of a line (that is what marks a new
   transaction, as opposed to a wrapped description line).
2. Turn it into an ISO ``YYYY-MM-DD`` string so SQLite can sort and range-query
   it, while flagging anything that looks wrong.

The day/month ambiguity of ``03/04/2025`` is resolved per *document*, not per
line: we scan every date in the file first, and if any one of them has a
first component above 12 the whole document is day-first (and vice versa).
Only if the document is genuinely ambiguous do we fall back to the ``--dayfirst``
CLI setting.  That stops a statement flipping convention halfway through.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Iterable, NamedTuple, Optional

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))

# Ordered most-specific first.  Each pattern is anchored by the caller.
DATE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 2025-04-03  /  2025/04/03
    ("ymd", re.compile(r"(?P<y>\d{4})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})")),
    # 03/04/2025  03-04-25  03.04.2025
    ("num", re.compile(r"(?P<a>\d{1,2})[-/.](?P<b>\d{1,2})[-/.](?P<y>\d{2,4})")),
    # 03 Apr 2025 / 03-Apr-2025 / 3 April 25
    ("dmy_name", re.compile(r"(?P<d>\d{1,2})[\s\-/]?(?P<mon>%s)\.?[\s\-/,]*(?P<y>\d{2,4})?" % _MONTH_ALT, re.I)),
    # Apr 03, 2025 / Apr 3
    ("mdy_name", re.compile(r"(?P<mon>%s)\.?[\s\-/]+(?P<d>\d{1,2})(?:[\s,]+(?P<y>\d{2,4}))?" % _MONTH_ALT, re.I)),
    # 03/04  (no year at all — very common on UK statements)
    ("dm_noyear", re.compile(r"(?P<a>\d{1,2})[-/.](?P<b>\d{1,2})(?![-/.\d])")),
]


class DateHit(NamedTuple):
    """A date found in text, before day/month disambiguation."""

    kind: str
    start: int
    end: int
    raw: str
    groups: dict


def find_leading_date(text: str) -> Optional[DateHit]:
    """Match a date at the very start of ``text`` (leading spaces allowed)."""
    stripped = text.lstrip()
    offset = len(text) - len(stripped)
    for kind, pat in DATE_PATTERNS:
        m = pat.match(stripped)
        if m:
            return DateHit(
                kind=kind,
                start=offset + m.start(),
                end=offset + m.end(),
                raw=m.group(0),
                groups={k: v for k, v in m.groupdict().items() if v is not None},
            )
    return None


def infer_dayfirst(hits: Iterable[DateHit], default: bool = True) -> bool:
    """Decide day-first vs month-first for a whole document.

    A single unambiguous date (first component > 12, or second > 12) settles it
    for the entire file.  Ties fall back to ``default``.
    """
    day_first_votes = 0
    month_first_votes = 0
    for h in hits:
        if h.kind not in ("num", "dm_noyear"):
            continue
        a = int(h.groups["a"])
        b = int(h.groups["b"])
        if a > 12 >= b:
            day_first_votes += 1
        elif b > 12 >= a:
            month_first_votes += 1
    if day_first_votes and not month_first_votes:
        return True
    if month_first_votes and not day_first_votes:
        return False
    return default


def _expand_year(y: Optional[str], fallback_year: Optional[int]) -> Optional[int]:
    if y is None:
        return fallback_year
    y = int(y)
    if y >= 1000:
        return y
    # Two-digit years: 70-99 -> 1900s, 00-69 -> 2000s.
    return 1900 + y if y >= 70 else 2000 + y


def to_iso(
    hit: DateHit,
    dayfirst: bool,
    fallback_year: Optional[int] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Convert a :class:`DateHit` to ``('YYYY-MM-DD', None)``.

    On failure returns ``(None, reason)`` so the caller can log why.
    """
    g = hit.groups
    try:
        if hit.kind == "ymd":
            y, m, d = int(g["y"]), int(g["m"]), int(g["d"])
        elif hit.kind in ("num", "dm_noyear"):
            a, b = int(g["a"]), int(g["b"])
            year = _expand_year(g.get("y"), fallback_year)
            if year is None:
                return None, "no year available for %r" % hit.raw
            if a > 12 and b > 12:
                return None, "neither component can be a month in %r" % hit.raw
            if a > 12:
                d, m = a, b
            elif b > 12:
                m, d = a, b
            else:
                d, m = (a, b) if dayfirst else (b, a)
            y = year
        elif hit.kind == "dmy_name":
            d = int(g["d"])
            m = MONTHS[g["mon"].lower()]
            y = _expand_year(g.get("y"), fallback_year)
            if y is None:
                return None, "no year available for %r" % hit.raw
        elif hit.kind == "mdy_name":
            d = int(g["d"])
            m = MONTHS[g["mon"].lower()]
            y = _expand_year(g.get("y"), fallback_year)
            if y is None:
                return None, "no year available for %r" % hit.raw
        else:  # pragma: no cover - defensive
            return None, "unknown date kind %r" % hit.kind
        return _dt.date(y, m, d).isoformat(), None
    except ValueError as exc:
        return None, "%s in %r" % (exc, hit.raw)


def find_statement_year(pages_text: Iterable[str]) -> Optional[int]:
    """Best-effort year for statements whose transaction lines omit it.

    Looks for a 4-digit year in the header area (e.g. "Statement period
    1 March 2025 to 31 March 2025") and takes the most frequent one.
    """
    counts: dict[int, int] = {}
    for text in pages_text:
        for m in re.finditer(r"\b(19|20)\d{2}\b", text):
            y = int(m.group(0))
            counts[y] = counts.get(y, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def is_suspicious(iso: str, today: Optional[_dt.date] = None) -> Optional[str]:
    """Flag dates that parsed fine but are almost certainly wrong."""
    today = today or _dt.date.today()
    d = _dt.date.fromisoformat(iso)
    if d > today + _dt.timedelta(days=1):
        return "date is in the future"
    if d.year < 1980:
        return "date is implausibly old"
    return None
