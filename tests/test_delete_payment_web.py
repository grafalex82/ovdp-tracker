"""Exercise receipt deletion from the correction page through the HTTP interface."""

import pytest

from ovdp_tracker.db import Database, DatabaseError
from ovdp_tracker.portfolio import get_lot_details


@pytest.fixture
def receipts(database, lot, lot_data):
    # Create a closed purchase with both same-day receipts and a neighboring purchase.
    coupon = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon",
                                            planned_date="2026-06-01", amount="80")
    redemption = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="redemption",
                                                planned_date=lot["maturity_date"], amount="1000")
    sale = database.add_sale(lot_id=lot["id"], sale_date="2026-05-01", sale_price="1000", quantity=3)
    neighbor = database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **lot_data)
    coupon_actual = database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=coupon["id"],
                                                actual_date="2026-06-02", amount="555")
    redemption_actual = database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=redemption["id"],
                                                    actual_date="2026-06-03", amount="7000")
    other = database.add_actual_payment(lot_id=neighbor["id"], scheduled_payment_id=coupon["id"],
                                        actual_date="2026-06-02", amount="800")
    return lot, neighbor, coupon, redemption, coupon_actual, redemption_actual, other, sale


def path(lot, actual):
    return f'/purchases/{lot["id"]}/payments/{actual["id"]}/delete'


def test_deletion_only_in_editor_and_dialog_uses_saved_values(app, receipts):
    # Show the deletion control only in the correction page for either receipt of a closed purchase.
    lot, _, coupon, _, coupon_actual, redemption_actual, _, _ = receipts
    with app.test_client() as client:
        for url in ("/", "/payments", f'/issues/{lot["isin"]}', f'/purchases/{lot["id"]}'):
            assert "Удалить выплату" not in client.get(url).get_data(as_text=True)
        assert "Удалить выплату" not in client.get(
            f'/purchases/{lot["id"]}/payments/{coupon["id"]}/confirm'
        ).get_data(as_text=True)
        for actual, kind, amount in ((coupon_actual, "Купон", "555,00"),
                                     (redemption_actual, "Погашение", "7000,00")):
            html = client.get(path(lot, actual).removesuffix("delete") + "edit").get_data(as_text=True)
            assert 'id="delete-payment-dialog"' in html and 'src="/static/payment.js"' in html
            assert kind in html and actual["actual_date"] in html and f"{amount} UAH" in html
            assert f'formaction="{path(lot, actual)}"' in html
            assert 'form="payment-form" name="confirm" value="delete"' in html
            assert 'id="delete-payment-cancel" autofocus' in html
            assert client.get(path(lot, actual)).status_code == 405


@pytest.mark.parametrize("confirm", [None, "", "cancel", "1"])
def test_unconfirmed_request_preserves_draft_and_receipt(app, database, receipts, confirm):
    # Reject requests without the explicit deletion value and retain the editor's original baseline.
    lot, _, _, _, actual, _, _, _ = receipts
    before = database.list_actual_payments()
    values = {"actual_date": "2026-04-01", "amount": "999", "_unsaved_baseline": "original-baseline"}
    if confirm is not None:
        values["confirm"] = confirm
    with app.test_client() as client:
        response = client.post(path(lot, actual), data=values)
    html = response.get_data(as_text=True)
    assert response.status_code == 400 and "Подтвердите удаление сохранённой выплаты." in html
    assert 'value="2026-04-01"' in html and 'value="999"' in html
    assert "original-baseline" in html and 'data-unsaved-error="true"' in html
    assert "2026-06-02" in html and "555,00 UAH" in html
    assert database.list_actual_payments() == before


@pytest.mark.parametrize("target", ["coupon", "redemption"])
def test_confirmed_deletion_ignores_draft_and_preserves_history(app, database, database_path, receipts, target):
    # Delete one receipt from a closed purchase while submitting deliberately invalid draft values.
    lot, _, coupon, redemption, coupon_actual, redemption_actual, other, sale = receipts
    actual = coupon_actual if target == "coupon" else redemption_actual
    with app.test_client() as client:
        response = client.post(path(lot, actual), data={"confirm": "delete", "actual_date": "bad", "amount": "bad"})
        assert response.status_code == 302 and response.location == f'/purchases/{lot["id"]}?payment_deleted=1'
        card = client.get(response.location).get_data(as_text=True)
        assert "Подтверждение выплаты удалено." in card
        assert client.post(path(lot, actual), data={"confirm": "delete"}).status_code == 404

    # Keep the other event, neighboring receipt, calendar and sale after reopening the database.
    with Database(database_path) as reopened:
        assert reopened.list_scheduled_payments(lot["issue_id"]) == [coupon, redemption]
        assert reopened.list_sales(lot["id"]) == [sale]
        assert reopened.list_actual_payments() == [item for item in (coupon_actual, redemption_actual, other)
                                                   if item["id"] != actual["id"]]
        purchase = get_lot_details(reopened, lot["id"])
        assert purchase["remaining_quantity"] == (0 if target == "coupon" else 7)


def test_missing_or_foreign_receipt_is_not_deleted(app, database, receipts):
    # Refuse a receipt owned by another purchase and unknown identifiers without changing storage.
    lot, neighbor, _, _, actual, _, _, _ = receipts
    before = database.list_actual_payments()
    with app.test_client() as client:
        for purchase, receipt in ((neighbor, actual), (lot, {"id": 9999}), ({"id": 9999}, actual)):
            assert client.post(path(purchase, receipt), data={"confirm": "delete"}).status_code == 404
    assert database.list_actual_payments() == before


def test_storage_failure_rolls_back_and_preserves_draft(app, database, receipts, monkeypatch):
    # Simulate a write failure after the row was deleted inside the domain transaction.
    lot, _, _, _, actual, _, _, _ = receipts
    before = database.list_actual_payments()
    original = Database.delete_actual_payment

    def fail_after_delete(self, actual_id, *, lot_id):
        original(self, actual_id, lot_id=lot_id)
        raise DatabaseError("Simulated payment deletion failure.")

    monkeypatch.setattr(Database, "delete_actual_payment", fail_after_delete)

    # Keep the draft, saved dialog summary and dirty baseline available after the failed operation.
    with app.test_client() as client:
        response = client.post(path(lot, actual), data={"confirm": "delete", "actual_date": "2026-04-01",
                                                        "amount": "999", "_unsaved_baseline": "original-baseline"})
    html = response.get_data(as_text=True)
    assert response.status_code == 503 and "Не удалось удалить выплату." in html
    assert 'value="2026-04-01"' in html and 'value="999"' in html
    assert 'data-unsaved-error="true"' in html and "original-baseline" in html
    assert "2026-06-02" in html and "555,00 UAH" in html
    assert database.list_actual_payments() == before
