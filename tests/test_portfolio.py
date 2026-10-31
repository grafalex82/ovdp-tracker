"""Verify ISIN positions, purchase details, shared calendars and the save-to-portfolio flow."""

from datetime import date
from html import unescape

import pytest

from ovdp_calculator import create_app
from ovdp_calculator.db import Database
from ovdp_calculator.portfolio import get_lot_details, get_position_details, list_positions


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
    assert "В портфеле пока нет действующих позиций в UAH" in home
    assert "Закрытых позиций в UAH пока нет" in history
    assert 'href="/purchases/new">Добавить облигацию</a>' in home
    assert "Добавить покупку" not in home
    assert 'href="/history?currency=UAH"' in home
    assert 'href="/"' in history
    assert database.list_lots() == []
    assert database.list_issues() == []


@pytest.mark.parametrize("currency", ["UAH", "USD", "EUR"])
def test_successful_save_closes_form_and_opens_purchase_in_portfolio(app, database, form, currency):
    # Save through HTTP and verify that the redirect opens the home page.
    form["currency"] = currency
    with app.test_client() as client:
        response = client.post("/purchases/new", data=form)
        assert response.status_code == 302
        assert response.location == f"/?saved=1&currency={currency}"
        home = client.get(response.location).get_data(as_text=True)
        lot, = database.list_lots()
        details = client.get(f'/issues/{lot["isin"]}').get_data(as_text=True)
        card = client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
        client.get(response.location)
        client.get(f'/purchases/{lot["id"]}')

    # Show a saved purchase and exact cost while refreshes remain read-only.
    assert "Покупка сохранена." in home
    assert '<form method="post"' not in home
    assert form["isin"] in home
    assert "3000,03" not in home
    assert "3000,03" in details
    assert f'href="/issues/{lot["isin"]}"' in home
    assert f'href="/purchases/{lot["id"]}"' in details
    assert f"3000,03 {currency}" in card
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
    database.add_sale(lot_id=first["id"], sale_date="2026-01-01", sale_price="1050", quantity=3)
    with app.test_client() as client:
        home = client.get("/").get_data(as_text=True)
        details = client.get(f'/issues/{first["isin"]}').get_data(as_text=True)
        first_card = client.get(f'/purchases/{first["id"]}').get_data(as_text=True)
        second_card = client.get(f'/purchases/{second["id"]}').get_data(as_text=True)

    # Sum 7 + 5 into one row and move independent purchase fields to the position details.
    assert home.count(f'data-isin="{first["isin"]}"') == 1
    assert "<td>12</td>" in home
    assert "80,00" in home
    assert "Дата покупки" not in home
    assert "Банк А" not in home and "Банк Б" not in home
    assert f'/purchases/{first["id"]}' not in home
    assert f'/purchases/{second["id"]}' not in home
    assert list_positions(database)[0]["remaining_quantity"] == 12
    assert len(database.list_issues()) == 1
    for lot, bank_name, price, total in [
        (first, "Банк А", "950,01", "9500,10"), (second, "Банк Б", "1024,00", "5120,00"),
    ]:
        row = purchase_row(details, lot["id"])
        for value in (bank_name, lot["purchase_date"], str(lot["quantity"]), price, total):
            assert value in row

    # Both cards show the same complete calendar and shared issue amounts.
    for card in (details, first_card, second_card):
        assert "1000,00 UAH" in card
        assert "80,00" in card
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

    # Show one selected currency at a time and retain it when switching to closed positions.
    with app.test_client() as client:
        for issue in issues:
            currency = issue["currency"]
            home = client.get(f'/?currency={currency}').get_data(as_text=True)
            assert home.count('data-isin=') == 1
            assert f'data-isin="{issue["isin"]}" data-currency="{currency}"' in home
            assert f'<option value="{currency}" selected>' in home
            assert f'href="/history?currency={currency}"' in home
            for other in issues:
                assert (other["isin"] in home) == (other == issue)
            assert "28500,30" not in home
            assert client.get(f'/issues/{issue["isin"]}').status_code == 200
        assert client.get("/").get_data(as_text=True) == client.get("/?currency=UAH").get_data(as_text=True)


