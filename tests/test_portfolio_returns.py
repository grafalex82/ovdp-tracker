"""Verify expected portfolio returns from complete histories of active purchases."""

from datetime import date
from decimal import Decimal, localcontext
import math

import pytest

from ovdp_tracker.analytics import get_portfolio_returns


def _add_purchase(database, bank, *, isin, maturity, redemption, currency="UAH", price="1000", quantity=1):
    # Save an independent issue and purchase with an optional maturity payment.
    issue = database.add_issue(isin=isin, nominal="1000", currency=currency, maturity_date=maturity)
    lot = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2025-01-01",
                           purchase_price=price, quantity=quantity)
    if redemption is not None:
        database.add_scheduled_payment(issue_id=issue["id"], kind="redemption", planned_date=maturity,
                                       amount=redemption)
    return lot


def test_portfolio_uses_combined_cash_flows_and_weighted_terms_per_currency(database, bank):
    # Create two active UAH purchases with 10% for one year and 20% per year for two years.
    first = _add_purchase(database, bank, isin="UA4000000001", maturity="2026-01-01", redemption="1100")
    second = _add_purchase(database, bank, isin="UA4000000002", maturity="2027-01-01", redemption="1400")
    closed = _add_purchase(database, bank, isin="UA4000000003", maturity="2027-01-01", redemption="1300")
    coupon = database.add_scheduled_payment(issue_id=closed["issue_id"], kind="coupon", planned_date="2025-03-01",
                                            amount="100")
    database.add_sale(lot_id=closed["id"], sale_date="2025-04-01", sale_price="1050", quantity=1)
    usd = _add_purchase(database, bank, isin="UA4000000004", currency="USD", maturity="2026-01-01",
                        redemption="1200")

    # Combine same-day purchases before XIRR and use 500 / (1000 + 2000), not the 15% average.
    with localcontext() as context:
        context.prec = 3
        result = get_portfolio_returns(database, currency="UAH", today=date(2025, 5, 1))
    assert result["purchase_ids"] == [first["id"], second["id"]]
    assert result["flows"] == [
        {"date": "2025-01-01", "amount": Decimal("-2000.00")},
        {"date": "2026-01-01", "amount": Decimal("1100.00")},
        {"date": "2027-01-01", "amount": Decimal("1400.00")},
    ]
    assert result["expected_profit"] == Decimal("500.00")
    assert result["simple_return"]["cost_days"] == Decimal("1095000.00")
    assert result["simple_return"]["rate"] == pytest.approx(Decimal(1) / 6, abs=Decimal("1e-25"))
    assert result["xirr"]["reason"] is None
    assert result["xirr"]["rate"] == pytest.approx((1100 + math.sqrt(12410000)) / 4000 - 1, abs=1e-8)
    assert database.list_actual_payments(closed["id"]) == []
    assert coupon["planned_date"] == "2025-03-01"

    # The other currency retains its own purchase and results without mixing the UAH amounts.
    dollar = get_portfolio_returns(database, currency="USD", today=date(2025, 5, 1))
    assert dollar["purchase_ids"] == [usd["id"]]
    assert dollar["expected_profit"] == Decimal("200.00")
    assert dollar["simple_return"]["rate"] == Decimal("0.2")
    assert dollar["xirr"]["rate"] == pytest.approx(0.2, abs=1e-8)


def test_partially_sold_purchase_keeps_purchase_coupon_sale_and_forecast(database, bank):
    # Confirm a coupon and sell three bonds before a second coupon and final redemption.
    lot = _add_purchase(database, bank, isin="UA4000000001", maturity="2027-01-01", redemption="1200", quantity=10)
    coupon = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date="2025-07-01",
                                            amount="100")
    database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date="2026-07-01", amount="100")
    database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=coupon["id"], actual_date="2025-07-02",
                                amount="1000")
    database.add_sale(lot_id=lot["id"], sale_date="2026-01-01", sale_price="1100", quantity=3)

    # Include all historical and forecast flows for the purchase that still has seven bonds.
    result = get_portfolio_returns(database, currency="UAH", today=date(2026, 1, 1))
    assert result["purchase_ids"] == [lot["id"]]
    assert result["flows"] == [
        {"date": "2025-01-01", "amount": Decimal("-10000.00")},
        {"date": "2025-07-02", "amount": Decimal("1000.00")},
        {"date": "2026-01-01", "amount": Decimal("3300.00")},
        {"date": "2026-07-01", "amount": Decimal("700.00")},
        {"date": "2027-01-01", "amount": Decimal("8400.00")},
    ]
    assert result["expected_profit"] == Decimal("3400.00")
    assert result["simple_return"]["cost_days"] == Decimal("6205000.00")
    assert result["simple_return"]["rate"] == Decimal("0.2")
    rate = result["xirr"]["rate"]
    origin = date(2025, 1, 1)
    npv = sum(float(flow["amount"]) / (1 + rate) ** ((date.fromisoformat(flow["date"]) - origin).days / 365)
              for flow in result["flows"])
    assert abs(npv) < 1e-8


