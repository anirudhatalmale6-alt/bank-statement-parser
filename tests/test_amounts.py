"""Money parsing — the part that silently corrupts totals when it is wrong."""

from decimal import Decimal

import pytest

from bankstmt.amounts import parse_amount, to_float

CASES = [
    ("1,234.56", Decimal("1234.56"), False),
    ("$1,234.56", Decimal("1234.56"), False),
    ("£1,234.56", Decimal("1234.56"), False),
    ("€1.234,56", Decimal("1234.56"), False),
    ("1 234,56", Decimal("1234.56"), False),
    ("-1,234.56", Decimal("-1234.56"), True),
    ("(1,234.56)", Decimal("-1234.56"), True),
    ("($1,234.56)", Decimal("-1234.56"), True),
    ("1,234.56-", Decimal("-1234.56"), True),
    ("1,234.56 DR", Decimal("-1234.56"), True),
    ("1,234.56CR", Decimal("1234.56"), False),
    ("0.00", Decimal("0.00"), False),
    ("5", Decimal("5"), False),
    ("1,000", Decimal("1000"), False),
    ("12.5", Decimal("12.5"), False),
]


@pytest.mark.parametrize("text,expected,negative", CASES)
def test_parse_amount(text, expected, negative):
    amt = parse_amount(text)
    assert amt is not None, f"{text!r} should parse"
    assert amt.value == expected
    assert amt.explicit_negative is negative


@pytest.mark.parametrize("text", ["", "  ", "REF", "N/A", "-", "abc", "12/03/2025"])
def test_non_amounts_are_rejected(text):
    assert parse_amount(text) is None


def test_thousands_separator_is_not_a_decimal_point():
    """`1.234` on a European statement means one thousand, not 1.234."""
    assert parse_amount("1.234").value == Decimal("1234")
    assert parse_amount("1,234").value == Decimal("1234")


def test_stored_as_real_number_rounded_to_cents():
    v = to_float(Decimal("1234.555"))
    assert isinstance(v, float)
    assert v == 1234.56


def test_no_float_drift_in_a_sum():
    """Amounts are summed as Decimal, so 0.1 + 0.2 behaves."""
    total = parse_amount("0.10").value + parse_amount("0.20").value
    assert total == Decimal("0.30")
