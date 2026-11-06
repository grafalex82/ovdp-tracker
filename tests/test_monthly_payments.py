"""Verify monthly payment totals, date filters and purchase-specific confirmations."""

from datetime import date
from decimal import Decimal, localcontext
import re

import pytest

from ovdp_tracker.analytics import monthly_payment_totals
from ovdp_tracker.issues import edit_issue
from ovdp_tracker.payments import confirm_payment
from ovdp_tracker.portfolio import list_payment_events
from ovdp_tracker.purchases import issue_form_values


TODAY = date(2026, 7, 1)


@pytest.fixture
def monthly_data(database, bank, monkeypatch):
    # Freeze the page date and create distinct coupon amounts and one redemption.
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return the date used by the monthly payment scenarios."""
            return cls(2026, 7, 1)

    monkeypatch.setattr("ovdp_tracker.web.date", LocalDate)
    issue = database.add_issue(isin="UA4000000001", nominal="1000", currency="UAH", maturity_date="2027-06-01")
    lot = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2025-01-01",
                           purchase_price="950", quantity=10)
    events = [database.add_scheduled_payment(issue_id=issue["id"], kind=kind, planned_date=day, amount=amount)
              for kind, day, amount in [("coupon", "2026-06-30", "80"), ("coupon", "2026-08-31", "85"),
                                        ("coupon", "2026-10-31", "90"), ("redemption", "2027-06-01", "1000")]]
    return issue, lot, events


def monthly(database, *, currency="UAH", start=None, end=None):
    # Use the same purchase-level filtered events as the page.
    groups = list_payment_events(database, currency=currency, start=start, end=end, today=TODAY)
    return {row["month"]: row for row in monthly_payment_totals(groups, start=start, end=end, today=TODAY)}


def test_confirmation_moves_only_one_expectation_and_keeps_empty_months(database, monthly_data):
    # June initially contains the first purchase's 800 and a neighboring purchase's 400.
    issue, lot, events = monthly_data
    neighbor = database.add_lot(issue_id=issue["id"], bank_id=lot["bank_id"], purchase_date="2025-02-01",
                                purchase_price="1000", quantity=5)
    start, end = date(2026, 6, 1), date(2026, 10, 31)
    before = monthly(database, start=start, end=end)
    assert before["2026-06"]["expected_coupon"] == Decimal("1200.00")
    assert before["2026-08"]["expected_coupon"] == Decimal("1275.00")
    assert before["2026-09"]["expected_total"] == Decimal("0.00")

    # The first pair's actual 795 in July replaces its 800 forecast, while the neighbor still expects 400.
    confirm_payment(database, lot["id"], events[0]["id"], {"actual_date": "2026-07-01", "amount": "795"}, TODAY)
    after = monthly(database, start=start, end=end)
    assert after["2026-06"]["expected_coupon"] == Decimal("400.00")
    assert after["2026-07"]["actual_coupon"] == after["2026-07"]["actual_total"] == Decimal("795.00")
    assert after["2026-08"] == before["2026-08"] and after["2026-10"] == before["2026-10"]
    assert database.list_actual_payments(neighbor["id"]) == []


def test_calendar_correction_updates_only_affected_unconfirmed_event(database, monthly_data):
    # Confirm the first event, then change only the August coupon from 85 to 87.
    issue, lot, events = monthly_data
    confirm_payment(database, lot["id"], events[0]["id"], {"actual_date": "2026-07-01", "amount": "795"}, TODAY)
    start, end = date(2026, 6, 1), date(2026, 10, 31)
    before = monthly(database, start=start, end=end)
    values = issue_form_values(issue, database.list_scheduled_payments(issue["id"]))
    next(coupon for coupon in values["coupons"] if coupon["date"] == "2026-08-31")["amount"] = "87"
    edit_issue(database, issue["id"], values)
    with localcontext() as context:
        context.prec = 3
        after = monthly(database, start=start, end=end)
    assert after["2026-08"]["expected_coupon"] == Decimal("870.00")
    assert all(after[month] == before[month] for month in ("2026-06", "2026-07", "2026-09", "2026-10"))


def test_monthly_boundaries_currency_redemption_and_closed_purchase(database, monthly_data, bank):
    # Retain an earned coupon after the purchase is closed and add a different currency.
    issue, lot, events = monthly_data
    database.add_sale(lot_id=lot["id"], sale_date="2026-06-30", sale_price="980", quantity=10)
    usd = database.add_issue(isin="UA4000000002", nominal="1000", currency="USD", maturity_date="2027-06-01")
    dollar = database.add_lot(issue_id=usd["id"], bank_id=bank["id"], purchase_date="2025-01-01",
                              purchase_price="900", quantity=1)
    usd_coupon = database.add_scheduled_payment(issue_id=usd["id"], kind="coupon", planned_date="2026-06-30",
                                                amount="50")
    redemption = database.add_scheduled_payment(issue_id=usd["id"], kind="redemption", planned_date="2027-06-01",
                                                amount="1000")
    database.add_actual_payment(lot_id=dollar["id"], scheduled_payment_id=usd_coupon["id"], actual_date="2026-06-30",
                                amount="49")
    database.add_actual_payment(lot_id=dollar["id"], scheduled_payment_id=redemption["id"], actual_date="2026-07-01",
                                amount="1000")
    june = monthly(database, start=date(2026, 6, 30), end=date(2026, 6, 30))
    assert june["2026-06"]["expected_coupon"] == Decimal("800.00")
    assert june["2026-06"]["actual_total"] == Decimal("0.00")
    dollars = monthly(database, currency="USD", start=date(2026, 6, 30), end=TODAY)
    assert dollars["2026-06"]["actual_coupon"] == Decimal("49.00")
    assert dollars["2026-07"]["actual_redemption"] == dollars["2026-07"]["actual_total"] == Decimal("1000.00")
    assert all(row["expected_total"] == 0 for row in dollars.values())
    assert monthly(database, start=date(2026, 6, 1), end=date(2026, 6, 29))["2026-06"]["expected_total"] == 0


def test_page_displays_default_and_explicit_ranges_including_empty_months(app, database, monthly_data):
    # The default starts this month and runs through the final unconfirmed redemption.
    _, lot, events = monthly_data
    confirm_payment(database, lot["id"], events[0]["id"], {"actual_date": "2026-07-01", "amount": "795"}, TODAY)
    with app.test_client() as client:
        assert 'href="/payments"' in client.get("/").get_data(as_text=True)
        page = client.get("/payments").get_data(as_text=True)
        filtered = client.get("/payments?start=2026-06-01&end=2026-08-31").get_data(as_text=True)
        partial = client.get("/payments?start=2026-07-02&end=2026-08-30").get_data(as_text=True)
        empty = client.get("/payments?currency=EUR&start=2026-06-01&end=2026-08-31").get_data(as_text=True)
        invalid = client.get("/payments?start=2026-09-01&end=2026-08-01")
    default_months = re.findall(r'data-month="(\d{4}-\d{2})"', page)
    assert len(default_months) == 12 and default_months[0] == "2026-07" and default_months[-1] == "2027-06"
    assert re.findall(r'data-month="(\d{4}-\d{2})"', filtered) == ["2026-06", "2026-07", "2026-08"]
    july_row = filtered.split('<tr data-month="2026-07">', 1)[1].split("</tr>", 1)[0]
    assert re.findall(r'<td class="number">(.*?)</td>', july_row) == [
        "0,00", "795,00", "0,00", "0,00", "0,00", "795,00",
    ]
    august_row = filtered.split('<tr data-month="2026-08">', 1)[1].split("</tr>", 1)[0]
    assert "850,00" in august_row
    partial_table = partial.split('aria-label="Выплаты по месяцам"', 1)[1].split("</table>", 1)[0]
    assert "795,00" not in partial_table and "850,00" not in partial_table
    assert len(re.findall(r'data-month="', empty)) == 3 and "0,00" in empty
    assert invalid.status_code == 400 and 'data-month=' not in invalid.get_data(as_text=True)


def test_open_bounds_and_no_expected_redemptions(database, monthly_data):
    # An individual open boundary reaches selected events even when they fall outside the current month.
    start_only = monthly(database, start=date(2026, 10, 1))
    end_only = monthly(database, end=date(2026, 6, 30))
    assert list(start_only)[0] == "2026-10" and list(start_only)[-1] == "2027-06"
    assert list(end_only) == ["2026-06"]
    assert list(monthly(database, currency="EUR")) == ["2026-07"]
