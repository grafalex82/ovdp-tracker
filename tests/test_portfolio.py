"""Verify purchase lists, shared calendars, closed history and the save-to-portfolio flow."""

from datetime import date
from html import unescape

import pytest

from ovdp_calculator import create_app
from ovdp_calculator.db import Database
from ovdp_calculator.portfolio import get_lot_details, list_lot_groups


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    # Freeze the same local day at both the HTTP and portfolio boundaries.
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return the local day used for deterministic purchase and portfolio checks."""
            return cls(2026, 10, 31)

    # Keep explicit dates in domain calls compatible with ordinary date instances.
    monkeypatch.setattr("ovdp_calculator.web.date", LocalDate)
    monkeypatch.setattr("ovdp_calculator.portfolio.date", LocalDate)


@pytest.fixture
def saved_purchase(database, lot):
    # Insert events out of order, including two different kinds on maturity day.
    for kind, day in [
        ("redemption", lot["maturity_date"]), ("coupon", lot["maturity_date"]),
        ("coupon", "2024-08-15"), ("coupon", "2023-06-01"),
    ]:
        database.add_scheduled_payment(
            amount="1000" if kind == "redemption" else "80", issue_id=lot["issue_id"], kind=kind, planned_date=day
        )

    # Return a purchase whose calendar can be shared by later purchases of its ISIN.
    return lot


@pytest.fixture
def form(issue_data, lot_data):
    return {
        **issue_data, **lot_data, "quantity": "3", "purchase_price": "1000,01", "bank_name": "Банк А",
        "coupon_schedule": "2027-06-01 80\n2024-08-15 80", "issue_mode": "manual", "infer_maturity_from_coupons": "0",
    }


def purchase_row(html, lot_id):
    return html.split(f'<tr id="lot-{lot_id}">', 1)[1].split("</tr>", 1)[0]


def test_empty_lists_offer_add_bond_and_do_not_create_data(app, database):
    # Visit both empty lists and follow the add action to the unchanged form.
    with app.test_client() as client:
        home = client.get("/").get_data(as_text=True)
        history = client.get("/history").get_data(as_text=True)
        assert client.get("/purchases/new").status_code == 200

    # Explain both empty states and preserve the user's requested action label.
    assert "В портфеле пока нет действующих покупок" in home
    assert "Закрытых покупок пока нет" in history
    assert 'href="/purchases/new">Добавить облигацию</a>' in home
    assert "Добавить покупку" not in home
    assert 'href="/history"' in home
    assert 'href="/"' in history
    assert database.list_lots() == []
    assert database.list_issues() == []


def test_successful_save_closes_form_and_opens_purchase_in_portfolio(app, database, form):
    # Save through HTTP and verify that the redirect opens the home page.
    with app.test_client() as client:
        response = client.post("/purchases/new", data=form)
        assert response.status_code == 302
        assert response.location == "/?saved=1"
        home = client.get(response.location).get_data(as_text=True)
        lot, = database.list_lots()
        card = client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
        client.get(response.location)
        client.get(f'/purchases/{lot["id"]}')

    # Show a saved purchase and exact cost while refreshes remain read-only.
    assert "Покупка сохранена." in home
    assert "<form" not in home
    assert form["isin"] in home
    assert "3000,03" in home
    assert f'href="/purchases/{lot["id"]}"' in home
    assert "3000,03 UAH" in card
    assert database.list_lots() == [lot]
    assert len(database.list_issues()) == 1
    assert len(database.list_scheduled_payments()) == 3


def test_invalid_purchase_stays_in_populated_form_and_does_not_enter_list(app, database, form):
    # Submit an invalid quantity without losing the other input strings.
    form["quantity"] = "0"
    with app.test_client() as client:
        response = client.post("/purchases/new", data=form)
        home = client.get("/").get_data(as_text=True)

    # Retain field errors on the form instead of redirecting or claiming success.
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert response.location is None
    assert '<form method="post"' in html
    assert 'id="quantity-error"' in html
    assert 'value="1000,01"' in html
    assert "Покупка сохранена." not in html
    assert form["isin"] not in home
    assert database.list_lots() == []


def test_two_purchases_share_issue_but_keep_independent_fields(app, database, saved_purchase, lot_data):
    # Buy the same issue later at another price through a different bank.
    first = saved_purchase
    second = database.add_lot(
        issue_id=first["issue_id"], bank_id=database.add_bank("Банк Б")["id"],
        **{**lot_data, "purchase_date": "2025-01-20", "purchase_price": "1024", "quantity": 5},
    )
    with app.test_client() as client:
        home = client.get("/").get_data(as_text=True)
        first_card = client.get(f'/purchases/{first["id"]}').get_data(as_text=True)
        second_card = client.get(f'/purchases/{second["id"]}').get_data(as_text=True)

    # Group by ISIN once while rendering each purchase's own bank, date, quantity, price and cost.
    assert home.count(f'data-isin="{first["isin"]}"') == 1
    assert len(database.list_issues()) == 1
    for lot, bank_name, price, total in [
        (first, "Банк А", "950,01", "9500,10"), (second, "Банк Б", "1024,00", "5120,00"),
    ]:
        row = purchase_row(home, lot["id"])
        for value in (bank_name, lot["purchase_date"], str(lot["quantity"]), price, total, lot["maturity_date"]):
            assert value in row

    # Both cards show the same complete calendar and shared issue amounts.
    for card in (first_card, second_card):
        assert "1000,00 UAH" in card
        assert "80,00 UAH" in card
        assert "2023-06-01" in card
        for event in database.list_scheduled_payments(first["issue_id"]):
            assert f'data-event-id="{event["id"]}"' in card
    assert len(database.list_scheduled_payments()) == 4


def test_different_currency_issues_keep_separate_groups_and_amounts(app, database, issue_data, lot_data):
    # Give each currency its own ISIN, as required by the shared issue model.
    bank = database.add_bank("Банк А")
    issues = []
    for number, currency in enumerate(("UAH", "USD", "EUR"), 1):
        issue = database.add_issue(**{**issue_data, "isin": f"UA400000000{number}", "currency": currency})
        database.add_lot(issue_id=issue["id"], bank_id=bank["id"], **lot_data)
        issues.append(issue)
    with app.test_client() as client:
        home = client.get("/").get_data(as_text=True)

    # Render one group per issue without combining monetary totals across currencies.
    assert home.count('class="issue-group"') == 3
    for issue in issues:
        assert f'data-isin="{issue["isin"]}" data-currency="{issue["currency"]}"' in home
        assert f'Стоимость, {issue["currency"]}' in home
    assert "28500,30" not in home


@pytest.mark.parametrize("operation, remaining", [
    ("none", 10), ("partial-sale", 7), ("full-sale", 0), ("redemption", 0),
    ("coupon", 10), ("future-sale", 10), ("future-redemption", 10), ("past-plan", 10),
])
def test_active_and_closed_history_follow_actual_operations(app, database, lot, issue_data, operation, remaining):
    # Set up a planned maturity in the past when testing that a plan never closes a purchase.
    if operation == "past-plan":
        issue = database.add_issue(**{**issue_data, "isin": "UA4000000002", "maturity_date": "2025-06-01"})
        database.delete_lot(lot["id"])
        lot = database.add_lot(
            issue_id=issue["id"], bank_id=lot["bank_id"],
            purchase_date=lot["purchase_date"], purchase_price=lot["purchase_price"], quantity=lot["quantity"],
        )

    # Record only the operation that should affect this purchase's state.
    if "sale" in operation:
        database.add_sale(
            lot_id=lot["id"], sale_date="2026-11-01" if operation == "future-sale" else "2025-01-01",
            sale_price="1050", quantity=3 if operation == "partial-sale" else 10,
        )
    elif operation in ("redemption", "future-redemption", "coupon"):
        kind = "coupon" if operation == "coupon" else "redemption"
        event = database.add_scheduled_payment(
            amount="1000" if kind == "redemption" else "80",
            issue_id=lot["issue_id"], kind=kind, planned_date=lot["maturity_date"]
        )
        database.add_actual_payment(
            lot_id=lot["id"], scheduled_payment_id=event["id"], amount="795",
            actual_date="2026-11-01" if operation == "future-redemption" else "2026-10-31",
        )
    elif operation == "past-plan":
        database.add_scheduled_payment(
            amount="1000", issue_id=lot["issue_id"], kind="redemption", planned_date=lot["maturity_date"]
        )

    # Display the purchase in exactly the appropriate list and retain its direct card.
    with app.test_client() as client:
        home = client.get("/").get_data(as_text=True)
        history = client.get("/history").get_data(as_text=True)
        card = client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
    assert (f'id="lot-{lot["id"]}"' in home) == (remaining > 0)
    assert (f'id="lot-{lot["id"]}"' in history) == (remaining == 0)
    assert f"<dd>{remaining}</dd>" in card
    assert ("Закрыта" in card) == (remaining == 0)
    assert get_lot_details(database, lot["id"])["remaining_quantity"] == remaining


def test_closed_purchase_does_not_close_its_neighbor(app, database, saved_purchase, lot_data):
    # Redeem only the first purchase against the event shared by both purchases.
    first = saved_purchase
    second = database.add_lot(issue_id=first["issue_id"], bank_id=first["bank_id"], **lot_data)
    redemption, = [event for event in database.list_scheduled_payments() if event["kind"] == "redemption"]
    database.add_actual_payment(
        lot_id=first["id"], scheduled_payment_id=redemption["id"], actual_date="2026-10-31", amount="10000"
    )

    # Keep the second purchase active and the first purchase available in closed history.
    with app.test_client() as client:
        home = client.get("/").get_data(as_text=True)
        history = client.get("/history").get_data(as_text=True)
        assert client.get(f'/purchases/{first["id"]}').status_code == 200
    assert f'id="lot-{first["id"]}"' not in home
    assert f'id="lot-{second["id"]}"' in home
    assert f'id="lot-{first["id"]}"' in history
    assert f'id="lot-{second["id"]}"' not in history


def test_card_displays_all_purchase_fields_and_chronological_shared_calendar(app, database, saved_purchase):
    # Open a card directly, independently of any earlier page visit.
    lot = saved_purchase
    with app.test_client() as client:
        response = client.get(f'/purchases/{lot["id"]}')
    html = response.get_data(as_text=True)

    # Show original purchase fields, exact amounts, current state and a way back to the list.
    assert response.status_code == 200
    for value in (lot["isin"], "Банк А", "2024-02-15", "950,01 UAH", "9500,10 UAH", "1000,00 UAH", "80,00 UAH"):
        assert value in html
    assert '<a href="/">Портфель</a>' in html
    assert "Действующая" in html
    assert "Общий календарь выпуска" in html

    # Preserve every shared event while sorting by date and placing the final coupon before redemption.
    events = database.list_scheduled_payments(lot["issue_id"])
    expected = sorted(events, key=lambda event: (event["planned_date"], event["kind"] != "coupon"))
    positions = [html.index(f'data-event-id="{event["id"]}"') for event in expected]
    assert positions == sorted(positions)
    assert html.count("2027-06-01") == 3
    assert [event["id"] for event in events] == [event["id"] for event in database.list_scheduled_payments()]


def test_restart_and_repeated_gets_preserve_list_card_and_database(app, app_config, database, saved_purchase):
    # Capture rendered pages and shared records before reopening the application.
    lot = saved_purchase
    events = database.list_scheduled_payments()
    with app.test_client() as client:
        home = client.get("/").get_data(as_text=True)
        card = client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
    database.close()

    # Reopen the same temporary file and visit both pages repeatedly without creating anything.
    restarted = create_app(app_config)
    with restarted.test_client() as client:
        for _ in range(2):
            assert client.get("/").get_data(as_text=True) == home
            assert client.get(f'/purchases/{lot["id"]}').get_data(as_text=True) == card
    with Database(app_config["DATABASE"]) as reopened:
        assert reopened.list_lots() == [lot]
        assert len(reopened.list_issues()) == 1
        assert reopened.list_scheduled_payments() == events
        assert reopened.list_actual_payments() == []


def test_unknown_purchase_returns_404_without_creating_records(app, database):
    # Refuse unknown identifiers instead of rendering an empty purchase card.
    with app.test_client() as client:
        response = client.get("/purchases/999")

    # Leave storage untouched by the failed lookup.
    assert response.status_code == 404
    assert "Purchase not found." in response.get_data(as_text=True)
    assert get_lot_details(database, 999) is None
    assert list_lot_groups(database) == []


def test_list_and_card_escape_user_supplied_names(app, database, issue_data, lot_data):
    # Save names that must be treated as text in attributes and visible content.
    isin = '<script>alert("isin")</script>'
    bank_name = '<img src=x onerror="alert(1)">'
    issue = database.add_issue(**{**issue_data, "isin": isin})
    lot = database.add_lot(issue_id=issue["id"], bank_id=database.add_bank(bank_name)["id"], **lot_data)

    # Escape the same values consistently across the portfolio and purchase card.
    with app.test_client() as client:
        for path in ("/", f'/purchases/{lot["id"]}'):
            html = client.get(path).get_data(as_text=True)
            assert isin.upper() not in html
            assert bank_name not in html
            assert isin.upper() in unescape(html)
            assert bank_name in unescape(html)
