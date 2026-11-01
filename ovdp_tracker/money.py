"""Parse cent-precision monetary inputs and calculate exact totals without binary floats."""

from decimal import Decimal, InvalidOperation, localcontext
import re

_DECIMAL_INPUT = re.compile(r"[+-]?(?:[0-9]+(?:[.,][0-9]*)?|[.,][0-9]+)")
_CENT = Decimal("0.01")


def parse_money(value: str) -> Decimal:
    """Return an exact Decimal from a string with a dot or comma and at most two decimal places."""
    # Accept decimal notation only, avoiding implicit conversion from binary floats.
    if not isinstance(value, str) or not _DECIMAL_INPUT.fullmatch(value.strip()):
        raise ValueError("Money must be a finite decimal string with a dot or comma separator.")

    # Convert the original digits directly, preserving their precision and trailing zeros.
    try:
        amount = Decimal(value.strip().replace(",", "."))
    except InvalidOperation as error:
        raise ValueError("Money must be a finite decimal string.") from error
    if not amount.is_finite():
        raise ValueError("Money must be finite.")

    # Reject excess decimal places, including trailing zeros, instead of rounding the user's input.
    if amount.as_tuple().exponent < -2:
        raise ValueError("Money must have at most two decimal places.")
    return amount


def operation_amount(unit_amount: Decimal, quantity: int = 1) -> Decimal:
    """Return a cent-precision unit_amount times a nonnegative integer quantity, exactly to cents.

    Use quantity=1 for a payment total. Negative amounts are allowed for outgoing cash flows.
    """
    # Reject values that would introduce binary floats, nonfinite totals, or fractional quantities.
    if not isinstance(unit_amount, Decimal) or not unit_amount.is_finite():
        raise ValueError("Unit amount must be a finite Decimal.")
    if unit_amount.as_tuple().exponent < -2:
        raise ValueError("Unit amount must have at most two decimal places.")
    if type(quantity) is not int or quantity < 0:
        raise ValueError("Quantity must be a nonnegative integer.")

    # Keep multiplication and cent formatting exact, regardless of ambient precision.
    quantity_digits = len(str(quantity))
    with localcontext() as context:
        context.prec = max(
            len(unit_amount.as_tuple().digits) + quantity_digits, unit_amount.adjusted() + quantity_digits + 3
        )
        return (unit_amount * quantity).quantize(_CENT)
