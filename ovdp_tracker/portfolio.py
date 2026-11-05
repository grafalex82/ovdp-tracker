"""Describe ISIN positions, purchases and their shared calendars for portfolio pages."""

from datetime import date

from .db import Database
from .money import operation_amount, parse_money


def _describe_lot(database: Database, lot: dict, today: date, schedule: list[dict] | None = None) -> dict:
    # Read the shared calendar in chronological order, keeping same-day event kinds separate.
    schedule = sorted(
        database.list_scheduled_payments(lot["issue_id"]) if schedule is None else schedule,
        key=lambda event: (event["planned_date"], event["kind"] != "coupon", event["id"]),
    )
    bank = database.get_bank(lot["bank_id"])
    sales = database.list_sales(lot["id"])
    actuals = database.list_actual_payments(lot["id"])

    # Count only completed sales and actual redemptions through the requested local date.
    day = today.isoformat()
    sold = sum(sale["quantity"] for sale in sales if sale["sale_date"] <= day)
    redemptions = {event["id"] for event in schedule if event["kind"] == "redemption"}
    redeemed = any(
        actual["scheduled_payment_id"] in redemptions and actual["actual_date"] <= day
        for actual in actuals
    )
    remaining = 0 if redeemed else lot["quantity"] - sold

    # Add presentation values without changing any stored purchase or issue data.
    payments = _describe_payments(lot, schedule, sales, actuals, today)
    return {
        **lot, "bank_name": bank["name"], "schedule": schedule, "remaining_quantity": remaining,
        "closed": remaining == 0, "payments": payments,
        "has_overdue_payments": any(payment["status"] == "overdue" for payment in payments),
        "purchase_total": str(operation_amount(parse_money(lot["purchase_price"]), lot["quantity"])),
        "sales": [
            {**sale, "total": str(operation_amount(parse_money(sale["sale_price"]), sale["quantity"]))}
            for sale in sorted(sales, key=lambda item: (item["sale_date"], item["id"]))
        ],
    }


def _describe_payments(
    lot: dict, schedule: list[dict], sales: list[dict], actuals: list[dict], today: date
) -> list[dict]:
    # Index this purchase's confirmations and locate the actual end of ownership.
    confirmations = {actual["scheduled_payment_id"]: actual for actual in actuals}
    redemption_ids = {event["id"] for event in schedule if event["kind"] == "redemption"}
    redeemed_on = min(
        (actual["actual_date"] for actual in actuals if actual["scheduled_payment_id"] in redemption_ids), default=None
    )
    payments = []

    # Apply purchase, coupon, sale and redemption order within each calendar day.
    for event in schedule:
        if event["planned_date"] < lot["purchase_date"]:
            continue
        actual = confirmations.get(event["id"])
        if event["kind"] == "coupon" and redeemed_on and event["planned_date"] > redeemed_on and actual is None:
            continue
        sold = sum(
            sale["quantity"] for sale in sales
            if sale["sale_date"] < event["planned_date"]
            or (event["kind"] == "redemption" and sale["sale_date"] == event["planned_date"])
        )
        quantity = lot["quantity"] - sold
        if quantity > 0 or actual is not None:
            if actual is not None:
                status = "confirmed"
            elif parse_money(event["amount"]) == 0:
                status = "zero"
            elif event["planned_date"] < today.isoformat():
                status = "overdue"
            else:
                status = "pending"
            payments.append({
                **event, "quantity": quantity, "actual": actual, "status": status,
                "planned_amount": str(operation_amount(parse_money(event["amount"]), quantity)),
            })

    # Preserve each event's plan alongside its independent confirmation.
    return payments


def _describe_position(database: Database, issue: dict, lots: list[dict], today: date) -> dict:
    # Describe all purchases, including closed purchases inside a still-active position.
    schedule = database.list_scheduled_payments(issue["id"])
    purchases = [_describe_lot(database, lot, today, schedule) for lot in lots]
    purchases.sort(key=lambda purchase: (purchase["purchase_date"], purchase["id"]))
    remaining = sum(purchase["remaining_quantity"] for purchase in purchases)

    # Select the first positive future coupon still due to at least one active purchase.
    coupons = [
        payment for purchase in purchases if not purchase["closed"] for payment in purchase["payments"]
        if payment["kind"] == "coupon" and payment["planned_date"] >= today.isoformat()
        and parse_money(payment["planned_amount"]) > 0 and payment["actual"] is None
    ]
    next_coupon = min(coupons, key=lambda event: event["planned_date"], default=None)
    remaining_coupon_count = len({payment["planned_date"] for payment in coupons})

    # Keep shared issue fields and the full calendar separate from purchase-specific values.
    return {
        **issue, "lots": purchases, "remaining_quantity": remaining, "closed": remaining == 0,
        "has_overdue_payments": any(purchase["has_overdue_payments"] for purchase in purchases),
        "schedule": purchases[0]["schedule"] if purchases else [], "next_coupon": next_coupon,
        "remaining_coupon_count": remaining_coupon_count, "calendar": _describe_calendar(purchases),
    }


