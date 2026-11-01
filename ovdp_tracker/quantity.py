"""Validate the number of bonds in an individual purchase or sale."""

import re

MAX_BOND_QUANTITY = 10_000


def parse_quantity(value: str) -> int:
    """Return a positive whole bond count from value within the supported operation limit."""

    # Reject malformed input and normalize leading zeros before converting to an integer.
    value = value.strip()
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError("Quantity must be a positive integer.")
    digits = value.lstrip("0") or "0"
    if len(digits) > len(str(MAX_BOND_QUANTITY)) or int(digits) > MAX_BOND_QUANTITY:
        raise ValueError(f"Quantity must not exceed {MAX_BOND_QUANTITY} bonds.")

    # Require a nonzero count for each purchase or sale.
    quantity = int(digits)
    if quantity == 0:
        raise ValueError("Quantity must be a positive integer.")
    return quantity
