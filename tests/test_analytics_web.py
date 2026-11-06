"""Verify presentation of full-horizon returns and purchase-specific receipts."""

from datetime import date

import pytest


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    # Freeze the local date consistently for HTTP and purchase descriptions.
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return the date used by the web analytics scenarios."""
            return cls(2026, 1, 1)

    monkeypatch.setattr("ovdp_tracker.web.date", LocalDate)
    monkeypatch.setattr("ovdp_tracker.portfolio.date", LocalDate)


def test_position_and_portfolio_keep_amounts_and_simple_return_when_xirr_fails(app, database, bank, monkeypatch):
    # Record two purchases sharing one calendar, with only the first coupon confirmed.
    issue = database.add_issue(isin="UA4000000001", nominal="1000", currency="UAH", maturity_date="2027-01-01")
    first = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2025-01-01",
                             purchase_price="1000", quantity=10)
    second = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2025-01-01",
                              purchase_price="1000", quantity=5)
    coupon = database.add_scheduled_payment(issue_id=issue["id"], kind="coupon", planned_date="2025-07-01",
                                            amount="80")
    database.add_scheduled_payment(issue_id=issue["id"], kind="coupon", planned_date="2027-01-01", amount="85")
    database.add_scheduled_payment(issue_id=issue["id"], kind="redemption", planned_date="2027-01-01", amount="1000")
    database.add_actual_payment(lot_id=first["id"], scheduled_payment_id=coupon["id"], actual_date="2025-07-02",
                                amount="795")
    failure = lambda flows: {"rate": None, "reason": "numerical_failure"}
    monkeypatch.setattr("ovdp_tracker.analytics.xirr", failure)
    monkeypatch.setattr("ovdp_tracker.web.xirr", failure)

    # Check each screen retains the independently computed money and simple annual return.
    with app.test_client() as client:
        home = client.get("/").get_data(as_text=True)
        details = client.get(f'/issues/{issue["isin"]}').get_data(as_text=True)
        card = client.get(f'/purchases/{first["id"]}').get_data(as_text=True)
        second_card = client.get(f'/purchases/{second["id"]}').get_data(as_text=True)
    for html in (home, details, card, second_card):
        assert "численный расчёт не удался" in html
        assert "Простая доходность, % годовых · весь срок" in html
    assert "предварительные" in home and "предварительные" in details and "предварительные" in second_card
    assert "ожидаемые" in card and "предварительные" not in card
    for html in (home, details):
        assert "2470,00 UAH" in html
        assert "8,23%" in html
    assert "795,00 UAH" in details
    assert "1675,00 UAH" in details
    assert "15000,00 UAH" in details
    assert "1645,00 UAH" in card


def test_closed_position_has_no_fabricated_expected_return_and_purchase_status(app, database, bank):
    # Close a purchase by sale while its earlier coupon still needs confirmation.
    issue = database.add_issue(isin="UA4000000001", nominal="1000", currency="UAH", maturity_date="2027-01-01")
    lot = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2025-01-01",
                           purchase_price="1000", quantity=1)
    coupon = database.add_scheduled_payment(issue_id=issue["id"], kind="coupon", planned_date="2025-06-01",
                                            amount="80")
    database.add_scheduled_payment(issue_id=issue["id"], kind="redemption", planned_date="2027-01-01", amount="1000")
    database.add_sale(lot_id=lot["id"], sale_date="2025-07-01", sale_price="1100", quantity=1)

    # The closed position has no expected active yield, while its purchase remains provisional.
    with app.test_client() as client:
        history = client.get("/history").get_data(as_text=True)
        details = client.get(f'/issues/{issue["isin"]}').get_data(as_text=True)
        card = client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
    assert "нет действующих покупок" in history and "нет действующих покупок" in details
    assert "0,00%" not in history and "0,00%" not in details
    assert "ожидаемые" in card and "предварительные" in card
    assert "180,00 UAH" in card and "100,00 UAH" in card

    # Once the coupon is confirmed, the closed purchase's full-horizon results become factual.
    database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=coupon["id"], actual_date="2025-06-02",
                                amount="75")
    with app.test_client() as client:
        card = client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
    assert "покупки: фактические" in card
    assert "предварительные" not in card
    assert "175,00 UAH" in card


def test_position_row_combines_purchases_and_portfolio_keeps_currencies_separate(app, database, bank):
    # Buy the same issue at two dates and add an unrelated purchase in another currency.
    issue = database.add_issue(isin="UA4000000001", nominal="1200", currency="UAH", maturity_date="2027-01-01")
    for day in ("2025-01-01", "2026-01-01"):
        database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date=day,
                         purchase_price="1000", quantity=1)
    database.add_scheduled_payment(issue_id=issue["id"], kind="redemption", planned_date="2027-01-01", amount="1200")
    dollar = database.add_issue(isin="UA4000000002", nominal="1500", currency="USD", maturity_date="2027-01-01")
    database.add_lot(issue_id=dollar["id"], bank_id=bank["id"], purchase_date="2025-01-01",
                     purchase_price="1000", quantity=1)
    database.add_scheduled_payment(issue_id=dollar["id"], kind="redemption", planned_date="2027-01-01", amount="1500")

    # Present the combined XIRR in the row and both rates and profit in the correct currency only.
    with app.test_client() as client:
        home = client.get("/?currency=UAH").get_data(as_text=True)
        details = client.get(f'/issues/{issue["isin"]}').get_data(as_text=True)
        usd = client.get("/?currency=USD").get_data(as_text=True)
    row = home.split('data-isin="UA4000000001"', 1)[1].split("</tr>", 1)[0]
    assert "12,79%" in row
    for html in (home, details):
        assert "12,79%" in html and "13,33%" in html and "400,00 UAH" in html
        assert "500,00 USD" not in html
    assert "500,00 USD" in usd and "400,00 UAH" not in usd
