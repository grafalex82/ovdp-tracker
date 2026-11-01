"""Verify explicit purchase deletion without losing shared issue data or neighboring history."""

from datetime import date

import pytest

from ovdp_tracker.db import Database, DatabaseError
from ovdp_tracker.portfolio import get_lot_details, get_position_details, list_positions


@pytest.fixture
def history(database, lot, lot_data, payment):
    # Give two independent purchases their own sales and confirmations of the same event.
    neighbor = database.add_lot(
        issue_id=lot["issue_id"], bank_id=database.add_bank("Bank B")["id"],
        **{**lot_data, "purchase_price": "1024", "quantity": 5},
    )
    for purchase, quantity, amount in ((lot, 3, "795"), (neighbor, 1, "400")):
        database.add_sale(lot_id=purchase["id"], sale_date="2025-01-01", sale_price="1050", quantity=quantity)
        database.add_actual_payment(
            lot_id=purchase["id"], scheduled_payment_id=payment["id"], actual_date="2024-08-16", amount=amount,
        )

    # Include a future expectation whose total must shrink when the first purchase is deleted.
    database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date="2027-06-01", amount="85",
    )
    database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="redemption", planned_date="2027-06-01", amount="1000",
    )
    return lot, neighbor


def snapshot(database):
    return {
        "issues": database.list_issues(), "banks": database.list_banks(), "lots": database.list_lots(),
        "events": database.list_scheduled_payments(), "sales": database.list_sales(),
        "actuals": database.list_actual_payments(),
    }


def test_delete_preview_and_cancel_preserve_complete_history(app, database, history):
    # Reach deletion through editing while keeping the purchase details free of the destructive action.
    lot, _ = history
    saved = snapshot(database)
    path = f'/purchases/{lot["id"]}/delete'
    with app.test_client() as client:
        details = client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
        assert f'href="{path}"' not in details
        assert "Удалить покупку" not in details
        editing = client.get(f'/purchases/{lot["id"]}/edit').get_data(as_text=True)
        assert f'href="{path}"' in editing
        assert "data-unsaved-form" in editing
        assert "Потребуется отдельное подтверждение." in editing
        response = client.get(path)
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert lot["isin"] in html and "Банк А" in html and lot["purchase_date"] in html
        assert "9500,10 UAH" in html
        assert "Удаление необратимо" in html and "Восстановить историю покупки" in html
        assert 'name="confirm" value="delete"' in html
        assert f'<a class="button" href="/purchases/{lot["id"]}/edit">Отмена</a>' in html
        assert '<span aria-current="page">Удаление покупки</span>' in html
        assert f'<a href="/issues/{lot["isin"]}">{lot["isin"]}</a>' in html
        assert f'<a href="/purchases/{lot["id"]}">Покупка №{lot["id"]}</a>' in html
        assert 'aria-current="true">Портфель</a>' in html
        assert client.get(f'/purchases/{lot["id"]}/edit').status_code == 200

    # Preview, refresh and cancellation must leave both purchases and their full history intact.
    assert snapshot(database) == saved


@pytest.mark.parametrize("values", [{}, {"confirm": ""}, {"confirm": "cancel"}, {"confirm": "1"}])
def test_delete_requires_explicit_confirmation(app, database, history, values):
    # Reject requests that do not select the complete-history deletion action.
    lot, _ = history
    saved = snapshot(database)
    with app.test_client() as client:
        response = client.post(f'/purchases/{lot["id"]}/delete', data=values)

    # Keep the confirmation page available and leave all records unchanged.
    assert response.status_code == 400
    assert "Подтвердите удаление всей истории покупки." in response.get_data(as_text=True)
    assert snapshot(database) == saved


