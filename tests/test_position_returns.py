"""Verify ISIN returns from the complete cash flows of active purchases."""

from datetime import date
from decimal import Decimal, localcontext

import pytest

from ovdp_tracker.analytics import get_position_returns


def _issue(database, *, maturity="2027-01-01", currency="UAH"):
    # Save a shared issue and its single redemption event.
    issue = database.add_issue(isin="UA4000000001", nominal="1200", currency=currency, maturity_date=maturity)
    database.add_scheduled_payment(issue_id=issue["id"], kind="redemption", planned_date=maturity, amount="1200")
    return issue


def _lot(database, issue, bank, *, day, quantity=1, price="1000"):
    return database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date=day,
                            purchase_price=price, quantity=quantity)


def test_position_combines_two_purchase_horizons_without_averaging(database, bank):
    # Purchase one bond on each of two dates, both sharing the same 2027 redemption.
    issue = _issue(database)
    first = _lot(database, issue, bank, day="2025-01-01")
    second = _lot(database, issue, bank, day="2026-01-01")
    other = database.add_issue(isin="UA4000000002", nominal="1000", currency="UAH", maturity_date="2027-01-01")
    _lot(database, other, bank, day="2025-01-01", price="1")

    # Calculate from the combined flows and exact holding terms independently of the unrelated issue.
    with localcontext() as context:
        context.prec = 3
        result = get_position_returns(database, " ua4000000001 ", today=date(2026, 1, 1))
    assert result["isin"] == issue["isin"] and result["currency"] == "UAH"
    assert result["purchase_ids"] == [first["id"], second["id"]]
    assert result["flows"] == [
        {"date": "2025-01-01", "amount": Decimal("-1000.00")},
        {"date": "2026-01-01", "amount": Decimal("-1000.00")},
        {"date": "2027-01-01", "amount": Decimal("2400.00")},
    ]
    assert result["expected_profit"] == Decimal("400.00")
    assert result["xirr"]["reason"] is None
    assert result["xirr"]["rate"] == pytest.approx(0.12788206, abs=1e-7)
    assert result["simple_return"]["cost_days"] == Decimal("1095000.00")
    assert result["simple_return"]["rate"] == pytest.approx(Decimal(2) / 15, abs=Decimal("1e-25"))


def test_position_includes_partial_sale_and_excludes_closed_and_pre_purchase_coupons(database, bank):
    # Create three purchases sharing coupons; close one and partially sell another.
    issue = _issue(database, currency="USD")
    first = _lot(database, issue, bank, day="2025-01-01", quantity=10)
    closed = _lot(database, issue, bank, day="2025-04-01", quantity=2)
    late = _lot(database, issue, bank, day="2025-05-01", quantity=2)
    first_coupon = database.add_scheduled_payment(issue_id=issue["id"], kind="coupon", planned_date="2025-03-01",
                                                  amount="80")
    database.add_scheduled_payment(issue_id=issue["id"], kind="coupon", planned_date="2026-04-01", amount="90")
    database.add_actual_payment(lot_id=first["id"], scheduled_payment_id=first_coupon["id"],
                                actual_date="2025-03-02", amount="795")
    database.add_sale(lot_id=first["id"], sale_date="2025-06-10", sale_price="1050", quantity=3)
    database.add_sale(lot_id=closed["id"], sale_date="2025-07-01", sale_price="1100", quantity=2)

    # Keep the partial purchase's full history and only the late purchase's eligible coupon.
    result = get_position_returns(database, issue["isin"], today=date(2025, 7, 1))
    assert result["currency"] == "USD"
    assert result["purchase_ids"] == [first["id"], late["id"]]
    assert result["flows"] == [
        {"date": "2025-01-01", "amount": Decimal("-10000.00")},
        {"date": "2025-03-02", "amount": Decimal("795.00")},
        {"date": "2025-05-01", "amount": Decimal("-2000.00")},
        {"date": "2025-06-10", "amount": Decimal("3150.00")},
        {"date": "2026-04-01", "amount": Decimal("810.00")},
        {"date": "2027-01-01", "amount": Decimal("10800.00")},
    ]
    assert result["expected_profit"] == Decimal("3555.00")
    assert result["xirr"]["reason"] is None and result["simple_return"]["reason"] is None


def test_closed_position_and_unknown_issue_are_distinguished(database, bank):
    # Close the only purchase even though a historical coupon remains unconfirmed.
    issue = _issue(database)
    lot = _lot(database, issue, bank, day="2025-01-01")
    database.add_scheduled_payment(issue_id=issue["id"], kind="coupon", planned_date="2025-03-01", amount="80")
    database.add_sale(lot_id=lot["id"], sale_date="2025-04-01", sale_price="1100", quantity=1)
    orphan = database.add_issue(isin="UA4000000002", nominal="1000", currency="EUR", maturity_date="2027-01-01")

    # Return explicit reasons for closed positions and None when no purchase belongs to the issue.
    result = get_position_returns(database, issue["isin"], today=date(2025, 5, 1))
    assert result["purchase_ids"] == [] and result["flows"] == []
    assert result["xirr"] == {"rate": None, "reason": "no_active_purchases"}
    assert result["simple_return"]["reason"] == "no_active_purchases"
    assert result["simple_return"]["rate"] is None
    assert get_position_returns(database, orphan["isin"]) is None
    assert get_position_returns(database, "UA4000000003") is None


def test_position_with_zero_term_returns_reasons(database, bank):
    # Keep the purchase active on its planned same-day redemption without inventing an annual yield.
    issue = _issue(database, maturity="2025-01-01")
    _lot(database, issue, bank, day="2025-01-01", price="1200")
    result = get_position_returns(database, issue["isin"], today=date(2025, 1, 1))
    assert result["expected_profit"] == Decimal("0.00")
    assert result["xirr"] == {"rate": None, "reason": "zero_duration"}
    assert result["simple_return"] == {"rate": None, "reason": "zero_duration", "cost_days": Decimal("0.00")}


def test_position_xirr_failure_does_not_hide_simple_return(database, bank, monkeypatch):
    # Simulate an unstable XIRR while preserving independently computable amounts and simple return.
    issue = _issue(database)
    _lot(database, issue, bank, day="2025-01-01")
    monkeypatch.setattr("ovdp_tracker.analytics.xirr", lambda flows: {"rate": None, "reason": "numerical_failure"})
    result = get_position_returns(database, issue["isin"], today=date(2025, 1, 1))
    assert result["xirr"] == {"rate": None, "reason": "numerical_failure"}
    assert result["expected_profit"] == Decimal("200.00")
    assert result["simple_return"]["rate"] == Decimal("0.1")
