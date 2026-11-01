"""Exercise deletion of one saved sale while preserving the complete surrounding history."""

from datetime import date

import pytest

from ovdp_tracker.db import Database, DatabaseError
from ovdp_tracker.portfolio import get_lot_details, get_position_details, list_positions


@pytest.fixture
def sale_history(database, lot, lot_data):
    # Create a shared calendar and a partial sale before its coupon date.
    coupon = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date="2025-07-01", amount="80",
    )
    redemption = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="redemption", planned_date=lot["maturity_date"], amount="1000",
    )
    sale = database.add_sale(lot_id=lot["id"], sale_date="2025-03-01", sale_price="1050", quantity=3)

    # Retain a neighboring purchase with its own bank, sale and confirmation of the shared coupon.
    neighbor = database.add_lot(
        issue_id=lot["issue_id"], bank_id=database.add_bank("Bank B")["id"], **{**lot_data, "quantity": 5},
    )
    database.add_sale(lot_id=neighbor["id"], sale_date="2025-04-01", sale_price="1020", quantity=1)
    database.add_actual_payment(
        lot_id=neighbor["id"], scheduled_payment_id=coupon["id"], actual_date="2025-07-02", amount="320",
    )
    return lot, sale, neighbor, coupon, redemption


def snapshot(database):
    return {
        "issues": database.list_issues(), "events": database.list_scheduled_payments(),
        "banks": database.list_banks(), "lots": database.list_lots(),
        "actuals": database.list_actual_payments(), "sales": database.list_sales(),
    }


def deletion_path(lot, sale):
    return f'/purchases/{lot["id"]}/sales/{sale["id"]}/delete'


def test_sale_deletion_is_offered_only_in_editor_with_saved_summary(app, database, sale_history):
    # Open all other entry points without exposing or executing a sale deletion action.
    lot, sale, _, _, _ = sale_history
    saved = snapshot(database)
    with app.test_client() as client:
        for path in ("/", "/history", f'/issues/{lot["isin"]}', f'/purchases/{lot["id"]}',
                     f'/purchases/{lot["id"]}/sales/new'):
            assert "Удалить продажу" not in client.get(path).get_data(as_text=True)
        response = client.get(f'/purchases/{lot["id"]}/sales/{sale["id"]}/edit')
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Удалить продажу" in html and 'id="delete-sale-dialog"' in html
        assert 'src="/static/sale.js"' in html and "data-unsaved-form" in html
        assert f'formaction="{deletion_path(lot, sale)}"' in html
        assert 'form="sale-form" name="confirm" value="delete"' in html
        assert lot["isin"] in html and f'№{lot["id"]} от {lot["purchase_date"]}' in html
        assert "2025-03-01" in html and "1050,00 UAH" in html and "3150,00 UAH" in html
        assert "Удаление необратимо" in html and "Восстановить продажу" in html
        assert 'id="delete-sale-cancel" autofocus>Отмена</button>' in html
        assert client.get(deletion_path(lot, sale)).status_code == 405

    # Viewing the editor and confirmation data must leave every entity unchanged.
    assert snapshot(database) == saved


@pytest.mark.parametrize("confirm", [None, "", "cancel", "1"])
def test_sale_deletion_requires_explicit_confirmation(app, database, sale_history, confirm):
    # Reject deletion without the specific confirmation value, even with valid sale fields.
    lot, sale, _, _, _ = sale_history
    saved = snapshot(database)
    values = {"sale_date": "2025-03-01", "quantity": "3", "sale_price": "1050"}
    if confirm is not None:
        values["confirm"] = confirm
    with app.test_client() as client:
        response = client.post(deletion_path(lot, sale), data=values)

    # Keep the selected editor and its history available after rejecting the request.
    assert response.status_code == 400
    assert "Подтвердите удаление сохранённой продажи." in response.get_data(as_text=True)
    assert snapshot(database) == saved