@pytest.mark.parametrize("nominal_rate, displayed", [("19.18", "19,18%"), ("0", "0%"), ("", "Не указана")])
def test_portfolio_columns_show_the_saved_nominal_yield(app, database, issue_data, lot_data, nominal_rate, displayed):
    # Store the nominal issue rate independently of purchase price or calculated investment returns.
    issue = database.add_issue(**issue_data, nominal_rate=nominal_rate)
    database.add_lot(issue_id=issue["id"], bank_id=database.add_bank("Банк А")["id"], **lot_data)

    # Present the requested columns and explain which yield the rate represents.
    with app.test_client() as client:
        html = client.get("/").get_data(as_text=True)
    assert '<th scope="col">Количество</th>' in html
    assert '<th scope="col">Купон</th>' in html
    assert '<th scope="col">Доходность</th>' in html
    assert "Осталось, шт." not in html and "Ближайший купон" not in html
    assert "доходность — номинальная" in html
    assert displayed in html


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
    assert (f'data-isin="{lot["isin"]}"' in home) == (remaining > 0)
    assert (f'data-isin="{lot["isin"]}"' in history) == (remaining == 0)
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
        details = client.get(f'/issues/{first["isin"]}').get_data(as_text=True)
        assert client.get(f'/purchases/{first["id"]}').status_code == 200
    assert f'data-isin="{first["isin"]}"' in home
    assert f'data-isin="{first["isin"]}"' not in history
    assert "Закрыта" in purchase_row(details, first["id"])
    assert "Действующая" in purchase_row(details, second["id"])
    position = get_position_details(database, first["isin"])
    assert position["remaining_quantity"] == second["quantity"]
    assert len(position["lots"]) == 2


@pytest.mark.parametrize("confirmed_later_coupon", [False, True])
def test_early_actual_redemption_stops_later_coupons_only_for_its_purchase(
    app, database, lot, lot_data, confirmed_later_coupon,
):
    # Share a coupon on the actual redemption day and a later coupon on the planned maturity day.
    neighbor = database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **lot_data)
    today_coupon = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date="2026-10-31", amount="80"
    )
    later_coupon = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date=lot["maturity_date"], amount="85"
    )
    redemption = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="redemption", planned_date=lot["maturity_date"], amount="1000"
    )
    actual = database.add_actual_payment(
        lot_id=lot["id"], scheduled_payment_id=redemption["id"], actual_date="2026-10-31", amount="10000"
    )
    actuals = [actual]
    if confirmed_later_coupon:
        actuals.append(database.add_actual_payment(
            lot_id=lot["id"], scheduled_payment_id=later_coupon["id"], actual_date="2026-10-31", amount="795"
        ))

    # Retain the same-day coupon before redemption, but exclude later expectations from the redeemed purchase.
    position = get_position_details(database, lot["isin"])
    redeemed, active = position["lots"]
    assert redeemed["closed"] and not active["closed"]
    assert position["remaining_quantity"] == neighbor["quantity"]
    expected = [today_coupon["id"], later_coupon["id"], redemption["id"]] if confirmed_later_coupon else [
        today_coupon["id"], redemption["id"],
    ]
    assert [payment["id"] for payment in redeemed["payments"]] == expected
    assert [payment["id"] for payment in active["payments"]] == [
        today_coupon["id"], later_coupon["id"], redemption["id"],
    ]
    assert redeemed["payments"][-1]["actual"] == actual

    # Keep the shared calendar and neighboring rights while preserving already recorded coupon receipts.
    with app.test_client() as client:
        details = client.get(f'/issues/{lot["isin"]}').get_data(as_text=True)
    assert f'data-event-id="{later_coupon["id"]}"' in details
    assert (f'data-lot-id="{lot["id"]}" data-payment-id="{later_coupon["id"]}"' in details) == confirmed_later_coupon
    assert f'data-lot-id="{neighbor["id"]}" data-payment-id="{later_coupon["id"]}"' in details
    assert database.list_actual_payments(lot["id"]) == actuals


@pytest.mark.parametrize("currency", ["UAH", "USD", "EUR"])
def test_last_closed_purchase_moves_whole_position_to_currency_history(app, database, issue_data, lot_data, currency):
    # Keep the whole position active until the last purchase has been fully sold.
    issue = database.add_issue(**{**issue_data, "currency": currency})
    bank = database.add_bank("Банк А")
    first = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], **lot_data)
    second = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], **lot_data)
    for lot in (first, second):
        database.add_sale(lot_id=lot["id"], sale_date="2025-01-01", sale_price="1050", quantity=lot["quantity"])

    # Closed history retains currency selection, every purchase and the shared calendar page.
    with app.test_client() as client:
        home = client.get(f"/?currency={currency}").get_data(as_text=True)
        history = client.get(f"/history?currency={currency}").get_data(as_text=True)
        details = client.get(f'/issues/{issue["isin"]}').get_data(as_text=True)
    assert f'data-isin="{issue["isin"]}"' not in home
    assert history.count(f'data-isin="{issue["isin"]}"') == 1
    assert "<td>0</td>" in history
    assert "Нет будущего купона" in history
    assert f'href="/?currency={currency}"' in history
    assert "Закрытая позиция" in details
    assert "Закрыта" in purchase_row(details, first["id"])
    assert "Закрыта" in purchase_row(details, second["id"])


