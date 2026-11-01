"""Validate and record sales against the complete history of one purchase."""

from datetime import date
import re

from .db import Database
from .money import parse_money


class SaleValidationError(ValueError):
    """A sale cannot be recorded with the supplied fields or purchase history."""


def create_sale(database: Database, lot_id: int, values: dict, today: date) -> dict:
    """Validate and atomically record a sale of lot_id using form values and the local date."""

    # Validate the submitted operation before acquiring the database write lock.
    try:
        day = date.fromisoformat(values.get("sale_date", ""))
    except ValueError as error:
        raise SaleValidationError("Enter a valid sale date.") from error
    if day > today:
        raise SaleValidationError("Sale date cannot be in the future.")
    quantity_text = values.get("quantity", "").strip()
    if not re.fullmatch(r"[0-9]+", quantity_text) or int(quantity_text) <= 0:
        raise SaleValidationError("Quantity must be a positive integer.")
    quantity = int(quantity_text)
    try:
        price = parse_money(values.get("sale_price", ""))
        if price <= 0:
            raise ValueError("Sale price must be positive.")
    except ValueError as error:
        raise SaleValidationError("Enter a positive sale price with at most two decimal places.") from error

    # Check the entire chronological history under the same lock used for insertion.
    with database.transaction():
        lot = database.get_lot(lot_id)
        if lot is None:
            raise SaleValidationError("Purchase not found.")
        if day.isoformat() < lot["purchase_date"]:
            raise SaleValidationError("Sale date cannot precede the purchase.")
        redemption_ids = {
            event["id"] for event in database.list_scheduled_payments(lot["issue_id"])
            if event["kind"] == "redemption"
        }
        redemption_dates = [
            actual["actual_date"] for actual in database.list_actual_payments(lot_id)
            if actual["scheduled_payment_id"] in redemption_ids
        ]
        if redemption_dates and day.isoformat() > min(redemption_dates):
            raise SaleValidationError("Cannot sell bonds after their actual redemption.")
        sales = database.list_sales(lot_id) + [{"sale_date": day.isoformat(), "quantity": quantity, "id": 0}]
        remaining = lot["quantity"]
        for sale in sorted(sales, key=lambda item: (item["sale_date"], item["id"])):
            remaining -= sale["quantity"]
            if remaining < 0:
                raise SaleValidationError("Sale exceeds the available quantity in the purchase history.")

        # Persist only after every existing and new operation has passed validation.
        return database.add_sale(lot_id=lot_id, sale_date=day.isoformat(), sale_price=str(price), quantity=quantity)
