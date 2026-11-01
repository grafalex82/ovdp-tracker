"""Verify cent-precision input validation and exact monetary operation totals."""

from decimal import Decimal, ROUND_DOWN, localcontext

import pytest

from ovdp_tracker.money import operation_amount, parse_money


@pytest.mark.parametrize("value, expected", [
    ("1000,01", "1000.01"),
    ("  +1000.10  ", "1000.10"),
    ("1000.1", "1000.1"),
    ("0", "0"),
    ("-0,01", "-0.01"),
    (".25", "0.25"),
    ("123456789012345678901234567890.12", "123456789012345678901234567890.12"),
])
def test_parse_money_preserves_cent_precision_values(value, expected):
    assert str(parse_money(value)) == expected


@pytest.mark.parametrize("value", [
    "", " ", "abc", "NaN", "sNaN", "Infinity", "-Infinity", "inf", "∞",
    "1,000.005", "1.2.3", "1,2,3", "1 000", "1_000", "1e3", "100 UAH", "--1",
    1000.005, 1000, True, None, Decimal("1000.005"),
])
def test_parse_money_rejects_invalid_and_non_string_inputs(value):
    with pytest.raises(ValueError, match="finite decimal string"):
        parse_money(value)


@pytest.mark.parametrize("value", ["1000,005", "80.123", "1000.010", "0.000", "-0,001"])
def test_parse_money_rejects_more_than_two_decimal_places(value):
    with pytest.raises(ValueError, match="at most two decimal places"):
        parse_money(value)


@pytest.mark.parametrize("unit_amount, quantity, expected", [
    ("1000,01", 3, "3000.03"),
    ("1050", 3, "3150.00"),
    ("0.00", 1, "0.00"),
    ("0.01", 3, "0.03"),
    ("-0.01", 1, "-0.01"),
    ("1.1", 1, "1.10"),
    ("80.12", 0, "0.00"),
    ("123456789012345678901234567890.01", 3, "370370367037037036703703703670.03"),
])
def test_operation_amount_multiplies_exactly_to_cents(unit_amount, quantity, expected):
    assert str(operation_amount(parse_money(unit_amount), quantity)) == expected


def test_operation_amount_does_not_depend_on_or_modify_the_ambient_decimal_context():
    # Simulate an unrelated calculation changing the ambient precision and rounding mode.
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_DOWN

        # Keep the monetary product exact in an isolated context.
        result = operation_amount(parse_money("123456789.01"), 3)
        assert str(result) == "370370367.03"
        assert context.prec == 6
        assert context.rounding == ROUND_DOWN


def test_aggregates_sum_exact_cent_amounts():
    assert sum(operation_amount(parse_money("0.01")) for _ in range(3)) == Decimal("0.03")


@pytest.mark.parametrize("value", [0.1, "0.1", Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_operation_amount_requires_a_finite_decimal(value):
    with pytest.raises(ValueError, match="finite Decimal"):
        operation_amount(value)


@pytest.mark.parametrize("value", [Decimal("1000.005"), Decimal("80.123"), Decimal("1.000")])
def test_operation_amount_rejects_excess_precision_without_rounding(value):
    with pytest.raises(ValueError, match="at most two decimal places"):
        operation_amount(value)


@pytest.mark.parametrize("quantity", [-1, 1.5, Decimal("3"), "3", True])
def test_operation_amount_requires_a_nonnegative_integer_quantity(quantity):
    with pytest.raises(ValueError, match="nonnegative integer"):
        operation_amount(Decimal("1000"), quantity)