@pytest.mark.parametrize("confirmed", [0, 1, 2])
def test_next_coupon_uses_today_and_keeps_partly_confirmed_shared_date(app, database, lot, lot_data, confirmed):
    # Include past, zero, today's and later coupon amounts in the same shared calendar.
    second = database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **lot_data)
    for day, amount in [("2025-01-01", "70"), ("2026-10-30", "0"), ("2026-10-31", "80"), ("2027-06-01", "85")]:
        event = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date=day, amount=amount)
        if day == "2026-10-31":
            for purchase in (lot, second)[:confirmed]:
                database.add_actual_payment(
                    lot_id=purchase["id"], scheduled_payment_id=event["id"], actual_date=day, amount="795"
                )

    # Only a fully confirmed date gives way to the next coupon, retaining that event's own amount.
    position, = list_positions(database)
    expected_date, expected_amount = ("2027-06-01", "85") if confirmed == 2 else ("2026-10-31", "80")
    assert position["next_coupon"]["planned_date"] == expected_date
    assert position["next_coupon"]["amount"] == expected_amount
    with app.test_client() as client:
        home = client.get("/").get_data(as_text=True)
    assert f"<td>{expected_date}</td>" in home
    assert f"<td>{expected_amount},00</td>" in home


@pytest.mark.parametrize("coupon_date, amount", [(None, None), ("2025-01-01", "80"), ("2027-06-01", "0")])
def test_no_future_positive_coupon_is_explicit(app, database, lot, coupon_date, amount):
    # Cover zero-coupon, past-only and zero-amount calendars without inventing a coupon.
    if coupon_date is not None:
        database.add_scheduled_payment(
            issue_id=lot["issue_id"], kind="coupon", planned_date=coupon_date, amount=amount
        )

    # Keep the active position while explaining the missing upcoming coupon.
    assert list_positions(database)[0]["next_coupon"] is None
    with app.test_client() as client:
        home = client.get("/").get_data(as_text=True)
    assert "Нет будущего купона" in home
    assert f'data-isin="{lot["isin"]}"' in home


def test_position_payments_preserve_purchase_rights_and_independent_facts(app, database, issue_data, lot_data):
    # Buy the same issue before and after its first coupon, then sell part of the first purchase.
    issue = database.add_issue(**{**issue_data, "maturity_date": "2026-12-01"})
    bank = database.add_bank("Банк А")
    first = database.add_lot(
        issue_id=issue["id"], bank_id=bank["id"], **{**lot_data, "purchase_date": "2026-01-01"}
    )
    second = database.add_lot(
        issue_id=issue["id"], bank_id=bank["id"], **{**lot_data, "purchase_date": "2026-08-01", "quantity": 5}
    )
    june = database.add_scheduled_payment(issue_id=issue["id"], kind="coupon", planned_date="2026-06-01", amount="80")
    december = database.add_scheduled_payment(
        issue_id=issue["id"], kind="coupon", planned_date="2026-12-01", amount="85"
    )
    redemption = database.add_scheduled_payment(
        issue_id=issue["id"], kind="redemption", planned_date="2026-12-01", amount="1000"
    )
    database.add_sale(lot_id=first["id"], sale_date="2026-07-01", sale_price="1050", quantity=3)
    database.add_actual_payment(
        lot_id=first["id"], scheduled_payment_id=june["id"], actual_date="2026-06-03", amount="795"
    )

    # Display one calendar while keeping purchase-specific rights, historical quantities and facts separate.
    position = get_position_details(database, issue["isin"])
    first_payments = position["lots"][0]["payments"]
    second_payments = position["lots"][1]["payments"]
    assert [payment["id"] for payment in first_payments] == [june["id"], december["id"], redemption["id"]]
    assert [payment["planned_amount"] for payment in first_payments] == ["800.00", "595.00", "7000.00"]
    assert first_payments[0]["actual"]["amount"] == "795"
    assert [payment["id"] for payment in second_payments] == [december["id"], redemption["id"]]
    assert all(payment["actual"] is None for payment in second_payments)
    with app.test_client() as client:
        details = client.get(f'/issues/{issue["isin"]}').get_data(as_text=True)
    assert details.count('data-event-id=') == 3
    assert f'data-lot-id="{first["id"]}" data-payment-id="{june["id"]}"' in details
    assert f'data-lot-id="{second["id"]}" data-payment-id="{june["id"]}"' not in details
    assert "795,00" in details and "2026-06-03" in details and "595,00" in details


