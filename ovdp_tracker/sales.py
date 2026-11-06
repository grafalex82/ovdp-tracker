"""Validate and record sales against the complete history of one purchase."""

from datetime import date

from .db import Database
from .money import parse_money
from .quantity import parse_quantity


class SaleValidationError(ValueError):
    """A sale cannot be recorded with the supplied fields or purchase history."""


def create_sale(database: Database, lot_id: int, values: dict, today: date) -> dict:
    """Validate and atomically record a sale of lot_id using form values and the local date."""

    # Parse independent fields before acquiring the database write lock.
    sale = validate_sale(values, today)

    # Validate the complete history and insert the sale in one transaction.
    with database.transaction():
        lot = database.get_lot(lot_id)
        if lot is None:
            raise SaleValidationError("Purchase not found.")
        check_history(database, lot, database.list_sales(lot_id) + [{**sale, "id": 0}])
        return database.add_sale(lot_id=lot_id, **sale)


def validate_sale(values: dict, today: date) -> dict:
    """Return validated sale fields from values, rejecting sale dates after the supplied local today."""

    # Validate the submitted operation before acquiring the database write lock.
    try:
        day = date.fromisoformat(values.get("sale_date", ""))
    except ValueError as error:
        raise SaleValidationError("Enter a valid sale date.") from error
    if day > today:
        raise SaleValidationError("Sale date cannot be in the future.")
    try:
        quantity = parse_quantity(values.get("quantity", ""))
    except ValueError as error:
        raise SaleValidationError(str(error)) from error
    try:
        price = parse_money(values.get("sale_price", ""))
        if price <= 0:
            raise ValueError("Sale price must be positive.")
    except ValueError as error:
        raise SaleValidationError("Enter a positive sale price with at most two decimal places.") from error

    # Return values accepted by the storage API.
    return {"sale_date": day.isoformat(), "sale_price": str(price), "quantity": quantity}


def check_history(database: Database, lot: dict, sales: list[dict]) -> None:
    """Reject sales or confirmed payments inconsistent with the proposed lot and its history."""

    # Check each sale against the purchase, actual redemption and chronological balance.
    events = database.list_scheduled_payments(lot["issue_id"])
    actuals = database.list_actual_payments(lot["id"])
    redemption_ids = {event["id"] for event in events if event["kind"] == "redemption"}
    redemption_dates = [a["actual_date"] for a in actuals if a["scheduled_payment_id"] in redemption_ids]
    remaining = lot["quantity"]
    for sale in sorted(sales, key=lambda item: (item["sale_date"], item["id"])):
        if sale["sale_date"] < lot["purchase_date"]:
            raise SaleValidationError("Sale date cannot precede the purchase.")
        if redemption_dates and sale["sale_date"] > min(redemption_dates):
            raise SaleValidationError("Cannot sell bonds after their actual redemption.")
        remaining -= sale["quantity"]
        if remaining < 0:
            raise SaleValidationError("Sale exceeds the available quantity in the purchase history.")

    # Preserve confirmed events only when the purchase owned bonds at the event date.
    by_id = {event["id"]: event for event in events}
    for actual in actuals:
        event = by_id[actual["scheduled_payment_id"]]
        day = event["planned_date"]
        sold = sum(s["quantity"] for s in sales if s["sale_date"] < day
                   or (event["kind"] == "redemption" and s["sale_date"] == day))
        if day < lot["purchase_date"] or actual["actual_date"] < lot["purchase_date"] or sold >= lot["quantity"]:
            raise SaleValidationError("The change conflicts with a confirmed payment.")
