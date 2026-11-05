"""Check dated annualized cash-flow returns and structured calculation failures."""

from decimal import Decimal

import pytest

from ovdp_tracker.analytics import get_purchase_cash_flows, xirr


def _flows(*values):
    return [{"date": day, "amount": Decimal(amount)} for day, amount in values]


@pytest.mark.parametrize(("values", "expected"), [
    ((("2025-01-01", "-1000"), ("2027-01-01", "1200")), 0.095445115),
    ((("2025-01-01", "-1000"), ("2026-01-01", "100"), ("2027-01-01", "1100")), 0.10),
    ((("2025-01-01", "-1000"), ("2026-01-01", "800")), -0.20),
    ((("2025-01-01", "-1000"), ("2026-01-01", "1000")), 0.0),
    ((("2025-01-01", "-10000"), ("2026-01-01", "100")), -0.99),
    ((("2024-01-01", "-1000"), ("2025-01-01", "1100")), 1.1 ** (365 / 366) - 1),
])
def test_xirr_reference_returns(values, expected):
    result = xirr(_flows(*values))
    assert result["reason"] is None
    assert result["rate"] == pytest.approx(expected, abs=0.000001)


@pytest.mark.parametrize("values", [
    (("2025-01-01", "-100"), ("2026-01-01", "230"), ("2027-01-01", "-132")),
    (("2025-01-01", "-100"), ("2026-01-01", "220.50"), ("2027-01-01", "-121.55")),
])
def test_xirr_reports_two_roots_even_within_one_scan_interval(values):
    assert xirr(_flows(*values)) == {"rate": None, "reason": "multiple_roots"}


@pytest.mark.parametrize(("values", "reason"), [
    ((), "missing_sign"),
    ((("2025-01-01", "0"),), "missing_sign"),
    ((("2025-01-01", "-100"), ("2026-01-01", "-20")), "missing_sign"),
    ((("2025-01-01", "-100"), ("2025-01-01", "120")), "zero_duration"),
    ((("2025-01-01", "-100"), ("2025-01-01", "100"), ("2026-01-01", "50")), "missing_sign"),
    ((("2025-01-01", "-100"), ("2026-01-01", "200"), ("2027-01-01", "-101")), "numerical_failure"),
    ((("2025-01-01", "-100"), ("2026-01-01", "200"), ("2027-01-01", "-100")), "numerical_failure"),
])
def test_xirr_explains_unavailable_results(values, reason):
    assert xirr(_flows(*values)) == {"rate": None, "reason": reason}


def test_xirr_aggregates_dates_and_ignores_order_and_zero_totals():
    # Net same-day flows before solving, independently of input order and irrelevant zero totals.
    result = xirr(_flows(("2027-01-01", "1200"), ("2026-01-01", "25"), ("2025-01-01", "-1200"),
                         ("2026-01-01", "-25"), ("2025-01-01", "200")))
    assert result["reason"] is None
    assert result["rate"] == pytest.approx(0.095445115, abs=0.000001)


def test_xirr_consumes_actual_purchase_flows(database, bank):
    # Use a confirmed coupon on its actual date and the remaining redemption forecast.
    issue = database.add_issue(isin="UA4000000002", nominal="1000", currency="UAH", maturity_date="2027-01-01")
    lot = database.add_lot(issue_id=issue["id"], purchase_date="2025-01-01", purchase_price="1000", quantity=1,
                           bank_id=bank["id"])
    coupon = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date="2025-12-01",
                                            amount="80")
    database.add_scheduled_payment(issue_id=lot["issue_id"], kind="redemption", planned_date="2027-01-01",
                                   amount="1100")
    database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=coupon["id"], actual_date="2026-01-01",
                                amount="100")

    # The confirmed amount replaces its calendar forecast, producing the 10% benchmark.
    purchase = get_purchase_cash_flows(database, lot["id"])
    result = xirr(purchase["flows"])
    assert result["reason"] is None
    assert result["rate"] == pytest.approx(0.10, abs=0.000001)


def test_xirr_rejects_a_root_with_an_invalid_residual(monkeypatch):
    # Simulate a failed numerical refinement that returns an unrelated candidate.
    monkeypatch.setattr("ovdp_tracker.analytics._bisect", lambda *args: 0.0)
    result = xirr(_flows(("2025-01-01", "-1000"), ("2026-01-01", "1200")))
    assert result == {"rate": None, "reason": "numerical_failure"}


def test_xirr_normalizes_amounts_beyond_float_range():
    result = xirr(_flows(("2025-01-01", "-1e310"), ("2026-01-01", "1.1e310")))
    assert result["reason"] is None
    assert result["rate"] == pytest.approx(0.10, abs=0.000001)