def test_delete_preserves_neighbor_and_recalculates_position(app, database, database_path, history):
    # Save the neighbor's full description and every shared record before deleting the first purchase.
    lot, neighbor = history
    today = date(2026, 11, 1)
    saved = snapshot(database)
    neighbor_details = get_lot_details(database, neighbor["id"], today=today)
    assert get_position_details(database, lot["isin"], today=today)["remaining_quantity"] == 11

    # Commit the selected deletion once, then follow its redirect and revisit the removed purchase.
    with app.test_client() as client:
        response = client.post(f'/purchases/{lot["id"]}/delete', data={"confirm": "delete"})
        assert response.status_code == 302
        assert response.location == "/?deleted=1&currency=UAH"
        page = client.get(response.location).get_data(as_text=True)
        assert "Покупка и её история удалены." in page
        assert lot["isin"] in page
        assert client.get(f'/purchases/{lot["id"]}').status_code == 404
        assert client.post(f'/purchases/{lot["id"]}/delete', data={"confirm": "delete"}).status_code == 404
        assert client.get(f'/purchases/{neighbor["id"]}').status_code == 200

    # Verify persisted deletion, retained shared events and exact remaining purchase expectations.
    with Database(database_path) as restarted:
        assert restarted.get_lot(lot["id"]) is None
        assert restarted.list_lots() == [neighbor]
        assert restarted.list_issues() == saved["issues"]
        assert restarted.list_scheduled_payments() == saved["events"]
        assert restarted.list_banks() == saved["banks"]
        assert restarted.list_sales() == [sale for sale in saved["sales"] if sale["lot_id"] == neighbor["id"]]
        assert restarted.list_actual_payments() == [
            actual for actual in saved["actuals"] if actual["lot_id"] == neighbor["id"]
        ]
        assert get_lot_details(restarted, neighbor["id"], today=today) == neighbor_details
        position = list_positions(restarted, today=today)[0]
        assert position["remaining_quantity"] == 4
        assert [purchase["id"] for purchase in position["lots"]] == [neighbor["id"]]
        assert position["next_coupon"]["planned_amount"] == "340.00"


@pytest.mark.parametrize("currency", ["UAH", "USD", "EUR"])
@pytest.mark.parametrize("closed", [False, True])
def test_delete_last_purchase_removes_position_and_keeps_issue_for_reuse(
    app, database, issue_data, lot_data, currency, closed,
):
    # Create an active or fully sold last purchase with its shared issue and redemption event.
    issue = database.add_issue(**{**issue_data, "currency": currency})
    bank = database.add_bank("Bank A")
    lot = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], **lot_data)
    event = database.add_scheduled_payment(
        issue_id=issue["id"], kind="redemption", planned_date=issue["maturity_date"], amount="1000",
    )
    if closed:
        database.add_sale(lot_id=lot["id"], sale_date="2025-01-01", sale_price="1050", quantity=lot["quantity"])

    # Delete from either state and return to the portfolio in the purchase's own currency.
    with app.test_client() as client:
        response = client.post(f'/purchases/{lot["id"]}/delete', data={"confirm": "delete"})
        assert response.location == f"/?deleted=1&currency={currency}"
        assert "Покупка и её история удалены." in client.get(response.location).get_data(as_text=True)
        for path in (f"/?currency={currency}", f"/history?currency={currency}"):
            assert issue["isin"] not in client.get(path).get_data(as_text=True)
        assert client.get(f'/issues/{issue["isin"]}').status_code == 404
        lookup = client.get(f'/issues/lookup?isin={issue["isin"]}&source=local').get_json()
        assert lookup["existing"] is True and lookup["details_url"] is None
        assert lookup["values"]["currency"] == currency

    # Preserve the orphan issue and calendar so a later purchase can reuse the same data.
    assert list_positions(database, currency=currency) == []
    assert list_positions(database, currency=currency, closed=True) == []
    assert database.list_issues() == [issue]
    assert database.list_scheduled_payments() == [event]
    assert database.list_banks() == [bank]
    assert database.list_lots() == database.list_sales() == database.list_actual_payments() == []


def test_delete_storage_error_preserves_history_and_offers_retry(app, database, history, monkeypatch):
    # Simulate an unavailable write while keeping reads and the selected history available.
    lot, _ = history
    saved = snapshot(database)

    def unavailable(self, lot_id):
        raise DatabaseError("The database operation failed.")

    monkeypatch.setattr(Database, "delete_lot", unavailable)

    # Present a recoverable error on the same confirmation page without losing any history.
    with app.test_client() as client:
        response = client.post(f'/purchases/{lot["id"]}/delete', data={"confirm": "delete"})
    assert response.status_code == 503
    assert "Не удалось удалить покупку." in response.get_data(as_text=True)
    assert 'name="confirm" value="delete"' in response.get_data(as_text=True)
    assert snapshot(database) == saved


@pytest.mark.parametrize("method", ["get", "post"])
def test_delete_missing_purchase_returns_not_found(app, method):
    with app.test_client() as client:
        response = getattr(client, method)("/purchases/999/delete")
    assert response.status_code == 404
