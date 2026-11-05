"""Verify explicit receipt corrections, atomic history checks and shared-calendar regressions."""

from contextlib import contextmanager
from datetime import date
from html import unescape

import pytest

from ovdp_tracker.db import Database, DatabaseError
from ovdp_tracker.issues import edit_issue
from ovdp_tracker.payments import PaymentValidationError, confirm_payment, edit_payment
from ovdp_tracker.portfolio import get_lot_details, get_position_details
from ovdp_tracker.purchases import IssueValidationError, issue_form_values

TODAY = date(2025, 7, 2)
CORRECTION = {"actual_date": "2025-07-01", "amount": "790,01"}


@pytest.fixture(autouse=True)
def local_day(monkeypatch):
    # Fix local validation and page balances independently of the execution date.
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return the fixed local date used by correction and portfolio pages."""
            return cls(2025, 7, 2)

    monkeypatch.setattr("ovdp_tracker.web.date", LocalDate)
    monkeypatch.setattr("ovdp_tracker.portfolio.date", LocalDate)


@pytest.fixture
def receipts(database, lot, lot_data):
    # Give two purchases the same calendar and independent confirmed coupons.
    neighbor = database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **{**lot_data, "quantity": 5})
    events = [database.add_scheduled_payment(issue_id=lot["issue_id"], kind=kind, planned_date=day, amount=amount)
              for kind, day, amount in [("coupon", "2025-06-01", "80"), ("coupon", "2026-06-01", "90"),
                                        ("redemption", "2027-06-01", "1000")]]
    actuals = [confirm_payment(database, purchase["id"], events[0]["id"],
                              {"actual_date": "2025-06-30", "amount": amount}, TODAY)
               for purchase, amount in [(lot, "795"), (neighbor, "400")]]
    return lot, neighbor, events, actuals


def snapshot(database):
    return [reader() for reader in (database.list_issues, database.list_scheduled_payments, database.list_lots,
                                   database.list_banks, database.list_actual_payments, database.list_sales)]


def correction_path(lot, actual):
    return f'/purchases/{lot["id"]}/payments/{actual["id"]}/edit'


def test_correction_replaces_receipt_and_preserves_all_other_data(database, database_path, receipts):
    # Move the receipt between months and replace its exact total without inserting another receipt.
    lot, neighbor, events, actuals = receipts
    before = snapshot(database)
    actual = edit_payment(database, lot["id"], actuals[0]["id"], CORRECTION, TODAY)
    assert actual == {**actuals[0], "actual_date": "2025-07-01", "amount": "790.01"}
    assert database.list_actual_payments() == [actual, actuals[1]]
    assert snapshot(database)[:4] == before[:4] and database.list_sales() == before[5]
    assert database.list_actual_payments(neighbor["id"]) == [actuals[1]]

    # Keep the original plan alongside the corrected fact, without an additional expectation for this purchase.
    position = get_position_details(database, lot["isin"], today=TODAY)
    coupon = position["calendar"][0]
    assert coupon["planned_amount"] == "1200.00" and coupon["quantity"] == 15
    assert coupon["purchases"][0]["planned_amount"] == "800.00"
    assert coupon["purchases"][0]["actual"] == actual
    assert position["lots"][0]["payments"][1]["planned_amount"] == "900.00"

    # Reopening storage retains both receipt identities and the complete shared calendar.
    with Database(database_path) as reopened:
        assert reopened.list_actual_payments() == [actual, actuals[1]]
        assert reopened.list_scheduled_payments() == events


def test_storage_correction_autocommits_and_joins_explicit_rollback(database, database_path, receipts):
    # Exercise the public storage update independently of the domain operation.
    lot, neighbor, _, actuals = receipts
    actual = database.update_actual_payment(actuals[0]["id"], lot_id=lot["id"], **CORRECTION)
    with Database(database_path) as reopened:
        assert reopened.get_actual_payment(actual["id"], lot_id=lot["id"]) == actual
        assert reopened.get_actual_payment(actual["id"], lot_id=neighbor["id"]) is None
        assert reopened.update_actual_payment(actual["id"], lot_id=neighbor["id"], **CORRECTION) is None
        assert reopened.update_actual_payment(999, lot_id=lot["id"], **CORRECTION) is None

    # A caller's failed transaction must undo an otherwise valid receipt update.
    with pytest.raises(RuntimeError, match="Simulated correction failure"):
        with database.transaction():
            database.update_actual_payment(actual["id"], lot_id=lot["id"], actual_date="2025-06-30", amount="1")
            raise RuntimeError("Simulated correction failure.")
    assert database.list_actual_payments() == [actual, actuals[1]]


@pytest.mark.parametrize("amount", ["", "0", "-1", "790.010", "790.005", "NaN", "Infinity", "1e3", "1,2.3"])
def test_invalid_corrected_total_preserves_original(database, receipts, amount):
    lot, _, _, actuals = receipts
    before = snapshot(database)
    with pytest.raises(PaymentValidationError) as error:
        edit_payment(database, lot["id"], actuals[0]["id"], {**CORRECTION, "amount": amount}, TODAY)
    assert "amount" in error.value.errors
    assert snapshot(database) == before


@pytest.mark.parametrize("day", ["", "20250701", "2025-02-30", "2024-02-14", "2025-07-03", None])
def test_invalid_corrected_date_preserves_original(database, receipts, day):
    lot, _, _, actuals = receipts
    before = snapshot(database)
    with pytest.raises(PaymentValidationError) as error:
        edit_payment(database, lot["id"], actuals[0]["id"], {**CORRECTION, "actual_date": day}, TODAY)
    assert "actual_date" in error.value.errors
    assert snapshot(database) == before


@pytest.mark.parametrize("day", ["2024-02-15", "2025-07-02"])
def test_correction_allows_inclusive_date_boundaries(database, receipts, day):
    lot, _, _, actuals = receipts
    actual = edit_payment(database, lot["id"], actuals[0]["id"], {**CORRECTION, "actual_date": day}, TODAY)
    assert actual["actual_date"] == day and actual["amount"] == "790.01"


@pytest.mark.parametrize("case", ["missing-purchase", "missing-receipt", "foreign-receipt"])
def test_correction_requires_owned_existing_receipt(database, receipts, case):
    lot, _, _, actuals = receipts
    lot_id = 999 if case == "missing-purchase" else lot["id"]
    actual_id = 999 if case == "missing-receipt" else actuals[case == "foreign-receipt"]["id"]
    before = snapshot(database)
    with pytest.raises(PaymentValidationError) as error:
        edit_payment(database, lot_id, actual_id, CORRECTION, TODAY)
    assert error.value.errors == {"form": "Confirmed payment not found."}
    assert snapshot(database) == before


@pytest.mark.parametrize("case", ["zero-coupon", "no-ownership", "before-purchase"])
def test_correction_rechecks_entitlement(database, receipts, case):
    # Simulate a changed or inconsistent history through the public storage API.
    lot, _, events, actuals = receipts
    if case == "zero-coupon":
        values = issue_form_values(lot, events)
        values["coupons"][0]["amount"] = "0"
        edit_issue(database, lot["issue_id"], values)
    elif case == "no-ownership":
        database.add_sale(lot_id=lot["id"], sale_date="2025-05-01", sale_price="1050", quantity=10)
    else:
        database.update_lot(lot["id"], purchase_date="2025-06-02", purchase_price=lot["purchase_price"],
                            quantity=lot["quantity"], bank_id=lot["bank_id"])
    before = snapshot(database)

    # Reject corrections to a zero coupon or an event that the purchase no longer owns.
    with pytest.raises(PaymentValidationError):
        edit_payment(database, lot["id"], actuals[0]["id"], CORRECTION, TODAY)
    assert snapshot(database) == before


@pytest.mark.parametrize("day, accepted", [("2025-06-30", False), ("2025-07-01", True)])
def test_corrected_redemption_checks_sales_and_same_day_order(database, receipts, day, accepted):
    # Redeem seven bonds after a partial sale and keep a confirmed coupon on the sale date.
    lot, neighbor, events, actuals = receipts
    same_day_coupon = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date="2025-07-01", amount="85"
    )
    coupon = confirm_payment(database, lot["id"], same_day_coupon["id"],
                             {"actual_date": "2025-07-01", "amount": "850"}, TODAY)
    database.add_sale(lot_id=lot["id"], sale_date="2025-07-01", sale_price="1050", quantity=3)
    redemption = confirm_payment(database, lot["id"], events[-1]["id"],
                                 {"actual_date": "2025-07-02", "amount": "7000"}, TODAY)
    before = snapshot(database)

    # Reject redemption before the saved sale but allow coupon, sale and redemption on the same date.
    if not accepted:
        with pytest.raises(PaymentValidationError) as error:
            edit_payment(database, lot["id"], redemption["id"], {"actual_date": day, "amount": "6995"}, TODAY)
        assert error.value.errors == {"form": "Cannot sell bonds after their actual redemption."}
        assert snapshot(database) == before
    else:
        updated = edit_payment(database, lot["id"], redemption["id"], {"actual_date": day, "amount": "6995"}, TODAY)
        details = get_lot_details(database, lot["id"], today=TODAY)
        assert details["closed"] and details["remaining_quantity"] == 0
        assert next(item for item in details["payments"] if item["id"] == same_day_coupon["id"])["actual"] == coupon
        assert updated == {**redemption, "actual_date": day, "amount": "6995"}
        assert database.list_actual_payments(neighbor["id"]) == [actuals[1]]
        assert database.list_sales() == before[5]


def test_closed_purchase_coupon_can_be_corrected(database, receipts):
    lot, _, _, actuals = receipts
    database.add_sale(lot_id=lot["id"], sale_date="2025-06-01", sale_price="1050", quantity=10)
    updated = edit_payment(database, lot["id"], actuals[0]["id"], CORRECTION, TODAY)
    details = get_lot_details(database, lot["id"], today=TODAY)
    assert details["closed"] and details["payments"][0]["actual"] == updated


def test_ownership_and_history_checks_run_under_the_write_lock(database, receipts, monkeypatch):
    # Observe transaction scope without inspecting private connections or issuing SQL.
    lot, _, _, actuals = receipts
    transaction = database.transaction
    get_actual, update_actual = database.get_actual_payment, database.update_actual_payment
    active = False
    checked = []

    @contextmanager
    def tracked_transaction():
        nonlocal active
        with transaction():
            active = True
            try:
                yield
            finally:
                active = False

    def checked_get(*args, **kwargs):
        assert active
        checked.append("ownership")
        return get_actual(*args, **kwargs)

    def checked_update(*args, **kwargs):
        assert active
        checked.append("write")
        return update_actual(*args, **kwargs)

    def checked_history(*args):
        assert active
        checked.append("history")

    # Require ownership, update and proposed-history validation within the same transaction.
    monkeypatch.setattr(database, "transaction", tracked_transaction)
    monkeypatch.setattr(database, "get_actual_payment", checked_get)
    monkeypatch.setattr(database, "update_actual_payment", checked_update)
    monkeypatch.setattr("ovdp_tracker.payments.check_history", checked_history)
    edit_payment(database, lot["id"], actuals[0]["id"], CORRECTION, TODAY)
    assert "ownership" in checked and "write" in checked and "history" in checked


def test_http_editor_navigation_defaults_and_success(app, database, receipts):
    # Resolve the editor from the saved confirmation rather than the shared event or submitted relationship fields.
    lot, neighbor, events, actuals = receipts
    path = correction_path(lot, actuals[0])
    with app.test_client() as client:
        page = client.get(path).get_data(as_text=True)
        assert 'value="2025-06-30"' in page and 'value="795"' in page
        assert '<span aria-current="page">Исправление выплаты</span>' in page
        assert f'href="/issues/{lot["isin"]}"' in page and f'href="/purchases/{lot["id"]}"' in page
        assert 'aria-current="true">Портфель</a>' in page
        assert 'data-unsaved-form data-unsaved-error="false"' in page and 'unsaved-form.js' in page
        assert 'min="2024-02-15" max="2025-07-02"' in page and 'Сохранить исправление' in page
        for source in (f'/purchases/{lot["id"]}', f'/issues/{lot["isin"]}'):
            assert f'href="{path}"' in client.get(source).get_data(as_text=True)
        response = client.post(path, data={
            **CORRECTION, "lot_id": neighbor["id"], "scheduled_payment_id": events[1]["id"],
        })
        assert response.status_code == 302 and response.location == f'/purchases/{lot["id"]}?payment_updated=1'
        for _ in range(2):
            result = client.get(response.location).get_data(as_text=True)
            assert 'Выплата исправлена.' in result and '790,01' in result
            assert 'data-unsaved-form' not in result
        assert client.post(path, data=CORRECTION).status_code == 302
    expected = {**actuals[0], "actual_date": "2025-07-01", "amount": "790.01"}
    assert database.list_actual_payments() == [expected, actuals[1]]


@pytest.mark.parametrize("method", ["get", "post"])
def test_http_rejects_missing_and_foreign_receipts(app, database, receipts, method):
    lot, _, _, actuals = receipts
    before = snapshot(database)
    with app.test_client() as client:
        send = getattr(client, method)
        for path in (correction_path({"id": 999}, actuals[0]), correction_path(lot, {"id": 999}),
                     correction_path(lot, actuals[1])):
            assert send(path, data=CORRECTION).status_code == 404
    assert snapshot(database) == before


def test_http_invalid_fields_preserve_input_and_protected_baseline(app, database, receipts):
    # Keep escaped baseline data and both invalid fields after rejecting the submission.
    lot, _, _, actuals = receipts
    baseline = '<script>"original receipt"</script>'
    before = snapshot(database)
    with app.test_client() as client:
        response = client.post(correction_path(lot, actuals[0]), data={
            "actual_date": "2025-07-03", "amount": "790.010", "_unsaved_baseline": baseline,
        })
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert 'value="2025-07-03"' in html and 'value="790.010"' in html
    assert 'Фактическая дата выплаты не может быть в будущем.' in html
    assert 'Укажите не более двух знаков после разделителя.' in html
    assert 'data-unsaved-error="true"' in html and 'aria-invalid="true"' in html
    assert baseline in unescape(html) and baseline not in html
    assert snapshot(database) == before


def test_http_write_failure_rolls_back_and_preserves_draft(app, database, receipts, monkeypatch):
    # Fail after the actual UPDATE to verify the full correction is rolled back.
    lot, _, _, actuals = receipts
    original = Database.update_actual_payment
    before = snapshot(database)

    def fail_after_update(self, actual_id, **values):
        original(self, actual_id, **values)
        raise DatabaseError("Simulated receipt update failure.")

    # Preserve the rejected draft and its dirty baseline in the reusable form.
    monkeypatch.setattr(Database, "update_actual_payment", fail_after_update)
    with app.test_client() as client:
        response = client.post(correction_path(lot, actuals[0]), data={**CORRECTION, "_unsaved_baseline": "original"})
    html = response.get_data(as_text=True)
    assert response.status_code == 503 and 'Не удалось исправить выплату.' in html
    assert 'value="2025-07-01"' in html and 'value="790,01"' in html
    assert 'data-unsaved-error="true"' in html and 'value="original"' in html
    assert snapshot(database) == before


def test_shared_coupon_correction_preserves_corrected_fact_and_neighbor(database, receipts):
    # Correct the receipt first, then update only its shared unit amount from 80 to 85.
    lot, neighbor, events, actuals = receipts
    actual = edit_payment(database, lot["id"], actuals[0]["id"], CORRECTION, TODAY)
    values = issue_form_values(lot, events)
    values["coupons"][0]["amount"] = "85"
    before = snapshot(database)
    edit_issue(database, lot["issue_id"], values)

    # Retain the corrected receipt, neighboring confirmation, other events and all independent operations.
    assert database.list_actual_payments() == [actual, actuals[1]]
    assert database.list_scheduled_payments() == [{**events[0], "amount": "85"}, *events[1:]]
    assert snapshot(database)[2:] == before[2:]
    assert get_lot_details(database, neighbor["id"])["payments"][0]["planned_amount"] == "425.00"


@pytest.mark.parametrize("confirmed_by_neighbor", [False, True])
def test_removing_confirmed_event_rolls_back_shared_correction(database, receipts, confirmed_by_neighbor):
    # Keep an edited receipt while protecting a calendar event confirmed by either purchase.
    lot, neighbor, events, actuals = receipts
    edit_payment(database, lot["id"], actuals[0]["id"], CORRECTION, TODAY)
    if confirmed_by_neighbor:
        confirm_payment(database, neighbor["id"], events[1]["id"],
                        {"actual_date": "2025-07-01", "amount": "450"}, TODAY)
    values = issue_form_values(lot, events)
    values.update(nominal="1200", currency="EUR")
    values["coupons"].pop(1 if confirmed_by_neighbor else 0)
    before = snapshot(database)

    # Reject every shared change without losing corrected facts or a neighbor's separate confirmation.
    with pytest.raises(IssueValidationError):
        edit_issue(database, lot["issue_id"], values)
    assert snapshot(database) == before
