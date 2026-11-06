"""Build exact purchase cash flows and distinguish realized from full-horizon profit."""

from calendar import monthrange
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
import math

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


def _discounted_values(terms: list[tuple[float, float]], log_rate: float) -> tuple[float, float]:
    # Factor out the largest discount exponent to avoid overflow without changing signs or roots.
    anchor = terms[-1][0] if log_rate < 0 else terms[0][0]
    weighted = [(years, amount * math.exp(-(years - anchor) * log_rate)) for years, amount in terms]
    scale = math.fsum(abs(amount) for _, amount in weighted)

    # Normalize both the residual and derivative against the discounted absolute cash flows.
    value = math.fsum(amount for _, amount in weighted) / scale
    derivative = math.fsum(-years * amount for years, amount in weighted) / scale
    return value, derivative


def _bisect(terms: list[tuple[float, float]], left: float, right: float, component: int) -> float | None:
    # Refine a bracket while retaining a sign change and limiting the number of iterations.
    left_value = _discounted_values(terms, left)[component]
    for _ in range(100):
        middle = (left + right) / 2
        if middle == left or middle == right:
            return middle
        value = _discounted_values(terms, middle)[component]
        if value == 0:
            return middle
        if (left_value < 0) != (value < 0):
            right = middle
        else:
            left, left_value = middle, value

    # A bracket that cannot be refined reliably is not an acceptable result.
    return None


def _xirr_result(terms: list[tuple[float, float]], root: float | None) -> dict:
    # Reject absent, unrepresentable or numerically inaccurate candidates.
    if root is None:
        return {"rate": None, "reason": "numerical_failure"}
    try:
        rate = math.expm1(root)
    except OverflowError:
        return {"rate": None, "reason": "numerical_failure"}
    if (not math.isfinite(rate) or rate <= -1 or math.ulp(rate) > 1e-7
            or abs(_discounted_values(terms, root)[0]) > 1e-12):
        return {"rate": None, "reason": "numerical_failure"}

    # Return an unrounded fractional annual rate for presentation or aggregation.
    return {"rate": rate, "reason": None}


def xirr(flows: list[dict]) -> dict:
    """Calculate annual XIRR for purchase cash-flow dictionaries using actual days and a 365-day year.

    Aggregate Decimal amounts in one currency by ISO date and ignore zero totals. Return the rate as a fraction in
    ``rate`` with ``reason`` set to None; otherwise return None and a reason code: ``missing_sign``,
    ``zero_duration``, ``multiple_roots`` or ``numerical_failure``. Search is bounded to representable
    floating-point rates and cannot prove uniqueness for every possible nonconventional cash-flow series.
    """

    # Identify the zero-duration case before combining cash flows that share one date.
    nonzero = [flow for flow in flows if flow["amount"] != 0]
    has_positive = any(flow["amount"] > 0 for flow in nonzero)
    has_negative = any(flow["amount"] < 0 for flow in nonzero)
    if has_positive and has_negative and len({flow["date"] for flow in nonzero}) == 1:
        return {"rate": None, "reason": "zero_duration"}
    grouped = {}
    for flow in nonzero:
        grouped.setdefault(date.fromisoformat(flow["date"]), []).append(flow["amount"])
    totals = sorted((day, _sum_amounts(amounts)) for day, amounts in grouped.items())
    totals = [(day, amount) for day, amount in totals if amount != 0]
    if not any(amount > 0 for _, amount in totals) or not any(amount < 0 for _, amount in totals):
        return {"rate": None, "reason": "missing_sign"}

    # Scale Decimal amounts before converting to floats and retain the earliest date as the origin.
    origin = totals[0][0]
    maximum = max(amount.copy_abs() for _, amount in totals)
    with localcontext() as context:
        context.prec = 40
        terms = [((day - origin).days / 365, float(amount / maximum)) for day, amount in totals]
    if any(amount == 0 for _, amount in terms):
        return {"rate": None, "reason": "numerical_failure"}

    # A single chronological sign change permits at most one root, so bracket it directly.
    lower, upper = math.log(2 ** -53), math.log(float.fromhex("0x1.fffffffffffffp+1023"))
    sign_changes = sum((first[1] < 0) != (second[1] < 0) for first, second in zip(terms, terms[1:]))
    if sign_changes == 1:
        left_value = _discounted_values(terms, lower)[0]
        right_value = _discounted_values(terms, upper)[0]
        if left_value == 0 or right_value == 0 or (left_value < 0) == (right_value < 0):
            return {"rate": None, "reason": "numerical_failure"}
        return _xirr_result(terms, _bisect(terms, lower, upper, 0))

    # Scan for multiple roots and extrema, including intervals with two roots.
    step = 0.05
    points = [lower]
    while points[-1] + step < upper:
        points.append(points[-1] + step)
    points.append(upper)
    previous_point = points[0]
    previous_value, previous_derivative = _discounted_values(terms, previous_point)
    previous_derivative_point = previous_point
    roots = []
    uncertain = False
    for point in points[1:]:
        value, derivative = _discounted_values(terms, point)
        if previous_value == 0:
            roots.append(previous_point)
        elif value != 0 and (previous_value < 0) != (value < 0):
            root = _bisect(terms, previous_point, point, 0)
            if root is None:
                return {"rate": None, "reason": "numerical_failure"}
            roots.append(root)

        # Inspect each detected stationary point so two roots within one scan interval are not missed.
        if derivative != 0 and previous_derivative != 0 and (derivative < 0) != (previous_derivative < 0):
            extremum = _bisect(terms, previous_derivative_point, point, 1)
            if extremum is None:
                return {"rate": None, "reason": "numerical_failure"}
            extremum_value = _discounted_values(terms, extremum)[0]
            if previous_derivative_point == previous_point:
                for a, b in ((previous_point, extremum), (extremum, point)):
                    fa, fb = _discounted_values(terms, a)[0], _discounted_values(terms, b)[0]
                    if fa != 0 and fb != 0 and (fa < 0) != (fb < 0):
                        root = _bisect(terms, a, b, 0)
                        if root is None:
                            return {"rate": None, "reason": "numerical_failure"}
                        roots.append(root)
            if abs(extremum_value) < 1e-14:
                uncertain = True
        if derivative != 0:
            previous_derivative, previous_derivative_point = derivative, point
        previous_point, previous_value = point, value
    if previous_value == 0:
        roots.append(previous_point)

    # Deduplicate brackets and reject cases with multiple roots or an uncertain stationary point.
    roots.sort()
    unique = []
    for root in roots:
        if not unique or abs(root - unique[-1]) > 1e-10:
            unique.append(root)
    if len(unique) > 1:
        return {"rate": None, "reason": "multiple_roots"}
    if not unique or uncertain:
        return {"rate": None, "reason": "numerical_failure"}
    return _xirr_result(terms, unique[0])


