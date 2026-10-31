"""Validate purchase inputs and save a lot with its payment schedule atomically."""

from datetime import date
import re

from .db import Database
from .money import parse_money

CURRENCIES = ("UAH", "USD", "EUR")


class PurchaseValidationError(ValueError):
    """Carry field-specific errors without discarding the submitted purchase values."""

    def __init__(self, errors: dict):
        """Keep the supplied field errors for rendering beside the purchase inputs."""
        super().__init__("The purchase contains invalid fields.")
        self.errors = errors


def _parse_date(value: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Enter a valid date in YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Enter a valid date in YYYY-MM-DD format.") from error


def validate_purchase(values: dict, today: date) -> dict:
    """Parse submitted strings against today's date; return purchase data or raise field errors."""

    # Require the issue and bank names without imposing an unrequested ISIN format.
    errors = {}
    purchase = {}
    for field in ("isin", "bank_name"):
        purchase[field] = values.get(field, "").strip()
        if not purchase[field]:
            errors[field] = "This field is required."

    # Validate currency and a positive quantity that fits the storage integer type.
    purchase["currency"] = values.get("currency", "")
    if purchase["currency"] not in CURRENCIES:
        errors["currency"] = "Currency must be UAH, USD, or EUR."
    quantity = values.get("quantity", "").strip()
    if not re.fullmatch(r"[0-9]+", quantity) or len(quantity.lstrip("0")) > 19:
        errors["quantity"] = "Quantity must be a positive integer within the supported range."
    else:
        purchase["quantity"] = int(quantity.lstrip("0") or "0")
        if not 0 < purchase["quantity"] <= 9223372036854775807:
            errors["quantity"] = "Quantity must be a positive integer within the supported range."

    # Parse exact monetary strings and enforce the domain-specific sign constraints.
    for field in ("nominal", "purchase_price", "coupon_amount"):
        try:
            amount = parse_money(values.get(field, ""))
            if amount < 0 or (field != "coupon_amount" and amount == 0):
                message = "Amount must be nonnegative." if field == "coupon_amount" else "Amount must be positive."
                raise ValueError(message)
            purchase[field] = str(amount)
        except ValueError as error:
            errors[field] = str(error)

    # Accept historical dates and infer maturity only while its default remains automatic.
    dates = {}
    for field in ("purchase_date", "maturity_date"):
        raw = values.get(field, "").strip()
        if field == "maturity_date" and not raw and values.get("maturity_manual") != "1":
            continue
        try:
            dates[field] = _parse_date(raw)
        except ValueError as error:
            errors[field] = str(error)
    coupons = []
    for raw in values.get("coupon_dates", "").splitlines():
        if not raw.strip():
            continue
        try:
            coupons.append(_parse_date(raw.strip()))
        except ValueError as error:
            errors["coupon_dates"] = str(error)
    if "maturity_date" not in dates and "maturity_date" not in errors:
        if coupons:
            dates["maturity_date"] = max(coupons)
        else:
            errors["maturity_date"] = "Enter a maturity date for a purchase without coupons."

    # Check the chronological boundaries and uniqueness without changing the user's schedule.
    purchase_date = dates.get("purchase_date")
    maturity_date = dates.get("maturity_date")
    if purchase_date is not None and purchase_date > today:
        errors["purchase_date"] = "Purchase date cannot be in the future; planned purchases are not supported."
    if purchase_date is not None and maturity_date is not None and maturity_date < purchase_date:
        errors["maturity_date"] = "Maturity date cannot be before the purchase date."
    if len(coupons) != len(set(coupons)):
        errors["coupon_dates"] = "Coupon dates must be unique."
    if any((purchase_date and day < purchase_date) or (maturity_date and day > maturity_date) for day in coupons):
        errors["coupon_dates"] = "Coupon dates must be between purchase and maturity, inclusive."
    if errors:
        raise PurchaseValidationError(errors)

    # Return canonical storage strings and a sorted schedule for atomic persistence.
    purchase.update({field: day.isoformat() for field, day in dates.items()})
    purchase["coupon_dates"] = [day.isoformat() for day in sorted(coupons)]
    return purchase


def create_purchase(database: Database, values: dict, today: date | None = None) -> dict:
    """Validate input strings and save their bank, lot, and schedule together; return the new lot."""

    # Finish validation before creating any bank or purchase records.
    purchase = validate_purchase(values, today if today is not None else date.today())
    bank_name = purchase.pop("bank_name")
    coupon_dates = purchase.pop("coupon_dates")

    # Commit the bank, purchase, and separate coupon/redemption events as one unit.
    with database.transaction():
        bank = database.add_bank(bank_name)
        lot = database.add_lot(**purchase, bank_id=bank["id"])
        for planned_date in coupon_dates:
            database.add_scheduled_payment(lot_id=lot["id"], kind="coupon", planned_date=planned_date)
        database.add_scheduled_payment(lot_id=lot["id"], kind="redemption", planned_date=lot["maturity_date"])
    return lot