def test_empty_or_fully_closed_currency_has_no_fabricated_return(database, bank):
    # A sold-out purchase is excluded even if an earlier coupon is still unconfirmed.
    lot = _add_purchase(database, bank, isin="UA4000000001", maturity="2027-01-01", redemption="1200")
    database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date="2025-02-01", amount="80")
    database.add_sale(lot_id=lot["id"], sale_date="2025-03-01", sale_price="1050", quantity=1)
    redeemed = _add_purchase(database, bank, isin="UA4000000002", maturity="2025-03-01", redemption="1100")
    event, = database.list_scheduled_payments(redeemed["issue_id"])
    database.add_actual_payment(lot_id=redeemed["id"], scheduled_payment_id=event["id"], actual_date="2025-03-01",
                                amount="1100")

    # Treat both sold and redeemed purchases as closed, and report no yield for either empty currency.
    for currency in ("UAH", "EUR"):
        result = get_portfolio_returns(database, currency=currency, today=date(2025, 4, 1))
        assert result["purchase_ids"] == [] and result["flows"] == []
        assert result["expected_profit"] == Decimal("0.00")
        assert result["xirr"] == {"rate": None, "reason": "no_active_purchases"}
        assert result["simple_return"]["reason"] == "no_active_purchases"
        assert result["simple_return"]["rate"] is None


def test_missing_redemption_preserves_profit_but_cannot_produce_full_horizon_returns(database, bank):
    # A coupon without the remaining bond's redemption cannot represent a complete investment horizon.
    lot = _add_purchase(database, bank, isin="UA4000000001", maturity="2027-01-01", redemption=None)
    database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date="2026-01-01", amount="80")
    result = get_portfolio_returns(database, currency="UAH", today=date(2025, 1, 1))
    assert result["expected_profit"] == Decimal("-920.00")
    assert result["xirr"] == {"rate": None, "reason": "missing_redemption"}
    assert result["simple_return"] == {"rate": None, "reason": "missing_redemption", "cost_days": None}


def test_zero_duration_purchase_contributes_profit_to_other_active_purchase(database, bank):
    # Combine a same-day redemption forecast with a separate two-year investment.
    _add_purchase(database, bank, isin="UA4000000001", maturity="2025-01-01", redemption="1100")
    _add_purchase(database, bank, isin="UA4000000002", maturity="2027-01-01", redemption="1200")
    result = get_portfolio_returns(database, currency="UAH", today=date(2025, 1, 1))
    assert result["expected_profit"] == Decimal("300.00")
    assert result["simple_return"]["cost_days"] == Decimal("730000.00")
    assert result["simple_return"]["rate"] == Decimal("0.15")
    assert result["xirr"]["reason"] is None


def test_zero_duration_reports_the_reason_before_same_day_flows_cancel(database, bank):
    # A same-day purchase and expected redemption have no annual holding period.
    _add_purchase(database, bank, isin="UA4000000001", maturity="2025-01-01", redemption="1000")
    result = get_portfolio_returns(database, currency="UAH", today=date(2025, 1, 1))
    assert result["flows"] == []
    assert result["xirr"] == {"rate": None, "reason": "zero_duration"}
    assert result["simple_return"] == {"rate": None, "reason": "zero_duration", "cost_days": Decimal("0.00")}


def test_xirr_failure_does_not_hide_simple_return_or_profit(database, bank, monkeypatch):
    # Simulate an unavailable XIRR while retaining independently calculable portfolio metrics.
    _add_purchase(database, bank, isin="UA4000000001", maturity="2027-01-01", redemption="1200")
    monkeypatch.setattr("ovdp_tracker.analytics.xirr", lambda flows: {"rate": None, "reason": "numerical_failure"})
    result = get_portfolio_returns(database, currency="UAH", today=date(2025, 1, 1))
    assert result["xirr"] == {"rate": None, "reason": "numerical_failure"}
    assert result["expected_profit"] == Decimal("200.00")
    assert result["simple_return"]["rate"] == Decimal("0.1")