def _simple_return_result(profit: Decimal, cost_days: Decimal) -> dict:
    # Keep the numerator exact and perform the only division with a controlled Decimal precision.
    if cost_days == 0:
        return {"rate": None, "reason": "zero_duration", "cost_days": cost_days}
    with localcontext() as context:
        context.prec = max(40, len(profit.as_tuple().digits) + 4, len(cost_days.as_tuple().digits) + 4)
        rate = profit * 365 / cost_days
    return {"rate": rate, "reason": None, "cost_days": cost_days}


def simple_annual_return(purchase: dict) -> dict:
    """Calculate a purchase's full-horizon simple annual return from its cash flows and expected profit.

    Return a fractional Decimal rate with at least 40 significant digits and exact cost-days for combining purchases.
    If the term is zero or an unsold part has no redemption, return None and a ``zero_duration`` or
    ``missing_redemption`` reason. Use actual redemption dates and otherwise the planned date.
    """

    # Resolve the original purchase, its sales and the redemption of any remaining bonds.
    flows = purchase["flows"]
    purchase_flow = next(flow for flow in flows if flow["kind"] == "purchase")
    origin = date.fromisoformat(purchase_flow["date"])
    sales = [flow for flow in flows if flow["kind"] == "sale"]
    redemption = next((flow for flow in flows if flow["kind"] == "redemption"), None)
    remaining = purchase_flow["quantity"] - sum(flow["quantity"] for flow in sales)
    if remaining > 0 and redemption is None:
        return {"rate": None, "reason": "missing_redemption", "cost_days": None}

    # Multiply each sold part's exact purchase cost by its own holding time in days.
    cost_days = [operation_amount(flow["cost_basis"], (date.fromisoformat(flow["date"]) - origin).days)
                 for flow in sales]
    if remaining > 0:
        remaining_cost = _sum_amounts(
            [purchase_flow["amount"].copy_negate()] + [flow["cost_basis"].copy_negate() for flow in sales]
        )
        days = (date.fromisoformat(redemption["date"]) - origin).days
        cost_days.append(operation_amount(remaining_cost, days))
    total_cost_days = _sum_amounts(cost_days)

    # Calculate the rate from full-horizon profit and the exact sum of cost-days.
    return _simple_return_result(purchase["expected_profit"], total_cost_days)


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


