"""Turning a PDF into positioned text lines.

``pdfplumber.Page.extract_text()`` is fine for prose but loses the one thing a
bank statement depends on: *which column* a number sat in.  A withdrawal and a
deposit look identical once the layout is flattened to a string.

So we rebuild lines ourselves from word boxes:

* group words into rows by their vertical centre (with a tolerance, because
  the same visual row is rarely pixel-identical);
* keep each word's x0/x1 so the parser can ask "was this number under the
  Debit heading or the Credit heading?";
* render a plain-text version of the row for regex work.

We also sniff the column headings (Date / Description / Debit / Credit /
Balance …) and record their x-ranges, which is what makes debit/credit sign
detection reliable rather than guesswork.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import pdfplumber

# Heading words we recognise, mapped to a canonical column role.
HEADING_WORDS: list[tuple[str, str]] = [
    (r"date", "date"),
    (r"value\s*date", "date"),
    (r"posting\s*date|post\s*date|transaction\s*date|txn\s*date", "date"),
    (r"description|details|particulars|narrative|transaction|memo|payee|reference", "description"),
    (r"debit|withdrawal[s]?|payments?\s*out|paid\s*out|money\s*out|charges?", "debit"),
    (r"credit|deposit[s]?|payments?\s*in|paid\s*in|money\s*in", "credit"),
    (r"amount|value", "amount"),
    (r"balance|bal\b|running\s*balance|closing\s*balance", "balance"),
]


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float

    @property
    def mid(self) -> float:
        return (self.x0 + self.x1) / 2.0


@dataclass
class Line:
    """One visual row of a page."""

    page_no: int
    line_no: int
    top: float
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def spans(self) -> list[tuple[int, int, Word]]:
        """Character offsets of each word inside :attr:`text`."""
        out: list[tuple[int, int, Word]] = []
        pos = 0
        for i, w in enumerate(self.words):
            if i:
                pos += 1  # the joining space
            out.append((pos, pos + len(w.text), w))
            pos += len(w.text)
        return out

    def words_in_range(self, x0: float, x1: float) -> list[Word]:
        return [w for w in self.words if x0 <= w.mid <= x1]


@dataclass
class ColumnMap:
    """x-ranges for the columns named by a heading row."""

    ranges: dict[str, tuple[float, float]] = field(default_factory=dict)
    source_line: Optional[str] = None

    def role_at(self, x: float) -> Optional[str]:
        """Which column does x fall in?  Nearest centre wins."""
        best, best_dist = None, None
        for role, (a, b) in self.ranges.items():
            if a <= x <= b:
                return role
            dist = a - x if x < a else x - b
            if best_dist is None or dist < best_dist:
                best, best_dist = role, dist
        # Only claim a column if we are reasonably close to one.
        if best_dist is not None and best_dist <= 25:
            return best
        return None

    @property
    def has_debit_credit(self) -> bool:
        return "debit" in self.ranges and "credit" in self.ranges

    def __bool__(self) -> bool:
        return bool(self.ranges)


def _group_words_into_lines(words: list[dict], tolerance: float = 3.0) -> list[list[Word]]:
    """Bucket words by vertical position."""
    ws = [Word(w["text"], float(w["x0"]), float(w["x1"]), float(w["top"])) for w in words]
    ws.sort(key=lambda w: (round(w.top, 1), w.x0))
    rows: list[list[Word]] = []
    for w in ws:
        if rows and abs(rows[-1][0].top - w.top) <= tolerance:
            rows[-1].append(w)
        else:
            rows.append([w])
    for r in rows:
        r.sort(key=lambda w: w.x0)
    return rows


def detect_columns(line: Line) -> ColumnMap:
    """Read a heading row into a :class:`ColumnMap`, or return an empty one."""
    text = line.text.lower()
    hits: list[tuple[str, float, float]] = []
    used: set[str] = set()

    # Match headings against the concatenated text, then map back to words.
    for start, end, w in line.spans():
        token = w.text.lower().strip(":|")
        for pattern, role in HEADING_WORDS:
            if re.fullmatch(pattern, token, re.I):
                hits.append((role, w.x0, w.x1))
                break

    if len({r for r, _, _ in hits}) < 2:
        return ColumnMap()
    # A heading row should be mostly headings, not a transaction that happens
    # to contain the word "balance".
    if len(hits) < max(2, len(line.words) // 3):
        return ColumnMap()

    # Merge repeated roles (e.g. "Money" "Out" -> two words, one role) and turn
    # each into a range that reaches halfway to its neighbours.
    merged: dict[str, tuple[float, float]] = {}
    for role, x0, x1 in hits:
        if role in merged:
            a, b = merged[role]
            merged[role] = (min(a, x0), max(b, x1))
        else:
            merged[role] = (x0, x1)

    ordered = sorted(merged.items(), key=lambda kv: kv[1][0])
    ranges: dict[str, tuple[float, float]] = {}
    for i, (role, (x0, x1)) in enumerate(ordered):
        left = x0 - 30 if i == 0 else (ordered[i - 1][1][1] + x0) / 2.0
        right = x1 + 60 if i == len(ordered) - 1 else (x1 + ordered[i + 1][1][0]) / 2.0
        ranges[role] = (left, right)
    return ColumnMap(ranges=ranges, source_line=line.text)


@dataclass
class ExtractedPage:
    page_no: int
    lines: list[Line]
    raw_text: str


def extract_pages(path: str, password: Optional[str] = None) -> list[ExtractedPage]:
    """Read every page of ``path`` into positioned lines.

    Raises ``ValueError`` if the PDF carries no extractable text at all, which
    almost always means it is a scan and needs OCR first.
    """
    pages: list[ExtractedPage] = []
    with pdfplumber.open(path, password=password) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(
                keep_blank_chars=False,
                use_text_flow=False,
                extra_attrs=[],
            )
            rows = _group_words_into_lines(words)
            lines = [
                Line(page_no=i, line_no=n, top=row[0].top, words=row)
                for n, row in enumerate(rows, start=1)
            ]
            pages.append(
                ExtractedPage(page_no=i, lines=lines, raw_text=page.extract_text() or "")
            )
    if not any(p.lines for p in pages):
        raise ValueError(
            "No text found in this PDF — it is most likely a scanned image. "
            "Run it through OCR first (e.g. `ocrmypdf in.pdf out.pdf`) and retry."
        )
    return pages
