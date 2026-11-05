"""Verify historical entitlements and the shared calendar's totals and purchase breakdowns."""

from datetime import date
from decimal import localcontext

import pytest

from ovdp_tracker.db import Database
from ovdp_tracker.portfolio import get_lot_details, get_position_details, list_positions


def calendar_row(html, event_id):
    return html.split(f'<tr data-event-id="{event_id}">', 1)[1].split("</tr>", 1)[0]


@pytest.fixture
def shared_payments(database, lot, lot_data):
    # Retain seven bonds in the first purchase and five in the neighboring purchase.
    database.add_sale(lot_id=lot["id"], sale_date="2026-07-01", sale_price="1050", quantity=3)
    neighbor = database.add_lot(
        issue_id=lot["issue_id"], bank_id=lot["bank_id"], **{**lot_data, "quantity": 5}
    )

    # Keep past and zero events in the full calendar alongside two positive upcoming coupons.
    events = [database.add_scheduled_payment(issue_id=lot["issue_id"], kind=kind, planned_date=day, amount=amount)
              for kind, day, amount in [
                  ("coupon", "2026-06-01", "80"), ("coupon", "2026-10-01", "0"),
                  ("coupon", "2026-10-31", "80"), ("coupon", "2026-12-01", "85"),
                  ("redemption", lot["maturity_date"], "1100"),
              ]]
    return lot, neighbor, events


def test_purchase_dates_and_sales_preserve_each_events_own_amount(database, issue_data, lot_data, bank):
    # Buy the same calendar in January and August, selling part of the first purchase after June.
    issue = database.add_issue(**{**issue_data, "maturity_date": "2026-12-01"})
    first = database.add_lot(
        issue_id=issue["id"], bank_id=bank["id"], **{**lot_data, "purchase_date": "2026-01-01"}
    )
    second = database.add_lot(
        issue_id=issue["id"], bank_id=bank["id"], **{**lot_data, "purchase_date": "2026-08-01", "quantity": 5}
    )
    for kind, day, amount in [
        ("coupon", "2026-06-01", "80"), ("coupon", "2026-12-01", "85"), ("redemption", "2026-12-01", "1100"),
    ]:
        database.add_scheduled_payment(issue_id=issue["id"], kind=kind, planned_date=day, amount=amount)
    database.add_sale(lot_id=first["id"], sale_date="2026-07-01", sale_price="1050", quantity=3)

    # Keep the old 800 entitlement and calculate the later coupon independently on seven bonds.
    position = get_position_details(database, issue["isin"], today=date(2026, 10, 31))
    january, august = position["lots"]
    assert [payment["planned_amount"] for payment in january["payments"]] == ["800.00", "595.00", "7700.00"]
    assert [payment["quantity"] for payment in january["payments"]] == [10, 7, 7]
    assert [payment["planned_date"] for payment in august["payments"]] == ["2026-12-01", "2026-12-01"]
    assert [payment["planned_amount"] for payment in august["payments"]] == ["425.00", "5500.00"]
    assert all(payment["planned_date"] >= second["purchase_date"] for payment in august["payments"])
    assert august["schedule"] == january["schedule"]
    assert len([event for event in position["schedule"] if event["kind"] == "coupon"]) == 2
    assert position["remaining_coupon_count"] == 1
    assert [event["quantity"] for event in position["calendar"]] == [10, 12, 12]
    assert [event["planned_amount"] for event in position["calendar"]] == ["800.00", "1020.00", "13200.00"]
    assert [purchase["lot_id"] for purchase in position["calendar"][0]["purchases"]] == [first["id"]]