def summarize_receipts(purchases: list[dict], *, today: date) -> dict:
    """Summarize completed profit and separate confirmed and expected payments through the full horizon."""

    # Collect purchase-specific flows without adding a second copy of the shared issue calendar.
    flows = [flow for purchase in purchases for flow in purchase["flows"]]
    completed = [flow for flow in flows if flow["status"] == "actual" and flow["date"] <= today.isoformat()]
    expected = [flow for flow in flows if flow["status"] == "expected"]

    # Keep future-dated confirmations out of received amounts and forecasts until their actual date.
    return {
        "realized_profit": _sum_amounts(purchase["realized_profit"] for purchase in purchases),
        "received_coupons": _sum_amounts(flow["amount"] for flow in completed if flow["kind"] == "coupon"),
        "expected_coupons": _sum_amounts(flow["amount"] for flow in expected if flow["kind"] == "coupon"),
        "expected_redemptions": _sum_amounts(flow["amount"] for flow in expected if flow["kind"] == "redemption"),
    }


def summarize_operations(database: Database, *, currency: str, start: date | None, end: date | None,
                         today: date) -> dict:
    """Return exact operation totals and realized profit in currency for an inclusive date range."""

    # Select actual purchase-specific flows by their operation dates, including closed purchases.
    purchases = [get_purchase_cash_flows(database, lot["id"], today=today) for lot in database.list_lots()
                 if lot["currency"] == currency]
    flows = [flow for purchase in purchases for flow in purchase["flows"] if flow["status"] == "actual"
             and (start is None or flow["date"] >= start.isoformat())
             and (end is None or flow["date"] <= end.isoformat())]

    # Keep purchases positive and subtract the original cost of completed disposals from period profit.
    totals = {kind: _sum_amounts(flow["amount"] for flow in flows if flow["kind"] == kind)
              for kind in ("purchase", "coupon", "redemption", "sale")}
    totals["purchase"] = totals["purchase"].copy_abs()
    completed = [flow for flow in flows if flow["kind"] != "purchase" and flow["date"] <= today.isoformat()]
    totals["realized_profit"] = _sum_amounts(
        [flow["amount"] for flow in completed] + [flow["cost_basis"].copy_negate() for flow in completed]
    )
    return totals


def monthly_payment_totals(groups: dict[str, list[dict]], *, start: date | None, end: date | None,
                           today: date) -> list[dict]:
    """Return exact expected and actual payment totals per month for filtered purchase payment groups.

    Include empty months. With both bounds omitted, cover the current month through the last expected redemption;
    an individual open bound extends to the earliest or latest selected payment or the other bound.
    """

    # Resolve finite display bounds while preserving the date filtering already applied to the events.
    events = [event for group in groups.values() for event in group]
    current = today.year * 12 + today.month - 1
    month_number = lambda day: int(day[:4]) * 12 + int(day[5:7]) - 1
    months = [month_number(event["display_date"]) for event in events]
    if start is None and end is None:
        redemptions = [month_number(event["display_date"]) for event in events
                       if event["kind"] == "redemption" and event["actual"] is None]
        first, last = current, max([current] + redemptions)
    else:
        start_month = start.year * 12 + start.month - 1 if start else None
        end_month = end.year * 12 + end.month - 1 if end else None
        first = start_month if start_month is not None else min([current, end_month] + months)
        last = end_month if end_month is not None else max([current, start_month] + months)

    # Collect each confirmation or remaining expectation exactly once in its applicable month.
    buckets = {month: {key: [] for key in ("expected_coupon", "actual_coupon", "expected_redemption",
                                           "actual_redemption")} for month in range(first, last + 1)}
    for event in events:
        month = month_number(event["display_date"])
        if month not in buckets:
            continue
        actual = event["actual"]
        key = ("actual_" if actual else "expected_") + event["kind"]
        buckets[month][key].append(parse_money(actual["amount"] if actual else event["planned_amount"]))

    # Sum exact cents, retain empty months and derive each total from its two distinct payment kinds.
    result = []
    for month, amounts in buckets.items():
        totals = {key: _sum_amounts(values) for key, values in amounts.items()}
        totals["expected_total"] = _sum_amounts((totals["expected_coupon"], totals["expected_redemption"]))
        totals["actual_total"] = _sum_amounts((totals["actual_coupon"], totals["actual_redemption"]))
        year, month_index = divmod(month, 12)
        result.append({"month": f"{year:04d}-{month_index + 1:02d}", **totals})
    return result


def summarize_balances(lots: list[dict]) -> dict:
    """Return the quantity, exact cost and nominal value of the described purchases' current balances."""

    # Value each remaining portion using its own purchase price and the issue nominal.
    return {
        "quantity": sum(lot["remaining_quantity"] for lot in lots),
        "cost": _sum_amounts(operation_amount(parse_money(lot["purchase_price"]), lot["remaining_quantity"])
                             for lot in lots),
        "nominal": _sum_amounts(operation_amount(parse_money(lot["nominal"]), lot["remaining_quantity"])
                                for lot in lots),
    }


