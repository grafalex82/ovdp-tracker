"""Verify receipt confirmation, historical rights, atomic writes and protected HTTP forms."""

from datetime import date
from html import unescape

import pytest

from ovdp_tracker.db import Database, DatabaseError
from ovdp_tracker.payments import PaymentValidationError, confirm_payment
from ovdp_tracker.portfolio import get_lot_details, get_position_details

TODAY = date(2025, 7, 1)
RECEIPT = {"actual_date": "2025-06-03", "amount": "795,00"}


@pytest.fixture(autouse=True)
def local_day(monkeypatch):
    # Keep validation and rendered portfolio balances independent of the execution date.
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return the fixed local date used by receipt and portfolio pages."""
            return cls(2025, 7, 1)

    monkeypatch.setattr("ovdp_tracker.web.date", LocalDate)
    monkeypatch.setattr("ovdp_tracker.portfolio.date", LocalDate)


@pytest.fixture
def shared_receipts(database, lot, lot_data):
    # Give two purchases independent rights to the same coupon and redemption calendar.
    neighbor = database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **{**lot_data, "quantity": 5})
    coupon = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date="2025-06-01", amount="80"
    )
    later = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date="2026-12-01", amount="85"
    )
    redemption = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="redemption", planned_date=lot["maturity_date"], amount="1000"
    )
    return lot, neighbor, coupon, later, redemption


def storage_snapshot(database):
    return [reader() for reader in (
        database.list_issues, database.list_scheduled_payments, database.list_lots,
        database.list_banks, database.list_actual_payments, database.list_sales,
    )]


def confirmation_path(lot, event):
    return f'/purchases/{lot["id"]}/payments/{event["id"]}/confirm'


def test_confirmation_preserves_shared_plan_neighbor_and_restart(database, database_path, shared_receipts):
    # Confirm a historical coupon below plan, leaving the neighboring purchase unconfirmed.
    lot, neighbor, coupon, later, redemption = shared_receipts
    before = storage_snapshot(database)
    actual = confirm_payment(database, lot["id"], coupon["id"], RECEIPT, TODAY)
    assert actual["amount"] == "795.00" and actual["actual_date"] == "2025-06-03"
    assert actual["lot_id"] == lot["id"] and actual["scheduled_payment_id"] == coupon["id"]
    assert storage_snapshot(database)[:4] == before[:4]
    assert database.list_sales() == before[5]
    assert database.list_actual_payments(neighbor["id"]) == []

    # Retain the full 1200 plan with an independent 795 fact and 400 expectation, without a top-up.
    position = get_position_details(database, lot["isin"], today=TODAY)
    event = next(item for item in position["calendar"] if item["id"] == coupon["id"])
    first, second = event["purchases"]
    assert event["quantity"] == 15 and event["planned_amount"] == "1200.00"
    assert first["planned_amount"] == "800.00" and first["actual"] == actual
    assert second["planned_amount"] == "400.00" and second["actual"] is None
    assert position["lots"][0]["payments"][1]["planned_amount"] == "850.00"

    # Reopen the same temporary file and independently confirm the neighboring purchase.
    with Database(database_path) as reopened:
        assert reopened.list_actual_payments() == [actual]
        other = confirm_payment(reopened, neighbor["id"], coupon["id"], {**RECEIPT, "amount": "400"}, TODAY)
        assert other["id"] != actual["id"] and other["lot_id"] == neighbor["id"]
    assert database.list_scheduled_payments() == [coupon, later, redemption]
    assert database.list_actual_payments(lot["id"]) == [actual]


@pytest.mark.parametrize("values", [{**RECEIPT, "amount": "900"}, {"actual_date": "invalid", "amount": "0"}, {}])
def test_repeated_submission_returns_original_receipt(database, database_path, shared_receipts, values):
    # Repeat from another storage instance, as separate HTTP requests do, without overwriting the original fact.
    lot, neighbor, coupon, _, _ = shared_receipts
    original = confirm_payment(database, lot["id"], coupon["id"], RECEIPT, TODAY)
    with Database(database_path) as reopened:
        assert confirm_payment(reopened, lot["id"], coupon["id"], values, TODAY) == original
    assert database.list_actual_payments() == [original]
    assert database.list_actual_payments(neighbor["id"]) == []


@pytest.mark.parametrize("amount", ["", "0", "-1", "795.000", "795.005", "NaN", "Infinity", "1e3", "1,2.3"])
def test_invalid_total_does_not_write(database, shared_receipts, amount):
    # Reject invalid or imprecise totals without altering any part of the purchase history.
    lot, _, coupon, _, _ = shared_receipts
    before = storage_snapshot(database)
    with pytest.raises(PaymentValidationError) as error:
        confirm_payment(database, lot["id"], coupon["id"], {**RECEIPT, "amount": amount}, TODAY)
    assert "amount" in error.value.errors
    assert storage_snapshot(database) == before


@pytest.mark.parametrize("day", ["", "20250603", "2025-02-30", "2024-02-14", "2025-07-02", None])
def test_invalid_actual_date_does_not_write(database, shared_receipts, day):
    # Require an ISO receipt date within the inclusive purchase-to-today interval.
    lot, _, coupon, _, _ = shared_receipts
    with pytest.raises(PaymentValidationError) as error:
        confirm_payment(database, lot["id"], coupon["id"], {**RECEIPT, "actual_date": day}, TODAY)
    assert "actual_date" in error.value.errors
    assert database.list_actual_payments() == []


@pytest.mark.parametrize("day", ["2024-02-15", "2025-07-01"])
def test_actual_date_boundaries_and_exact_cents_are_allowed(database, shared_receipts, day):
    lot, _, coupon, _, _ = shared_receipts
    actual = confirm_payment(database, lot["id"], coupon["id"], {"actual_date": day, "amount": "795,01"}, TODAY)
    assert actual["actual_date"] == day and actual["amount"] == "795.01"


@pytest.mark.parametrize("case", ["missing-purchase", "missing-event", "foreign-issue", "before-purchase",
                                 "sold-before-event", "zero-coupon", "after-redemption"])
def test_event_requires_purchase_specific_rights(database, shared_receipts, issue_data, case):
    # Prepare a non-entitled event while keeping another purchase of the same issue independent.
    lot, neighbor, coupon, _, redemption = shared_receipts
    lot_id, event_id = lot["id"], coupon["id"]
    if case == "missing-purchase":
        lot_id = 999
    elif case == "missing-event":
        event_id = 999
    elif case == "foreign-issue":
        other = database.add_issue(**{**issue_data, "isin": "UA4000000002"})
        event_id = database.add_scheduled_payment(
            issue_id=other["id"], kind="coupon", planned_date="2025-06-01", amount="80"
        )["id"]
    elif case in ("before-purchase", "zero-coupon"):
        event_id = database.add_scheduled_payment(
            issue_id=lot["issue_id"], kind="coupon",
            planned_date="2024-01-01" if case == "before-purchase" else "2025-05-01",
            amount="80" if case == "before-purchase" else "0",
        )["id"]
    elif case == "sold-before-event":
        database.add_sale(lot_id=lot_id, sale_date="2025-05-01", sale_price="1050", quantity=10)
    else:
        database.add_actual_payment(
            lot_id=lot_id, scheduled_payment_id=redemption["id"], actual_date="2025-05-01", amount="10000"
        )
    before = storage_snapshot(database)

    # Reject confirmation without creating a receipt or changing the neighboring entitlement.
    with pytest.raises(PaymentValidationError):
        confirm_payment(database, lot_id, event_id, RECEIPT, TODAY)
    assert storage_snapshot(database) == before
    assert database.list_actual_payments(neighbor["id"]) == []


@pytest.mark.parametrize("sale_date", ["2025-06-01", "2025-06-02"])
def test_closed_purchase_keeps_its_historical_coupon(database, shared_receipts, sale_date):
    # A same-day or later full sale does not remove the coupon earned before that sale.
    lot, _, coupon, _, _ = shared_receipts
    database.add_sale(lot_id=lot["id"], sale_date=sale_date, sale_price="1050", quantity=10)
    assert get_lot_details(database, lot["id"], today=TODAY)["closed"]
    actual = confirm_payment(database, lot["id"], coupon["id"], RECEIPT, TODAY)
    purchase = get_lot_details(database, lot["id"], today=TODAY)
    assert purchase["closed"] and purchase["remaining_quantity"] == 0
    assert purchase["payments"] == [
        {**coupon, "quantity": 10, "planned_amount": "800.00", "actual": actual, "status": "confirmed"}
    ]


def test_redemption_closes_only_the_unsold_remainder(database, shared_receipts):
    # Redeem seven unsold bonds after a partial sale, leaving the neighboring five-bond purchase active.
    lot, neighbor, coupon, later, redemption = shared_receipts
    database.add_sale(lot_id=lot["id"], sale_date="2025-06-03", sale_price="1050", quantity=3)
    actual = confirm_payment(database, lot["id"], redemption["id"], {**RECEIPT, "amount": "7000"}, TODAY)
    position = get_position_details(database, lot["isin"], today=TODAY)
    first, second = position["lots"]
    assert first["closed"] and first["remaining_quantity"] == 0
    assert first["payments"][-1]["actual"] == actual
    assert first["payments"][-1]["planned_amount"] == "7000.00"
    assert [payment["id"] for payment in first["payments"]] == [coupon["id"], redemption["id"]]
    assert [payment["id"] for payment in second["payments"]] == [coupon["id"], later["id"], redemption["id"]]
    assert not position["closed"] and position["remaining_quantity"] == neighbor["quantity"]


def test_early_redemption_conflicting_with_later_sale_rolls_back(database, shared_receipts):
    # A receipt must not turn an already saved later sale into a sale of redeemed bonds.
    lot, _, _, _, redemption = shared_receipts
    database.add_sale(lot_id=lot["id"], sale_date="2025-06-04", sale_price="1050", quantity=3)
    before = storage_snapshot(database)
    with pytest.raises(PaymentValidationError, match="invalid fields") as error:
        confirm_payment(database, lot["id"], redemption["id"], {**RECEIPT, "amount": "7000"}, TODAY)
    assert error.value.errors == {"form": "Cannot sell bonds after their actual redemption."}
    assert storage_snapshot(database) == before
    assert get_lot_details(database, lot["id"], today=TODAY)["remaining_quantity"] == 7


def test_form_defaults_navigation_and_confirmation_links(app, database, shared_receipts):
    # Present the selected purchase's total, not the unit amount or the combined issue total.
    lot, neighbor, coupon, _, _ = shared_receipts
    path = confirmation_path(lot, coupon)
    with app.test_client() as client:
        response = client.get(path)
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'value="2025-06-01"' in html and 'value="800.00"' in html
        assert 'min="2024-02-15" max="2025-07-01"' in html
        assert '<span aria-current="page">Подтверждение выплаты</span>' in html
        assert f'href="/issues/{lot["isin"]}"' in html and f'href="/purchases/{lot["id"]}"' in html
        assert 'data-unsaved-form data-unsaved-error="false"' in html
        assert "unsaved-form.js" in html and '>Отмена</a>' in html
        assert f'href="{path}"' in client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
        issue = client.get(f'/issues/{lot["isin"]}').get_data(as_text=True)
        assert f'href="{path}"' in issue and f'href="{confirmation_path(neighbor, coupon)}"' in issue
    assert database.list_actual_payments() == []


def test_http_success_repeat_and_refresh_confirm_only_the_selected_pair(app, database, shared_receipts):
    # Ignore forged relationship fields and bind the receipt only to the URL's selected pair.
    lot, neighbor, coupon, _, _ = shared_receipts
    path = confirmation_path(lot, coupon)
    with app.test_client() as client:
        response = client.post(path, data={**RECEIPT, "lot_id": neighbor["id"], "scheduled_payment_id": "999"})
        assert response.status_code == 302 and response.location == f'/purchases/{lot["id"]}?payment_saved=1'
        for _ in range(2):
            page = client.get(response.location).get_data(as_text=True)
            assert "Выплата подтверждена." in page and "795,00" in page
            assert 'data-unsaved-form' not in page and f'href="{path}"' not in page
        original, = database.list_actual_payments()
        assert client.post(path, data={**RECEIPT, "amount": "900"}).status_code == 302
        assert client.get(path).status_code == 302
        issue = client.get(f'/issues/{lot["isin"]}').get_data(as_text=True)
        assert "1200,00" in issue and "795,00" in issue and "400,00" in issue
        assert f'href="{path}"' not in issue and f'href="{confirmation_path(neighbor, coupon)}"' in issue
    assert database.list_actual_payments() == [original]
    assert original["lot_id"] == lot["id"] and original["amount"] == "795.00"


def test_http_validation_preserves_all_input_and_unsaved_baseline(app, database, shared_receipts):
    # Report both fields and preserve the original baseline safely after a rejected submission.
    lot, _, coupon, _, _ = shared_receipts
    baseline = '<script>"original receipt"</script>'
    with app.test_client() as client:
        response = client.post(confirmation_path(lot, coupon), data={
            "actual_date": "2025-07-02", "amount": "795.000", "_unsaved_baseline": baseline,
        })
        html = response.get_data(as_text=True)
        assert response.status_code == 400
        assert "Фактическая дата выплаты не может быть в будущем." in html
        assert "Укажите не более двух знаков после разделителя." in html
        assert 'value="2025-07-02"' in html and 'value="795.000"' in html
        assert 'data-unsaved-error="true"' in html and 'aria-invalid="true"' in html
        assert baseline in unescape(html) and baseline not in html
        assert client.get(f'/purchases/{lot["id"]}').status_code == 200
    assert database.list_actual_payments() == []


def test_http_mid_write_failure_rolls_back_and_keeps_draft(app, database, shared_receipts, monkeypatch):
    # Fail after insertion to verify the complete transaction rolls back while the form retains its draft.
    lot, _, coupon, _, _ = shared_receipts
    before = storage_snapshot(database)
    original = Database.add_actual_payment

    def fail_after_insert(self, **values):
        original(self, **values)
        raise DatabaseError("Simulated receipt storage failure.")

    monkeypatch.setattr(Database, "add_actual_payment", fail_after_insert)
    with app.test_client() as client:
        response = client.post(confirmation_path(lot, coupon), data={**RECEIPT, "_unsaved_baseline": "original"})
        html = response.get_data(as_text=True)
        assert response.status_code == 503 and "Не удалось подтвердить выплату." in html
        assert 'value="795,00"' in html and 'value="2025-06-03"' in html
        assert 'data-unsaved-error="true"' in html and 'value="original"' in html
    assert storage_snapshot(database) == before


def test_http_rechecks_rights_after_opening_form(app, database, shared_receipts):
    # A newly saved historical sale can invalidate an open form's former entitlement.
    lot, _, coupon, _, _ = shared_receipts
    path = confirmation_path(lot, coupon)
    with app.test_client() as client:
        assert client.get(path).status_code == 200
        database.add_sale(lot_id=lot["id"], sale_date="2025-05-01", sale_price="1050", quantity=10)
        assert client.post(path, data=RECEIPT).status_code == 404
    assert database.list_actual_payments() == []


@pytest.mark.parametrize("method", ["get", "post"])
def test_http_rejects_missing_foreign_and_zero_events(app, database, shared_receipts, issue_data, method):
    # Reject direct URLs that have no entitlement, including a foreign issue and a zero coupon.
    lot, _, coupon, _, _ = shared_receipts
    other = database.add_issue(**{**issue_data, "isin": "UA4000000002"})
    foreign = database.add_scheduled_payment(
        issue_id=other["id"], kind="coupon", planned_date="2025-06-01", amount="80"
    )
    zero = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date="2025-05-01", amount="0"
    )
    before = storage_snapshot(database)
    with app.test_client() as client:
        send = getattr(client, method)
        for path in (confirmation_path({"id": 999}, coupon), confirmation_path(lot, {"id": 999}),
                     confirmation_path(lot, foreign)):
            assert send(path, data=RECEIPT).status_code == 404
        assert send(confirmation_path(lot, zero), data=RECEIPT).status_code == 400
        card = client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
        issue = client.get(f'/issues/{lot["isin"]}').get_data(as_text=True)
        assert confirmation_path(lot, zero) not in card and confirmation_path(lot, zero) not in issue
    assert storage_snapshot(database) == before