@pytest.mark.parametrize("confirmed, count", [(0, 2), (1, 2), (2, 1)])
def test_shared_dates_count_once_while_confirmations_preserve_calendar_totals(
    app, database, shared_payments, monkeypatch, confirmed, count,
):
    # Confirm today's shared event independently for each purchase, preserving past and zero coupons.
    first, second, events = shared_payments
    for purchase in (first, second)[:confirmed]:
        database.add_actual_payment(
            lot_id=purchase["id"], scheduled_payment_id=events[2]["id"], actual_date="2026-10-31", amount="395"
        )
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return the fixed local day for portfolio pages."""
            return cls(2026, 10, 31)

    # Aggregate seven and five bonds, counting dates rather than purchases or individual bonds.
    monkeypatch.setattr("ovdp_tracker.portfolio.date", LocalDate)
    position, = list_positions(database)
    assert position["remaining_coupon_count"] == count
    event = position["calendar"][2]
    assert event["planned_date"] == "2026-10-31"
    assert event["quantity"] == 12 and event["planned_amount"] == "960.00"
    assert [purchase["planned_amount"] for purchase in event["purchases"]] == ["560.00", "400.00"]
    assert [purchase["actual"] is not None for purchase in event["purchases"]] == [confirmed > 0, confirmed > 1]
    assert [purchase["lot_id"] for purchase in event["purchases"]] == [first["id"], second["id"]]
    assert len(position["schedule"]) == 5

    # Keep both purchases as vertical list items in one cell of a six-column calendar row.
    with app.test_client() as client:
        home = client.get("/").get_data(as_text=True)
        details = client.get(f'/issues/{first["isin"]}').get_data(as_text=True)
    assert '<th scope="col">Осталось купонов</th>' in home
    assert f"<td>{count}</td>" in home
    assert f"<dd>{count}</dd>" in details
    row = calendar_row(details, events[2]["id"])
    assert row.count("<td") == 6
    assert '<td class="number">12</td>' in row and '<td class="number">960,00</td>' in row
    breakdown = row.rsplit("<td>", 1)[1]
    assert '<ul class="payment-purchases">' in breakdown and breakdown.count("<li ") == 2
    for purchase in (first, second):
        assert f'href="/purchases/{purchase["id"]}"' in breakdown
    assert breakdown.count("Статус: Подтверждено") == confirmed
    assert breakdown.count("Не подтверждено") == 2 - confirmed
    assert "Ближайшая выплата" not in details
    assert "Причитающиеся выплаты покупок</h2>" not in details
    for label in ("Тип выплаты", "На одну облигацию, UAH", "Количество на дату события",
                  "Общая плановая сумма, UAH", "Выплаты покупок"):
        assert label in details
    assert details.count('data-event-id=') == 5


@pytest.mark.parametrize("sold", [0, 3, 10])
def test_same_day_purchase_coupon_sales_and_redemption_order(database, lot, sold):
    # Place purchase, coupon, sale and maturity on one day, inserting redemption before the coupon.
    day = lot["maturity_date"]
    database.update_lot(lot["id"], purchase_date=day, purchase_price=lot["purchase_price"], quantity=10,
                        bank_id=lot["bank_id"])
    redemption = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="redemption", planned_date=day, amount="1100"
    )
    coupon = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date=day, amount="80")
    if sold:
        database.add_sale(lot_id=lot["id"], sale_date=day, sale_price="1050", quantity=sold)

    # Calculate coupons before sales and redemption after sales, without merging the two event kinds.
    details = get_lot_details(database, lot["id"], today=date.fromisoformat(day))
    position = get_position_details(database, lot["isin"], today=date.fromisoformat(day))
    assert details["payments"][0]["id"] == coupon["id"]
    assert details["payments"][0]["quantity"] == 10
    assert details["payments"][0]["planned_amount"] == "800.00"
    if sold < 10:
        assert details["payments"][1]["id"] == redemption["id"]
        assert details["payments"][1]["planned_amount"] == f"{1100 * (10 - sold)}.00"
        assert position["remaining_coupon_count"] == 1
    else:
        assert len(details["payments"]) == 1
        assert position["remaining_coupon_count"] == 0
    assert [event["kind"] for event in position["calendar"]] == ["coupon", "redemption"]
    assert [event["quantity"] for event in position["calendar"]] == [10, 10 - sold]
    assert [event["planned_amount"] for event in position["calendar"]] == ["800.00", f"{1100 * (10 - sold)}.00"]


@pytest.mark.parametrize("case", ["zero-coupon", "zero-amount", "past-only", "fully-sold", "redeemed"])
def test_calendar_preserves_events_even_without_upcoming_entitlements(database, lot, case):
    # Cover maturity-only, zero-value, expired, sold and redeemed positions without invented upcoming payments.
    day = "2025-06-01" if case == "past-only" else lot["maturity_date"]
    redemption = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="redemption", planned_date=lot["maturity_date"], amount="1100"
    )
    if case != "zero-coupon":
        database.add_scheduled_payment(
            issue_id=lot["issue_id"], kind="coupon", planned_date=day, amount="0" if case == "zero-amount" else "80"
        )
    if case == "fully-sold":
        database.add_sale(lot_id=lot["id"], sale_date="2025-01-01", sale_price="1050", quantity=10)
    elif case == "redeemed":
        database.add_actual_payment(
            lot_id=lot["id"], scheduled_payment_id=redemption["id"], actual_date="2026-10-31", amount="11000"
        )

    # Exclude zero and historical coupons from the remaining count while retaining the complete calendar.
    today = date(2027, 6, 2) if case == "past-only" else date(2026, 10, 31)
    position = get_position_details(database, lot["isin"], today=today)
    assert position["remaining_coupon_count"] == 0
    assert position["next_coupon"] is None
    assert len(position["calendar"]) == len(position["schedule"])
    redeemed = position["calendar"][-1]
    assert redeemed["id"] == redemption["id"]
    assert redeemed["planned_amount"] == ("0.00" if case == "fully-sold" else "11000.00")
    if case == "fully-sold":
        assert all(event["quantity"] == 0 and event["purchases"] == [] for event in position["calendar"])
    elif case == "zero-amount":
        coupon = position["calendar"][0]
        assert coupon["quantity"] == 10 and coupon["planned_amount"] == "0.00"
        assert len(coupon["purchases"]) == 1
    elif case == "redeemed":
        assert redeemed["quantity"] == 10 and redeemed["purchases"][0]["actual"]["amount"] == "11000"


def test_calendar_totals_are_exact_and_survive_reopening(database, database_path, lot, lot_data):
    # Multiply a cent-precision coupon for two purchases without using their price or the issue's nominal rate.
    database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **{**lot_data, "quantity": 3})
    database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date="2026-10-31", amount="80.01"
    )
    today = date(2026, 10, 31)
    with localcontext() as context:
        context.prec = 3
        before = get_position_details(database, lot["isin"], today=today)
    assert before["calendar"][0]["planned_amount"] == "1040.13"
    assert before["calendar"][0]["quantity"] == 13
    assert before["remaining_coupon_count"] == 1

    # Recompute identical summaries from the same saved calendar and purchase history after restart.
    with Database(database_path) as reopened:
        assert get_position_details(reopened, lot["isin"], today=today) == before


def test_confirmed_purchase_keeps_its_plan_without_a_top_up(app, database, lot, lot_data, monkeypatch):
    # Confirm the first purchase below its planned amount while leaving its neighbor unconfirmed.
    neighbor = database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **{**lot_data, "quantity": 5})
    coupon = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date="2026-06-01", amount="80"
    )
    actual = database.add_actual_payment(
        lot_id=lot["id"], scheduled_payment_id=coupon["id"], actual_date="2026-06-03", amount="795"
    )

    # Preserve the historical plan of 1200 while displaying 795 as fact for only the first purchase.
    event, = get_position_details(database, lot["isin"], today=date(2026, 10, 31))["calendar"]
    assert event["quantity"] == 15 and event["planned_amount"] == "1200.00"
    first, second = event["purchases"]
    assert first == {
        "lot_id": lot["id"], "quantity": 10, "planned_amount": "800.00", "actual": actual, "status": "confirmed"
    }
    assert second == {
        "lot_id": neighbor["id"], "quantity": 5, "planned_amount": "400.00", "actual": None, "status": "overdue"
    }
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return the fixed local date for the portfolio page."""
            return cls(2026, 10, 31)

    monkeypatch.setattr("ovdp_tracker.portfolio.date", LocalDate)
    with app.test_client() as client:
        row = calendar_row(client.get(f'/issues/{lot["isin"]}').get_data(as_text=True), coupon["id"])
    first_item, second_item = row.split("<li ")[1:]
    assert "1200,00" in row and "800,00" in first_item
    assert "Фактическая выплата: 795,00" in first_item and "Дата выплаты: 2026-06-03" in first_item
    assert "Подтверждено" in first_item and "Не подтверждено" not in first_item
    assert "400,00" in second_item and "Требует подтверждения" in second_item
    assert "Фактическая выплата:" not in second_item and "Дата выплаты:" not in second_item
    assert row.count("<li ") == 2


