"""Build exact purchase cash flows and distinguish realized from full-horizon profit."""

from datetime import date
from decimal import Decimal

from .db import Database
from .money import operation_amount, parse_money
from .portfolio import get_lot_details


def _sum_amounts(amounts) -> Decimal:
    # Sum integer cents so the result does not depend on the ambient Decimal precision.
    cents = 0
    for amount in amounts:
        sign, digits, exponent = amount.as_tuple()
        value = int("".join(map(str, digits))) * 10 ** (exponent + 2)
        cents += -value if sign else value

    # Construct the decimal directly without rounding in the active context.
    sign = "-" if cents < 0 else ""
    return Decimal(f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}")


def get_purchase_cash_flows(database: Database, lot_id: int, *, today: date | None = None) -> dict | None:
    """Return exact full-horizon flows and profits for lot_id, or None when the purchase is absent.

    Use the local date when today is None. Realized profit includes completed sales and confirmed
    receipts through today; expected profit includes all saved sales and entitled payment forecasts.
    Each flow retains its purchase, event and disposal cost basis for later aggregation.
    """

    # Reuse the portfolio's purchase-specific entitlement and same-day ordering rules.
    today = today if today is not None else date.today()
    lot = get_lot_details(database, lot_id, today=today)
    if lot is None:
        return None
    price = parse_money(lot["purchase_price"])
    purchase_total = operation_amount(price, lot["quantity"])
    flows = [{
        "lot_id": lot_id, "date": lot["purchase_date"], "kind": "purchase", "status": "actual",
        "amount": purchase_total.copy_negate(), "quantity": lot["quantity"], "cost_basis": Decimal("0.00"),
        "event_id": None,
    }]

    # Record every sale with its exact original cost, including saved future sales in the full horizon.
    for sale in lot["sales"]:
        flows.append({
            "lot_id": lot_id, "date": sale["sale_date"], "kind": "sale", "status": "actual",
            "amount": operation_amount(parse_money(sale["sale_price"]), sale["quantity"]),
            "quantity": sale["quantity"], "cost_basis": operation_amount(price, sale["quantity"]),
            "event_id": None, "sale_id": sale["id"],
        })

    # Replace each purchase/event forecast with its own confirmation when present.
    for payment in lot["payments"]:
        actual = payment["actual"]
        amount = operation_amount(parse_money(actual["amount"] if actual else payment["planned_amount"]))
        if amount == 0:
            continue
        quantity = payment["quantity"]
        if actual and payment["kind"] == "redemption":
            quantity = lot["quantity"] - sum(
                sale["quantity"] for sale in lot["sales"] if sale["sale_date"] <= actual["actual_date"]
            )
        flows.append({
            "lot_id": lot_id, "date": actual["actual_date"] if actual else payment["planned_date"],
            "kind": payment["kind"], "status": "actual" if actual else "expected",
            "amount": amount,
            "quantity": quantity,
            "cost_basis": operation_amount(price, quantity if payment["kind"] == "redemption" else 0),
            "event_id": payment["id"],
        })

    # Calculate realized results from completed receipts and disposals, and forecast the whole purchase.
    flows.sort(key=lambda flow: (flow["date"], {"purchase": 0, "coupon": 1, "sale": 2, "redemption": 3}[flow["kind"]]))
    completed = [flow for flow in flows if flow["status"] == "actual" and flow["date"] <= today.isoformat()
                 and flow["kind"] != "purchase"]
    realized_profit = _sum_amounts(
        [flow["amount"] for flow in completed] + [flow["cost_basis"].copy_negate() for flow in completed]
    )
    return {
        "lot_id": lot_id, "currency": lot["currency"], "flows": flows,
        "realized_profit": realized_profit, "expected_profit": _sum_amounts(flow["amount"] for flow in flows),
        "remaining_quantity": lot["remaining_quantity"],
        "remaining_cost": operation_amount(price, lot["remaining_quantity"]),
    }
