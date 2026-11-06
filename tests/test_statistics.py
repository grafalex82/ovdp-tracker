"""Verify statistics periods, currency separation and full-horizon returns."""

from datetime import date
import re

import pytest

from ovdp_tracker.analytics import summarize_operations


@pytest.fixture
def statistics_data(database, bank, monkeypatch):
    # Fix the local date and create historical operations across a month boundary.
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return the local date used by the statistics scenarios."""
            return cls(2026, 7, 15)

    monkeypatch.setattr("ovdp_tracker.web.date", LocalDate)
    issue = database.add_issue(isin="UA4000000001", nominal="1000", currency="UAH", maturity_date="2027-01-01")
    lot = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2025-01-01",
                           purchase_price="950.01", quantity=10)
    first = database.add_scheduled_payment(issue_id=issue["id"], kind="coupon", planned_date="2026-06-30",
                                           amount="80")
    second = database.add_scheduled_payment(issue_id=issue["id"], kind="coupon", planned_date="2026-07-01",
                                            amount="85")
    database.add_scheduled_payment(issue_id=issue["id"], kind="redemption", planned_date="2027-01-01", amount="1000")
    database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=first["id"], actual_date="2026-06-30",
                                amount="795")
    database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=second["id"], actual_date="2026-07-01",
                                amount="845")
    database.add_sale(lot_id=lot["id"], sale_date="2026-06-30", sale_price="980", quantity=3)

    # Keep a distinct currency out of the UAH totals.
    usd = database.add_issue(isin="UA4000000002", nominal="1000", currency="USD", maturity_date="2027-01-01")
    database.add_lot(issue_id=usd["id"], bank_id=bank["id"], purchase_date="2025-01-01",
                     purchase_price="1000", quantity=1)
    database.add_scheduled_payment(issue_id=usd["id"], kind="redemption", planned_date="2027-01-01", amount="1000")
    return lot


def test_operation_totals_include_boundaries_actual_dates_and_disposal_cost(database, statistics_data):
    # June includes the sale and first actual coupon, but not July's receipt or the planned redemption.
    totals = summarize_operations(database, currency="UAH", start=date(2026, 6, 1), end=date(2026, 6, 30),
                                  today=date(2026, 7, 15))
    assert str(totals["coupon"]) == "795.00"
    assert str(totals["sale"]) == "2940.00"
    assert str(totals["redemption"]) == "0.00"
    assert str(totals["realized_profit"]) == "884.97"

    # All historical purchases retain exact cents and other currencies remain separate.
    all_time = summarize_operations(database, currency="UAH", start=None, end=None, today=date(2026, 7, 15))
    assert str(all_time["purchase"]) == "9500.10"
    assert str(all_time["coupon"]) == "1640.00"
    usd = summarize_operations(database, currency="USD", start=None, end=None, today=date(2026, 7, 15))
    assert str(usd["purchase"]) == "1000.00" and str(usd["coupon"]) == "0.00"


def test_confirmed_redemption_is_included_by_actual_date(database, statistics_data):
    # Confirm the remaining seven bonds after the sale and test its actual-date boundary.
    redemption = next(event for event in database.list_scheduled_payments(statistics_data["issue_id"])
                      if event["kind"] == "redemption")
    database.add_actual_payment(lot_id=statistics_data["id"], scheduled_payment_id=redemption["id"],
                                actual_date="2026-07-15", amount="7000")
    totals = summarize_operations(database, currency="UAH", start=date(2026, 7, 15), end=date(2026, 7, 15),
                                  today=date(2026, 7, 15))
    assert str(totals["redemption"]) == "7000.00"
    assert str(totals["realized_profit"]) == "349.93"
    before = summarize_operations(database, currency="UAH", start=date(2026, 7, 1), end=date(2026, 7, 14),
                                  today=date(2026, 7, 15))
    assert str(before["redemption"]) == "0.00"


def test_empty_statistics_and_navigation(app):
    # Empty currencies show ordinary zero amounts and retain working section links.
    with app.test_client() as client:
        page = client.get("/statistics?currency=EUR&period=all").get_data(as_text=True)
        assert client.get("/").status_code == 200
        assert client.get("/payments").status_code == 200
    assert "0,00 EUR" in page and "-0,00 EUR" not in page
    assert "нет действующих покупок" in page


@pytest.mark.parametrize(("period", "expected_coupon"), [
    ("month", "845,00 UAH"), ("quarter", "845,00 UAH"), ("year", "1640,00 UAH"),
    ("all", "1640,00 UAH"),
])
def test_presets_and_full_horizon_returns(app, statistics_data, period, expected_coupon):
    # Presets use calendar boundaries while the full-horizon rates remain unchanged.
    with app.test_client() as client:
        page = client.get(f"/statistics?period={period}").get_data(as_text=True)
        june = client.get("/statistics?period=custom&start=2026-06-01&end=2026-06-30").get_data(as_text=True)
    operations = page.split("Операции за выбранный период", 1)[1].split("</dl>", 1)[0]
    assert expected_coupon in operations
    assert '<span aria-current="page">Статистика</span>' in page
    assert '<a href="/">Портфель</a>' in page and '<a href="/payments">Выплаты</a>' in page
    assert "795,00 UAH" in june and "2940,00 UAH" in june and "884,97 UAH" in june
    rates = re.findall(r"(?:XIRR|Простая доходность), % годовых · весь срок</dt>\s*<dd>(.*?)</dd>", page)
    june_rates = re.findall(r"(?:XIRR|Простая доходность), % годовых · весь срок</dt>\s*<dd>(.*?)</dd>", june)
    assert len(rates) == 2 and rates == june_rates


@pytest.mark.parametrize("query", ["start=2026-07-01&end=2026-06-30", "start=2026-02-30", "end=bad"])
def test_invalid_custom_range_is_not_calculated(app, statistics_data, monkeypatch, query):
    # Reject invalid dates before requesting any operation totals.
    def unexpected(*args, **kwargs):
        raise AssertionError("Invalid range must not be evaluated.")

    monkeypatch.setattr("ovdp_tracker.web.summarize_operations", unexpected)
    with app.test_client() as client:
        response = client.get(f"/statistics?period=custom&{query}")
    html = response.get_data(as_text=True)
    assert response.status_code == 400 and 'role="alert"' in html
    assert "Операции за выбранный период</h2>" not in html