def calculate_allocations(positions: list[dict], *, currency: str) -> dict[str, dict]:
    """Return exact current costs and fractional portfolio shares for active positions in currency.

    Exclude closed positions and purchases through their current remaining quantities. A zero total cost yields
    None shares; otherwise each share is an exact Fraction without rounding the underlying monetary amounts.
    """

    # Keep only active positions in the selected currency and sum their remaining purchase costs exactly.
    costs = {position["isin"]: summarize_balances(position["lots"])["cost"] for position in positions
             if position["currency"] == currency and not position["closed"]}
    total = _sum_amounts(costs.values())

    # Represent each share as an exact ratio and leave a zero denominator explicitly undefined.
    return {isin: {"cost": cost, "share": Fraction(cost) / Fraction(total) if total else None}
            for isin, cost in costs.items()}


def _add_months(day: date, months: int) -> date:
    # Clamp the day to the end of the target calendar month when necessary.
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    month += 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def maturity_distribution(lots: list[dict], *, today: date) -> list[dict]:
    """Group described current balances by maturity relative to today using calendar-month boundaries."""

    # Set exclusive upper bounds so each boundary belongs to the next group.
    boundaries = [today, _add_months(today, 3), _add_months(today, 6), _add_months(today, 12)]
    groups = [[] for _ in range(5)]
    for lot in lots:
        if not lot["remaining_quantity"]:
            continue
        maturity = date.fromisoformat(lot["maturity_date"])
        index = next((index for index, boundary in enumerate(boundaries) if maturity < boundary), 4)
        groups[index].append(lot)

    # Return every group, including empty ones, with exact and independently calculated totals.
    return [summarize_balances(group) for group in groups]


def _combine_purchase_returns(purchases: list[dict], currency: str) -> dict:
    # Exclude closed purchases while preserving the complete history of each active purchase.
    purchases = [purchase for purchase in purchases if purchase["remaining_quantity"] > 0]
    if not purchases:
        return {
            "currency": currency, "purchase_ids": [], "flows": [], "expected_profit": Decimal("0.00"),
            "xirr": {"rate": None, "reason": "no_active_purchases"},
            "simple_return": {"rate": None, "reason": "no_active_purchases", "cost_days": None},
        }

    # Aggregate exact amounts by date without losing the original flows needed to identify a zero-length horizon.
    flows = [flow for purchase in purchases for flow in purchase["flows"]]
    grouped = {}
    for flow in flows:
        grouped.setdefault(flow["date"], []).append(flow["amount"])
    totals = [(day, _sum_amounts(amounts)) for day, amounts in sorted(grouped.items())]
    combined = [{"date": day, "amount": amount} for day, amount in totals if amount != 0]
    profit = _sum_amounts(purchase["expected_profit"] for purchase in purchases)

    # Sum profits and exact cost-days, without averaging the individual percentages.
    parts = [simple_annual_return(purchase) for purchase in purchases]
    if any(part["reason"] == "missing_redemption" for part in parts):
        simple = {"rate": None, "reason": "missing_redemption", "cost_days": None}
        annual_xirr = {"rate": None, "reason": "missing_redemption"}
    else:
        simple = _simple_return_result(profit, _sum_amounts(part["cost_days"] for part in parts))
        annual_xirr = xirr(flows)
    return {
        "currency": currency, "purchase_ids": [purchase["lot_id"] for purchase in purchases],
        "flows": combined, "expected_profit": profit, "xirr": annual_xirr, "simple_return": simple,
    }


def get_portfolio_returns(database: Database, *, currency: str, today: date | None = None) -> dict:
    """Return full-horizon expected returns of active purchases in currency as of today.

    Include complete purchase histories, combine cash flows by date and return fractional XIRR and simple rates.
    Exclude closed purchases; today defaults to the local date. An empty portfolio has no annual returns.
    """

    # Resolve purchase histories only in the requested currency using the common aggregation rules.
    today = today if today is not None else date.today()
    purchases = [get_purchase_cash_flows(database, lot["id"], today=today) for lot in database.list_lots()
                 if lot["currency"] == currency]
    return _combine_purchase_returns(purchases, currency)


def get_position_returns(database: Database, isin: str, *, today: date | None = None) -> dict | None:
    """Return expected returns for active purchases of isin, or None if it has no portfolio position.

    Use the shared issue currency and complete purchase histories as of today, defaulting to the local date.
    Report the reason for unavailable rates when the position has no active purchases or a calculation fails.
    """

    # Resolve the saved issue and its purchases without including other ISINs in the same currency.
    issue = database.get_issue_by_isin(isin)
    if issue is None:
        return None
    lots = [lot for lot in database.list_lots() if lot["issue_id"] == issue["id"]]
    if not lots:
        return None

    # Apply the same full-horizon aggregation and active-purchase rules as portfolio totals.
    today = today if today is not None else date.today()
    purchases = [get_purchase_cash_flows(database, lot["id"], today=today) for lot in lots]
    return {**_combine_purchase_returns(purchases, issue["currency"]), "isin": issue["isin"]}
