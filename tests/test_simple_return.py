"""Check exact purchase cost-days and simple annual return over the full holding period."""

from datetime import date
from decimal import Decimal, localcontext

import pytest

from ovdp_tracker.analytics import get_purchase_cash_flows, simple_annual_return


def _purchase(database, bank, *, price="1000", quantity=1, maturity="2027-01-01", redemption="1200"):
    # Save one purchase and its optional redemption using the same issue maturity date.
    issue = database.add_issue(isin="UA4000000002", nominal="1000", currency="UAH", maturity_date=maturity)
    lot = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2025-01-01",
                           purchase_price=price, quantity=quantity)
    event = None
    if redemption is not None:
        event = database.add_scheduled_payment(issue_id=issue["id"], kind="redemption", planned_date=maturity,
                                               amount=redemption)
    return lot, event


def test_two_year_zero_coupon_return(database, bank):
    # A 200 gain on a 1000 purchase held for exactly two years produces 10% per year.
    lot, _ = _purchase(database, bank)
    purchase = get_purchase_cash_flows(database, lot["id"], today=date(2025, 1, 1))
    result = simple_annual_return(purchase)
    assert result == {"rate": Decimal("0.1"), "reason": None, "cost_days": Decimal("730000.00")}


@pytest.mark.parametrize("price", ["1000", "1000.01"])
def test_partial_sale_uses_each_parts_exact_cost_and_term(database, bank, price):
    # Sell four bonds after one year and keep six for redemption after two years.
    lot, _ = _purchase(database, bank, price=price, quantity=10)
    database.add_sale(lot_id=lot["id"], sale_date="2026-01-01", sale_price="1100", quantity=4)
    purchase = get_purchase_cash_flows(database, lot["id"], today=date(2026, 1, 1))
    sale, redemption = [flow for flow in purchase["flows"] if flow["kind"] in ("sale", "redemption")]

    # Check cent-precise costs and the exact cost-days, including under a limited Decimal context.
    if price == "1000.01":
        assert sale["cost_basis"] == Decimal("4000.04")
        assert redemption["cost_basis"] == Decimal("6000.06")
        expected_cost_days = Decimal("5840058.40")
        expected_profit = Decimal("1599.90")
    else:
        expected_cost_days = Decimal("5840000.00")
        expected_profit = Decimal("1600.00")
    with localcontext() as context:
        context.prec = 3
        result = simple_annual_return(purchase)
    assert purchase["expected_profit"] == expected_profit
    assert result["cost_days"] == expected_cost_days
    assert result["rate"] == pytest.approx(expected_profit * 365 / expected_cost_days, abs=Decimal("1e-25"))
    if price == "1000":
        assert result["rate"] == Decimal("0.10")


def test_coupon_date_does_not_change_holding_denominator(database, bank):
    # Add a coupon while leaving the purchase and redemption dates unchanged.
    lot, _ = _purchase(database, bank)
    database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date="2026-01-01", amount="100")
    result = simple_annual_return(get_purchase_cash_flows(database, lot["id"]))
    assert result["cost_days"] == Decimal("730000.00")
    assert result["rate"] == Decimal("0.15")


def test_actual_redemption_date_replaces_planned_holding_term(database, bank):
    # Confirm redemption one year early, so the actual date ends the investment term.
    lot, event = _purchase(database, bank)
    database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=event["id"], actual_date="2026-01-01",
                                amount="1200")
    result = simple_annual_return(get_purchase_cash_flows(database, lot["id"], today=date(2026, 1, 1)))
    assert result == {"rate": Decimal("0.2"), "reason": None, "cost_days": Decimal("365000.00")}


def test_full_sale_does_not_require_a_redemption(database, bank):
    # Sell the whole purchase at a loss after one year without a redemption event.
    lot, _ = _purchase(database, bank, redemption=None)
    database.add_sale(lot_id=lot["id"], sale_date="2026-01-01", sale_price="900", quantity=1)
    result = simple_annual_return(get_purchase_cash_flows(database, lot["id"], today=date(2026, 1, 1)))
    assert result == {"rate": Decimal("-0.1"), "reason": None, "cost_days": Decimal("365000.00")}


def test_no_redemption_and_zero_term_have_explicit_reasons(database, bank):
    # Report the missing end of an unsold holding and retain its independent profit data.
    lot, _ = _purchase(database, bank, redemption=None)
    purchase = get_purchase_cash_flows(database, lot["id"], today=date(2025, 1, 1))
    assert simple_annual_return(purchase) == {"rate": None, "reason": "missing_redemption", "cost_days": None}

    # A same-day sale has a zero term rather than a fabricated zero-percent yield.
    database.add_sale(lot_id=lot["id"], sale_date="2025-01-01", sale_price="1100", quantity=1)
    purchase = get_purchase_cash_flows(database, lot["id"], today=date(2025, 1, 1))
    assert purchase["expected_profit"] == Decimal("100.00")
    assert simple_annual_return(purchase) == {"rate": None, "reason": "zero_duration", "cost_days": Decimal("0.00")}


def test_zero_profit_has_a_zero_rate(database, bank):
    # Close a zero-profit purchase on a later date, producing a genuine zero rate.
    lot, _ = _purchase(database, bank, redemption="1000")
    purchase = get_purchase_cash_flows(database, lot["id"])
    assert simple_annual_return(purchase)["rate"] == Decimal("0")


def test_sale_after_unconfirmed_maturity_does_not_duplicate_cost_days(database, bank):
    # Record a sale after an unconfirmed maturity; only the unsold part reaches the planned redemption.
    lot, _ = _purchase(database, bank, price="1000.01", quantity=10)
    database.add_sale(lot_id=lot["id"], sale_date="2027-02-01", sale_price="1100", quantity=4)
    purchase = get_purchase_cash_flows(database, lot["id"], today=date(2027, 2, 1))
    result = simple_annual_return(purchase)
    assert result["cost_days"] == Decimal("4000.04") * 761 + Decimal("6000.06") * 730