def test_calendar_displays_zero_entitlements_and_zero_coupons_differently(app, database, lot):
    # Keep an event before ownership and a zero coupon during ownership in the same full calendar.
    before = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date="2023-06-01", amount="80"
    )
    zero = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date="2026-10-31", amount="0"
    )

    # Show no purchase list before ownership, but retain owned quantities and purchase links for the zero coupon.
    with app.test_client() as client:
        html = client.get(f'/issues/{lot["isin"]}').get_data(as_text=True)
    before_row, zero_row = [calendar_row(html, event["id"]) for event in (before, zero)]
    assert '<td class="number">0</td>' in before_row and '<td class="number">0,00</td>' in before_row
    assert "Причитающихся выплат нет." in before_row and "<li " not in before_row
    assert '<td class="number">10</td>' in zero_row and '<td class="number">0,00</td>' in zero_row
    assert f'href="/purchases/{lot["id"]}"' in zero_row
    assert "Нулевой купон — подтверждение не требуется" in zero_row
    assert "Не подтверждено" not in zero_row
    assert "Ближайшая выплата" not in html


def test_empty_calendar_has_a_six_column_empty_state(app, lot):
    # Render a saved purchase with no calendar events through the read-only details route.
    with app.test_client() as client:
        html = client.get(f'/issues/{lot["isin"]}').get_data(as_text=True)

    # Keep the full table structure and an explanatory empty state without an upcoming-payment block.
    assert '<td colspan="6">Расписание пока не заполнено.</td>' in html
    assert "Ближайшая выплата" not in html


def test_closed_purchase_remains_in_the_historical_calendar_totals(database, shared_payments):
    # Close the first purchase after the October coupon, keeping its neighboring purchase active.
    first, second, events = shared_payments
    database.add_sale(lot_id=first["id"], sale_date="2026-11-01", sale_price="1050", quantity=7)

    # Keep the historical twelve-bond coupon while reducing only later entitlements to the remaining five bonds.
    position = get_position_details(database, first["isin"], today=date(2026, 11, 2))
    assert position["lots"][0]["closed"] and not position["lots"][1]["closed"]
    october, december = position["calendar"][2:4]
    assert october["id"] == events[2]["id"] and october["quantity"] == 12
    assert october["planned_amount"] == "960.00"
    assert [purchase["lot_id"] for purchase in october["purchases"]] == [first["id"], second["id"]]
    assert december["quantity"] == 5 and december["planned_amount"] == "425.00"
    assert [purchase["lot_id"] for purchase in december["purchases"]] == [second["id"]]
