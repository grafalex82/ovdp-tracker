"""Verify payment statuses across dates, ownership histories and portfolio pages."""

from datetime import date

import pytest

from ovdp_tracker.payments import confirm_payment, edit_payment
from ovdp_tracker.portfolio import get_lot_details, get_position_details, list_positions


TODAY = date(2026, 7, 1)


@pytest.fixture
def local_today(monkeypatch):
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return the fixed local date for portfolio and payment pages."""
            return cls(2026, 7, 1)

    monkeypatch.setattr("ovdp_tracker.portfolio.date", LocalDate)
    monkeypatch.setattr("ovdp_tracker.web.date", LocalDate)


def add_event(database, lot, day, amount="80", kind="coupon"):
    return database.add_scheduled_payment(issue_id=lot["issue_id"], kind=kind, planned_date=day, amount=amount)


def test_status_boundaries_and_entitlement(database, lot, lot_data):
    # Create events before ownership, yesterday, today and tomorrow, including a zero coupon.
    earlier = add_event(database, lot, "2024-01-01")
    yesterday = add_event(database, lot, "2026-06-30")
    today = add_event(database, lot, "2026-07-01")
    tomorrow = add_event(database, lot, "2026-07-02")
    zero = add_event(database, lot, "2026-06-29", "0")
    database.add_lot(
        issue_id=lot["issue_id"], bank_id=lot["bank_id"], **{**lot_data, "purchase_date": "2026-07-01"}
    )

    # Only entitled, positive and strictly past events are overdue; future coupons retain their count.
    position = get_position_details(database, lot["isin"], today=TODAY)
    first, second = position["lots"]
    statuses = {payment["id"]: payment["status"] for payment in first["payments"]}
    assert statuses == {zero["id"]: "zero", yesterday["id"]: "overdue", today["id"]: "pending",
                        tomorrow["id"]: "pending"}
    assert earlier["id"] not in statuses
    assert not second["has_overdue_payments"]
    assert {payment["id"] for payment in second["payments"]} == {today["id"], tomorrow["id"]}
    assert position["has_overdue_payments"] and position["remaining_coupon_count"] == 2
    assert database.list_actual_payments() == []

    # A fully sold purchase has no right to later events and no overdue marker from those events.
    sold = database.add_lot(
        issue_id=lot["issue_id"], bank_id=lot["bank_id"], **{**lot_data, "purchase_date": "2026-06-28"}
    )
    database.add_sale(lot_id=sold["id"], sale_date="2026-06-29", sale_price="1000", quantity=10)
    after = get_lot_details(database, sold["id"], today=date(2026, 7, 3))
    assert after["closed"] and not after["has_overdue_payments"]
    assert {payment["id"] for payment in after["payments"]} == {zero["id"]}


def test_partial_confirmation_and_correction_update_views(app, database, lot, lot_data, local_today):
    # The two purchases have independent rights to the same overdue coupon.
    neighbor = database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **{**lot_data, "quantity": 5})
    coupon = add_event(database, lot, "2026-06-30")
    path = f'/purchases/{lot["id"]}/payments/{coupon["id"]}/confirm'
    with app.test_client() as client:
        assert "Требует подтверждения" in client.get("/").get_data(as_text=True)
        assert "требующие подтверждения" in client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
        assert client.post(path, data={"actual_date": "2026-07-01", "amount": "795"}).status_code == 302
        actual, = database.list_actual_payments(lot["id"])
        first = client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
        issue = client.get(f'/issues/{lot["isin"]}').get_data(as_text=True)
        assert "Подтверждено" in first and "требующие подтверждения" not in first
        assert "795,00" in issue and "400,00" in issue and "Требует подтверждения" in issue
        assert "Требует подтверждения" in client.get("/").get_data(as_text=True)

        # Correct the receipt without changing the other purchase's overdue status.
        edit_path = f'/purchases/{lot["id"]}/payments/{actual["id"]}/edit'
        assert client.post(edit_path, data={"actual_date": "2026-06-30", "amount": "790"}).status_code == 302
        assert "790,00" in client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
        other_path = f'/purchases/{neighbor["id"]}/payments/{coupon["id"]}/confirm'
        assert client.post(other_path, data={"actual_date": "2026-07-01", "amount": "400"}).status_code == 302
        assert "Требует подтверждения" not in client.get("/").get_data(as_text=True)
        assert "требующие подтверждения" not in client.get(f'/issues/{lot["isin"]}').get_data(as_text=True)


def test_redemption_sale_and_historical_coupons(database, lot, lot_data):
    # A planned maturity does not close a purchase; an actual redemption closes only its unsold remainder.
    neighbor = database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **{**lot_data, "quantity": 5})
    coupon = add_event(database, lot, "2026-06-01")
    redemption = add_event(database, lot, "2027-06-01", "1000", "redemption")
    database.add_sale(lot_id=lot["id"], sale_date="2026-06-02", sale_price="1000", quantity=3)
    assert get_lot_details(database, lot["id"], today=date(2027, 6, 2))["remaining_quantity"] == 7
    actual = confirm_payment(database, lot["id"], redemption["id"],
                             {"actual_date": "2026-06-30", "amount": "7000"}, TODAY)
    position = get_position_details(database, lot["isin"], today=TODAY)
    first, second = position["lots"]
    assert first["closed"] and first["payments"][-1]["actual"] == actual
    assert first["payments"][-1]["quantity"] == 7
    assert not position["closed"] and position["remaining_quantity"] == 5
    assert first["has_overdue_payments"] and second["has_overdue_payments"]
    edit_payment(database, lot["id"], actual["id"], {"actual_date": "2026-07-01", "amount": "6999"}, TODAY)
    database.add_sale(lot_id=neighbor["id"], sale_date="2026-06-02", sale_price="1000", quantity=5)
    closed, = list_positions(database, closed=True, today=TODAY)
    assert closed["has_overdue_payments"] and closed["remaining_quantity"] == 0
    assert all(any(item["id"] == coupon["id"] and item["status"] == "overdue" for item in purchase["payments"])
               for purchase in closed["lots"])


def test_early_redemption_preserves_same_day_and_confirmed_later_coupon(database, lot):
    # Record one later coupon before confirming an early redemption, leaving another later coupon unconfirmed.
    same_day = add_event(database, lot, "2026-06-30")
    later = add_event(database, lot, "2026-07-02")
    saved = add_event(database, lot, "2026-07-03")
    redemption = add_event(database, lot, "2027-06-01", "1000", "redemption")
    actual = confirm_payment(database, lot["id"], saved["id"], {"actual_date": "2026-06-29", "amount": "795"}, TODAY)
    confirm_payment(database, lot["id"], redemption["id"], {"actual_date": "2026-06-30", "amount": "10000"}, TODAY)
    payments = {payment["id"]: payment for payment in get_lot_details(database, lot["id"], today=TODAY)["payments"]}
    assert payments[same_day["id"]]["status"] == "overdue"
    assert later["id"] not in payments
    assert payments[saved["id"]]["actual"] == actual and payments[saved["id"]]["status"] == "confirmed"


def test_closed_portfolio_marks_unconfirmed_historical_coupon(app, database, lot, local_today):
    # Preserve the coupon earned on the day of a full sale and expose it from the closed portfolio.
    coupon = add_event(database, lot, "2026-06-30")
    database.add_sale(lot_id=lot["id"], sale_date="2026-06-30", sale_price="1000", quantity=10)
    with app.test_client() as client:
        assert "Требует подтверждения" in client.get("/history").get_data(as_text=True)
        assert "требующие подтверждения" in client.get(f'/issues/{lot["isin"]}').get_data(as_text=True)
        assert "Требует подтверждения" in client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
        path = f'/purchases/{lot["id"]}/payments/{coupon["id"]}/confirm'
        assert client.post(path, data={"actual_date": "2026-07-01", "amount": "800"}).status_code == 302
        assert "Требует подтверждения" not in client.get("/history").get_data(as_text=True)
