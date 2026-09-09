"""Date recognition, including the DD/MM vs MM/DD trap."""

import pytest

from bankstmt.dates import find_leading_date, infer_dayfirst, to_iso


def iso(text, dayfirst=True, year=None):
    hit = find_leading_date(text)
    assert hit is not None, f"no date found in {text!r}"
    value, err = to_iso(hit, dayfirst, year)
    assert err is None, err
    return value


@pytest.mark.parametrize("text,dayfirst,expected", [
    ("03/04/2025 SOMETHING", True, "2025-04-03"),
    ("03/04/2025 SOMETHING", False, "2025-03-04"),
    ("2025-04-03 SOMETHING", True, "2025-04-03"),
    ("03 Apr 2025 SOMETHING", True, "2025-04-03"),
    ("03-Apr-2025 SOMETHING", True, "2025-04-03"),
    ("Apr 3, 2025 SOMETHING", False, "2025-04-03"),
    ("31/12/99 SOMETHING", True, "1999-12-31"),
    ("01/01/25 SOMETHING", True, "2025-01-01"),
])
def test_leading_dates(text, dayfirst, expected):
    assert iso(text, dayfirst) == expected


def test_unambiguous_component_overrides_the_setting():
    """25/03 can only be a day-first date, whatever the flag says."""
    assert iso("25/03/2025 X", dayfirst=False) == "2025-03-25"


def test_document_wide_convention_is_inferred_from_one_clear_date():
    hits = [find_leading_date(t) for t in
            ["01/02/2025 A", "13/02/2025 B", "05/02/2025 C"]]
    assert infer_dayfirst(hits, default=False) is True

    hits = [find_leading_date(t) for t in
            ["02/01/2025 A", "02/13/2025 B", "02/05/2025 C"]]
    assert infer_dayfirst(hits, default=True) is False


def test_missing_year_uses_the_statement_year():
    assert iso("03/04 SOMETHING", dayfirst=True, year=2025) == "2025-04-03"


def test_impossible_date_is_reported_not_guessed():
    hit = find_leading_date("31/02/2025 X")
    value, err = to_iso(hit, dayfirst=True)
    assert value is None
    assert "day is out of range" in err


def test_a_description_is_not_a_date():
    assert find_leading_date("CARD PAYMENT TO TESCO 3421") is None
