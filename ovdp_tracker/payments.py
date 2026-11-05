"""Confirm purchase-specific receipts without changing the shared issue calendar."""

from datetime import date

from .db import Database
from .money import parse_money
from .portfolio import get_lot_details
from .purchases import parse_date
from .sales import SaleValidationError, check_history


class PaymentValidationError(ValueError):
    """Carry receipt field errors without discarding the submitted values."""

    def __init__(self, errors: dict):
        """Keep errors by field name for displaying invalid receipt inputs beside their controls."""
        super().__init__("The payment contains invalid fields.")
        self.errors = errors


def _validate_payment(values: dict, purchase_date: str, today: date) -> dict:
    # Require a real local receipt date within the purchase's completed history.
    errors = {}
    try:
        day = parse_date(values.get("actual_date", ""))
    except (TypeError, ValueError):
        errors["actual_date"] = "Enter a valid date in YYYY-MM-DD format."
    else:
        if day.isoformat() < purchase_date:
            errors["actual_date"] = "Actual payment date cannot precede the purchase."
        elif day > today:
            errors["actual_date"] = "Actual payment date cannot be in the future."

    # Preserve exact positive totals and report all invalid fields together.
    try:
        amount = parse_money(values.get("amount", ""))
        if amount <= 0:
            raise ValueError("Amount must be positive.")
    except ValueError as error:
        errors["amount"] = str(error)
    if errors:
        raise PaymentValidationError(errors)
    return {"actual_date": day.isoformat(), "amount": str(amount)}


def confirm_payment(database: Database, lot_id: int, event_id: int, values: dict, today: date) -> dict:
    """Confirm lot_id's entitled event_id using receipt values and the local today.

    Return the saved receipt, or the original receipt on a repeated submission without
    changing its date or total. Check entitlement and the resulting history atomically.
    """

    # Resolve entitlement under the write lock, including historical coupons of closed purchases.
    with database.transaction():
        lot = get_lot_details(database, lot_id, today=today)
        if lot is None:
            raise PaymentValidationError({"form": "Purchase not found."})
        payment = next((item for item in lot["payments"] if item["id"] == event_id), None)
        if payment is None:
            raise PaymentValidationError({"form": "Payment is not due for this purchase."})
        if payment["actual"] is not None:
            return payment["actual"]
        if payment["quantity"] <= 0 or parse_money(payment["planned_amount"]) <= 0:
            raise PaymentValidationError({"form": "Payment does not require confirmation."})

        # Save only this receipt and roll back if its actual redemption conflicts with any sale.
        parsed = _validate_payment(values, lot["purchase_date"], today)
        actual = database.add_actual_payment(lot_id=lot_id, scheduled_payment_id=event_id, **parsed)
        try:
            check_history(database, lot, database.list_sales(lot_id))
        except SaleValidationError as error:
            raise PaymentValidationError({"form": str(error)}) from error
        return actual