def _describe_calendar(purchases: list[dict]) -> list[dict]:
    # Retain the complete shared calendar, including events with no entitled purchases.
    groups = {
        event["id"]: {**event, "quantity": 0, "purchases": []}
        for event in (purchases[0]["schedule"] if purchases else [])
    }

    # Aggregate historical entitlements without removing confirmed or subsequently closed purchases.
    for purchase in purchases:
        for payment in purchase["payments"]:
            group = groups[payment["id"]]
            group["quantity"] += payment["quantity"]
            group["purchases"].append({
                "lot_id": purchase["id"], "quantity": payment["quantity"],
                "planned_amount": payment["planned_amount"], "actual": payment["actual"],
                "status": payment["status"],
            })

    # Multiply the shared unit amount by the full count, keeping exact cents and separate actual receipts.
    for group in groups.values():
        group["planned_amount"] = str(operation_amount(parse_money(group["amount"]), group["quantity"]))
    return list(groups.values())


def list_positions(
    database: Database, *, currency: str = "UAH", closed: bool = False, today: date | None = None
) -> list[dict]:
    """Return ISIN positions in currency and the requested closed state as of today, using the local date for None."""

    # Group purchases by their shared issue before applying the position state filter.
    today = today if today is not None else date.today()
    groups = {}
    for lot in database.list_lots():
        if lot["currency"] == currency:
            groups.setdefault(lot["issue_id"], []).append(lot)

    # Omit orphan issues and select closed positions only when every purchase is closed.
    positions = [
        _describe_position(database, database.get_issue(issue_id), lots, today) for issue_id, lots in groups.items()
    ]
    return sorted((position for position in positions if position["closed"] == closed), key=lambda item: item["isin"])


def get_position_details(database: Database, isin: str, *, today: date | None = None) -> dict | None:
    """Return the shared issue and all its purchases as of today, or None when isin has no portfolio position."""

    # Resolve the shared issue independently of currency filters or earlier navigation.
    issue = database.get_issue_by_isin(isin)
    if issue is None:
        return None
    lots = [lot for lot in database.list_lots() if lot["issue_id"] == issue["id"]]
    if not lots:
        return None

    # Use the same aggregation rules for direct details and portfolio rows.
    return _describe_position(database, issue, lots, today if today is not None else date.today())


def get_lot_details(database: Database, lot_id: int, *, today: date | None = None) -> dict | None:
    """Return a purchase with its issue calendar and current state, or None when lot_id is unknown."""

    # Look up the purchase without inventing a record for an unknown identifier.
    lot = database.get_lot(lot_id)
    if lot is None:
        return None

    # Use the same date and description rules as the portfolio list.
    return _describe_lot(database, lot, today if today is not None else date.today())


def list_payment_events(
    database: Database, *, currency: str, start: date | None = None, end: date | None = None,
    today: date | None = None,
) -> dict[str, list[dict]]:
    """Return purchase payments in currency grouped by status and filtered by inclusive dates.

    Filter confirmed receipts by actual date and other payments by planned date. Include closed purchases
    and use the local date when today is None; start and end may be omitted for open boundaries.
    """

    # Reuse purchase-specific entitlement and status rules for active and closed purchases alike.
    today = today if today is not None else date.today()
    groups = {"overdue": [], "upcoming": [], "history": []}
    for lot in database.list_lots():
        if lot["currency"] != currency:
            continue
        purchase = _describe_lot(database, lot, today)
        for payment in purchase["payments"]:
            actual = payment["actual"]
            group = "history" if actual else "overdue" if payment["status"] == "overdue" else "upcoming"
            if payment["status"] == "zero" and payment["planned_date"] < today.isoformat():
                continue
            day = actual["actual_date"] if actual else payment["planned_date"]
            if (start and day < start.isoformat()) or (end and day > end.isoformat()):
                continue
            groups[group].append({
                **payment, "isin": lot["isin"], "lot_id": lot["id"], "bank_name": purchase["bank_name"],
                "display_date": day,
            })

    # Sort individual purchase/event pairs deterministically without merging same-day events.
    for events in groups.values():
        events.sort(key=lambda item: (item["display_date"], item["isin"], item["lot_id"],
                                     item["kind"] != "coupon", item["id"]))
    return groups