def test_delete_sale_restores_quantity_and_coupon_without_applying_draft(app, database, database_path, sale_history):
    # Save surrounding records and the expected partial-sale balance.
    lot, sale, neighbor, _, _ = sale_history
    saved = snapshot(database)
    today = date(2026, 11, 1)
    before = get_lot_details(database, lot["id"], today=today)
    assert before["remaining_quantity"] == 7
    assert before["payments"][0]["planned_amount"] == "560.00"

    # Ignore invalid unsaved fields and extra identifiers when deleting the saved operation.
    with app.test_client() as client:
        response = client.post(deletion_path(lot, sale), data={
            "confirm": "delete", "sale_date": "invalid", "quantity": "99999", "sale_price": "not money",
            "lot_id": neighbor["id"], "sale_id": "99999",
        })
        assert response.status_code == 302
        assert response.location == f'/purchases/{lot["id"]}?sale_deleted=1'
        card = client.get(response.location).get_data(as_text=True)
        assert "Продажа удалена. Остаток и ожидаемые выплаты пересчитаны." in card
        assert "3150,00 UAH" not in card
        assert client.get(response.location).status_code == 200
        assert client.post(deletion_path(lot, sale), data={"confirm": "delete"}).status_code == 404

    # Verify persisted recalculation and isolation through a fresh storage connection.
    with Database(database_path) as restarted:
        after = snapshot(restarted)
        assert after == {**saved, "sales": [item for item in saved["sales"] if item["id"] != sale["id"]]}
        details = get_lot_details(restarted, lot["id"], today=today)
        assert details["remaining_quantity"] == 10
        assert details["payments"][0]["planned_amount"] == "800.00"
        assert get_position_details(restarted, lot["isin"], today=today)["remaining_quantity"] == 14


def test_delete_full_sale_reopens_purchase_and_position(app, database, lot):
    # Close an otherwise unredeemed purchase and its entire ISIN position.
    sale = database.add_sale(lot_id=lot["id"], sale_date="2025-03-01", sale_price="1050", quantity=10)
    assert get_lot_details(database, lot["id"])["closed"]
    assert list_positions(database) == []
    assert list_positions(database, closed=True)[0]["isin"] == lot["isin"]

    # Delete from the closed purchase's editor, then expose the restored purchase in the active portfolio.
    with app.test_client() as client:
        assert "Удалить продажу" in client.get(
            f'/purchases/{lot["id"]}/sales/{sale["id"]}/edit'
        ).get_data(as_text=True)
        assert client.post(deletion_path(lot, sale), data={"confirm": "delete"}).status_code == 302
    assert get_lot_details(database, lot["id"])["remaining_quantity"] == 10
    assert list_positions(database)[0]["isin"] == lot["isin"]
    assert list_positions(database, closed=True) == []


@pytest.mark.parametrize("redeemed", [False, True])
def test_delete_sale_preserves_confirmed_amounts_and_dates_without_supplement(
    app, database, sale_history, redeemed,
):
    # Confirm the reduced coupon and optionally redeem the remaining seven bonds.
    lot, sale, _, coupon, redemption = sale_history
    coupon_actual = database.add_actual_payment(
        lot_id=lot["id"], scheduled_payment_id=coupon["id"], actual_date="2025-07-03", amount="560",
    )
    if redeemed:
        database.add_actual_payment(
            lot_id=lot["id"], scheduled_payment_id=redemption["id"], actual_date="2025-08-01", amount="7000",
        )
    actuals = database.list_actual_payments()

    # Remove the sale without rewriting any confirmation or inventing an additional expected payment.
    with app.test_client() as client:
        response = client.post(deletion_path(lot, sale), data={"confirm": "delete"})
    assert response.status_code == 302
    assert database.list_actual_payments() == actuals
    details = get_lot_details(database, lot["id"], today=date(2026, 11, 1))
    assert details["payments"][0]["actual"] == coupon_actual
    assert details["payments"][0]["planned_amount"] == "800.00"
    assert len(details["payments"]) == 2
    assert details["closed"] == redeemed
    assert details["remaining_quantity"] == (0 if redeemed else 10)
    assert [payment["id"] for payment in details["payments"] if payment["actual"] is None] == (
        [] if redeemed else [redemption["id"]]
    )


