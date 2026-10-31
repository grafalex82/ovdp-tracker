"""Describe purchases and their shared issue calendars for portfolio pages."""

from datetime import date

from .db import Database
from .money import operation_amount, parse_money


def _describe_lot(database: Database, lot: dict, today: date) -> dict:
    # Read the shared calendar in chronological order, keeping same-day event kinds separate.
    schedule = sorted(
        database.list_scheduled_payments(lot["issue_id"]),
        key=lambda event: (event["planned_date"], event["kind"] != "coupon", event["id"]),
    )
    bank = database.get_bank(lot["bank_id"])

    # Count only completed sales and actual redemptions through the requested local date.
    day = today.isoformat()
    sold = sum(sale["quantity"] for sale in database.list_sales(lot["id"]) if sale["sale_date"] <= day)
    redemptions = {event["id"] for event in schedule if event["kind"] == "redemption"}
    redeemed = any(
        actual["scheduled_payment_id"] in redemptions and actual["actual_date"] <= day
        for actual in database.list_actual_payments(lot["id"])
    )
    remaining = 0 if redeemed else lot["quantity"] - sold

    # Add presentation values without changing any stored purchase or issue data.
    return {
        **lot, "bank_name": bank["name"], "schedule": schedule, "remaining_quantity": remaining,
        "closed": remaining == 0,
        "purchase_total": str(operation_amount(parse_money(lot["purchase_price"]), lot["quantity"])),
    }


def list_lot_groups(database: Database, *, closed: bool = False, today: date | None = None) -> list[dict]:
    """Return purchases grouped by ISIN for the active or closed list as of today, or the local date for None."""

    # Describe each purchase and select its current active or closed state.
    today = today if today is not None else date.today()
    groups = {}
    for lot in database.list_lots():
        purchase = _describe_lot(database, lot, today)
        if purchase["closed"] != closed:
            continue
        group = groups.setdefault(lot["isin"], {"isin": lot["isin"], "currency": lot["currency"], "lots": []})
        group["lots"].append(purchase)

    # Keep issue groups and the purchases within them in a stable, predictable order.
    for group in groups.values():
        group["lots"].sort(key=lambda purchase: (purchase["purchase_date"], purchase["id"]))
    return [groups[isin] for isin in sorted(groups)]


def get_lot_details(database: Database, lot_id: int, *, today: date | None = None) -> dict | None:
    """Return a purchase with its issue calendar and current state, or None when lot_id is unknown."""

    # Look up the purchase without inventing a record for an unknown identifier.
    lot = database.get_lot(lot_id)
    if lot is None:
        return None

    # Use the same date and description rules as the portfolio list.
    return _describe_lot(database, lot, today if today is not None else date.today())
