"""Verify current balance valuations and non-overlapping maturity groups."""

from datetime import date
from decimal import Decimal, localcontext
import re

import pytest

from ovdp_tracker.analytics import maturity_distribution, summarize_balances
from ovdp_tracker.portfolio import get_lot_details


@pytest.fixture
def fixed_today(monkeypatch):
    # Fix the date used by statistics and ISIN pages, including calendar month-end behavior.
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return the local date used by balance scenarios."""
            return cls(2024, 1, 31)

    monkeypatch.setattr("ovdp_tracker.web.date", LocalDate)
    return LocalDate.today()


def test_balances_aggregate_remaining_purchases_and_exclude_closed_parts(app, database, bank, fixed_today):
    # Keep seven of ten bonds, add a second active purchase and close a third by redemption.
    issue = database.add_issue(isin="UA4000000001", nominal="1000", currency="UAH", maturity_date="2025-01-31")
    first = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2023-01-01",
                             purchase_price="950", quantity=10)
    second = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2023-02-01",
                              purchase_price="1000.01", quantity=2)
    closed = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2023-03-01",
                              purchase_price="900", quantity=4)
    redemption = database.add_scheduled_payment(issue_id=issue["id"], kind="redemption",
                                                planned_date="2025-01-31", amount="1000")
    database.add_sale(lot_id=first["id"], sale_date="2023-06-01", sale_price="980", quantity=3)
    database.add_sale(lot_id=second["id"], sale_date="2024-02-01", sale_price="1000", quantity=1)
    database.add_actual_payment(lot_id=closed["id"], scheduled_payment_id=redemption["id"],
                                actual_date="2024-01-30", amount="4000")
    lots = [get_lot_details(database, lot["id"], today=fixed_today) for lot in (first, second, closed)]
    assert summarize_balances([lots[0]]) == {"quantity": 7, "cost": Decimal("6650.00"), "nominal": Decimal("7000.00")}
    with localcontext() as context:
        context.prec = 3
        assert summarize_balances(lots) == {"quantity": 9, "cost": Decimal("8650.02"), "nominal": Decimal("9000.00")}

    # Other currencies do not affect the UAH statistics; period filtering does not change current balances.
    usd = database.add_issue(isin="UA4000000002", nominal="1000", currency="USD", maturity_date="2025-01-31")
    database.add_lot(issue_id=usd["id"], bank_id=bank["id"], purchase_date="2023-01-01",
                     purchase_price="1000", quantity=100)
    database.add_scheduled_payment(issue_id=usd["id"], kind="redemption", planned_date="2025-01-31", amount="1000")
    with app.test_client() as client:
        page = client.get("/statistics?period=custom&start=2020-01-01&end=2020-01-31").get_data(as_text=True)
        position = client.get(f'/issues/{issue["isin"]}').get_data(as_text=True)
        dollar = client.get("/statistics?currency=USD&period=all").get_data(as_text=True)
    for html in (page, position):
        assert "8650,02 UAH" in html and "9000,00 UAH" in html
        assert "не рыночная стоимость" in html or "не являются рыночной стоимостью" in html
    assert "100000,00 USD" in dollar and "8650,02 UAH" not in dollar


def test_maturity_boundaries_and_overdue_group_include_today(database, bank, fixed_today):
    # Place one remaining bond immediately before and on each calendar-month boundary.
    dates = ["2024-01-30", "2024-01-31", "2024-04-29", "2024-04-30", "2024-07-30", "2024-07-31",
             "2025-01-30", "2025-01-31"]
    lots = []
    for index, maturity in enumerate(dates):
        issue = database.add_issue(isin=f"UA{index:010d}", nominal="1000", currency="UAH", maturity_date=maturity)
        lot = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2023-01-01",
                               purchase_price="950.01", quantity=1)
        database.add_scheduled_payment(issue_id=issue["id"], kind="redemption", planned_date=maturity, amount="1000")
        lots.append(get_lot_details(database, lot["id"], today=fixed_today))
    groups = maturity_distribution(lots, today=fixed_today)
    assert [group["quantity"] for group in groups] == [1, 2, 2, 2, 1]
    assert [group["cost"] for group in groups] == [Decimal("950.01"), Decimal("1900.02"), Decimal("1900.02"),
                                                   Decimal("1900.02"), Decimal("950.01")]
    assert [group["nominal"] for group in groups] == [Decimal("1000.00"), Decimal("2000.00"), Decimal("2000.00"),
                                                      Decimal("2000.00"), Decimal("1000.00")]


def test_month_end_clamping_and_table_rows(app, database, bank, monkeypatch):
    # August 31 plus six calendar months reaches the last day of February in a leap year.
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return a date whose six-month boundary requires clamping."""
            return cls(2023, 8, 31)

    monkeypatch.setattr("ovdp_tracker.web.date", LocalDate)
    for index, maturity in enumerate(("2023-08-30", "2024-02-28", "2024-02-29")):
        issue = database.add_issue(isin=f"UA{index:010d}", nominal="1000", currency="UAH", maturity_date=maturity)
        database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2023-01-01",
                         purchase_price="950", quantity=1)
        database.add_scheduled_payment(issue_id=issue["id"], kind="redemption", planned_date=maturity, amount="1000")
    with app.test_client() as client:
        html = client.get("/statistics?period=all").get_data(as_text=True)
    table = html.split('aria-label="Распределение по срокам погашения"', 1)[1].split("</table>", 1)[0]
    rows = re.findall(r"<tr><th scope=\"row\">(.*?)</th>(.*?)</tr>", table, re.DOTALL)
    assert len(rows) == 5
    assert [re.search(r'<td class="number">(\d+)</td>', row).group(1) for _, row in rows] == ["1", "0", "1", "1", "0"]