def test_delete_sale_keeps_other_sales_in_same_purchase(app, database, sale_history):
    # Retain a later sale and confirm that only the selected quantity returns to the purchase.
    lot, sale, _, _, _ = sale_history
    other = database.add_sale(lot_id=lot["id"], sale_date="2025-08-01", sale_price="990", quantity=2)
    saved = snapshot(database)
    with app.test_client() as client:
        assert client.post(deletion_path(lot, sale), data={"confirm": "delete"}).status_code == 302

    # Recalculate from both the retained history and the common unchanged calendar.
    assert snapshot(database) == {**saved, "sales": [item for item in saved["sales"] if item["id"] != sale["id"]]}
    assert database.list_sales(lot["id"]) == [other]
    details = get_lot_details(database, lot["id"], today=date(2026, 11, 1))
    assert details["remaining_quantity"] == 8
    assert details["payments"][0]["planned_amount"] == "800.00"


@pytest.mark.parametrize("target", ["foreign", "missing-sale", "missing-purchase"])
def test_delete_foreign_or_missing_sale_does_not_change_history(app, database, sale_history, target):
    # Construct a mismatched or missing identifier pair while retaining valid neighboring history.
    lot, sale, neighbor, _, _ = sale_history
    targets = {
        "foreign": (neighbor, sale), "missing-sale": (lot, {"id": 999}),
        "missing-purchase": ({"id": 999}, sale),
    }
    saved = snapshot(database)

    # Reject the pair consistently in both the editor and deletion endpoint.
    selected_lot, selected_sale = targets[target]
    path = deletion_path(selected_lot, selected_sale)
    with app.test_client() as client:
        assert client.post(path, data={"confirm": "delete"}).status_code == 404
        assert client.get(path.removesuffix("delete") + "edit").status_code == 404
    assert snapshot(database) == saved


def test_delete_sale_rechecks_remaining_history_atomically(app, database, sale_history):
    # Model inconsistent surviving history through the public storage API.
    lot, sale, _, _, _ = sale_history
    database.add_sale(lot_id=lot["id"], sale_date="2025-08-01", sale_price="1050", quantity=11)
    saved = snapshot(database)

    # Reject deletion when the resulting history would still violate the purchase balance.
    with app.test_client() as client:
        response = client.post(deletion_path(lot, sale), data={"confirm": "delete"})
    assert response.status_code == 400
    assert "превышает доступный остаток" in response.get_data(as_text=True)
    assert snapshot(database) == saved


def test_delete_sale_rolls_back_write_and_retains_unsaved_editor(app, database, sale_history, monkeypatch):
    # Fail after the actual deletion to prove that the enclosing transaction rolls back the write.
    lot, sale, _, _, _ = sale_history
    saved = snapshot(database)
    original_delete = Database.delete_sale

    def fail_after_delete(self, sale_id, *, lot_id):
        original_delete(self, sale_id, lot_id=lot_id)
        raise DatabaseError("Simulated sale deletion failure.")

    monkeypatch.setattr(Database, "delete_sale", fail_after_delete)

    # Preserve the failed editor submission and show the saved sale independently in its confirmation.
    with app.test_client() as client:
        response = client.post(deletion_path(lot, sale), data={
            "confirm": "delete", "sale_date": "2025-02-01", "quantity": "2", "sale_price": "999",
            "_unsaved_baseline": "original-baseline",
        })
    html = response.get_data(as_text=True)
    assert response.status_code == 503
    assert "Не удалось удалить продажу." in html
    assert 'name="quantity" value="2"' in html and 'name="sale_price" value="999"' in html
    assert "original-baseline" in html and 'data-unsaved-error="true"' in html
    assert "2025-03-01" in html and "3150,00 UAH" in html
    assert f'action="/purchases/{lot["id"]}/sales/{sale["id"]}/edit"' in html
    assert snapshot(database) == saved


def test_storage_delete_sale_scopes_ownership_and_commits_independently(database, database_path, sale_history):
    # Reject absent or foreign pairs through the public storage method without changing their history.
    lot, sale, neighbor, _, _ = sale_history
    saved = snapshot(database)
    assert database.delete_sale(sale["id"], lot_id=neighbor["id"]) is False
    assert database.delete_sale(999, lot_id=lot["id"]) is False
    assert snapshot(database) == saved

    # Commit a matching deletion once and preserve it when storage is reopened.
    assert database.delete_sale(sale["id"], lot_id=lot["id"]) is True
    assert database.delete_sale(sale["id"], lot_id=lot["id"]) is False
    with Database(database_path) as restarted:
        assert snapshot(restarted) == {
            **saved, "sales": [item for item in saved["sales"] if item["id"] != sale["id"]],
        }