@pytest.mark.parametrize("field, value", [
    ("currency", "USD"), ("nominal", "2000"), ("maturity_date", "2028-06-01"),
    ("coupon_schedule", "2027-06-01 85\n2024-08-15 80"),
])
def test_conflicting_repeat_purchase_cannot_change_position(app, database, form, field, value):
    # Save the shared issue before attempting to replace its common fields through a second purchase.
    with app.test_client() as client:
        assert client.post("/purchases/new", data=form).status_code == 302
        lot, = database.list_lots()
        before = get_position_details(database, lot["isin"])
        response = client.post("/purchases/new", data={**form, field: value, "bank_name": "Другой банк"})

    # Reject conflicting common fields atomically instead of creating another version of the position.
    assert response.status_code == 400
    assert get_position_details(database, lot["isin"]) == before
    assert len(database.list_lots()) == 1
    assert len(database.list_issues()) == 1
    assert len(database.list_banks()) == 1


@pytest.mark.parametrize("path", ["/?currency=GBP", "/history?currency=", "/history?currency=usd"])
def test_invalid_currency_filter_is_rejected_without_writes(app, database, path):
    # Refuse unsupported selections rather than presenting a misleading empty list.
    with app.test_client() as client:
        response = client.get(path)

    # Preserve the empty store after the rejected read-only request.
    assert response.status_code == 400
    assert "Unsupported portfolio currency." in response.get_data(as_text=True)
    assert database.list_lots() == []


def test_issue_without_purchases_has_no_portfolio_position(app, database, issue):
    # Neither an unknown ISIN nor an orphan shared issue creates an empty position.
    with app.test_client() as client:
        assert client.get("/issues/UA4999999999").status_code == 404
        assert client.get(f'/issues/{issue["isin"]}').status_code == 404

    # Keep shared records reusable while omitting them from both portfolio modes.
    assert get_position_details(database, issue["isin"]) is None
    assert list_positions(database) == []
    assert list_positions(database, closed=True) == []
    assert database.get_issue(issue["id"]) == issue


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
    assert len([event for event in expected if event["planned_date"] == "2027-06-01"]) == 2
    assert [event["id"] for event in events] == [event["id"] for event in database.list_scheduled_payments()]


def test_restart_and_repeated_gets_preserve_list_card_and_database(app, app_config, database, saved_purchase):
    # Capture rendered pages and shared records before reopening the application.
    lot = saved_purchase
    events = database.list_scheduled_payments()
    with app.test_client() as client:
        home = client.get("/").get_data(as_text=True)
        card = client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
        details = client.get(f'/issues/{lot["isin"]}').get_data(as_text=True)
    database.close()

    # Reopen the same temporary file and visit both pages repeatedly without creating anything.
    restarted = create_app(app_config)
    with restarted.test_client() as client:
        for _ in range(2):
            assert client.get("/").get_data(as_text=True) == home
            assert client.get(f'/purchases/{lot["id"]}').get_data(as_text=True) == card
            assert client.get(f'/issues/{lot["isin"]}').get_data(as_text=True) == details
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
    assert list_positions(database) == []


def test_list_and_card_escape_user_supplied_names(app, database, issue_data, lot_data):
    # Save names that must be treated as text in attributes and visible content.
    isin = '<script>alert("isin")</script>'
    bank_name = '<img src=x onerror="alert(1)">'
    issue = database.add_issue(**{**issue_data, "isin": isin})
    lot = database.add_lot(issue_id=issue["id"], bank_id=database.add_bank(bank_name)["id"], **lot_data)

    # Escape identifiers in the portfolio and both identifiers and bank names in the purchase card.
    with app.test_client() as client:
        for path in ("/", f'/purchases/{lot["id"]}'):
            html = client.get(path).get_data(as_text=True)
            assert isin.upper() not in html
            assert bank_name not in html
            assert isin.upper() in unescape(html)
            assert (bank_name in unescape(html)) == (path != "/")
