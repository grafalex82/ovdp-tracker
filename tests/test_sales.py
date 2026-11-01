"""Exercise sale entry, historical balances and purchase isolation."""

from datetime import date

import pytest

from ovdp_tracker.portfolio import get_lot_details
from ovdp_tracker.sales import SaleValidationError, create_sale


def test_partial_and_full_sale_keep_neighbor_and_history(app, database, lot, lot_data):
    # Give the same issue an independent neighboring purchase and coupon history.
    neighbor = database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **lot_data)
    for day in ("2025-01-01", "2025-07-01"):
        database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date=day, amount="80")

    # Record a partial sale through the form and inspect its effect on each purchase.
    with app.test_client() as client:
        path = f'/purchases/{lot["id"]}/sales/new'
        assert client.get(path).status_code == 200
        response = client.post(path, data={"sale_date": "2025-03-01", "quantity": "3", "sale_price": "1050"})
        assert response.status_code == 302
        card = client.get(response.location).get_data(as_text=True)
        assert "3150,00 UAH" in card
        assert "<dd>7</dd>" in card
        assert client.get(response.location).status_code == 200
    assert len(database.list_sales(lot["id"])) == 1
    assert database.list_sales(neighbor["id"]) == []
    described = get_lot_details(database, lot["id"], today=date(2025, 8, 1))
    assert [payment["planned_amount"] for payment in described["payments"]] == ["800.00", "560.00"]
    assert get_lot_details(database, neighbor["id"], today=date(2025, 8, 1))["remaining_quantity"] == 10

    # Close only the first purchase while retaining both sales and the other purchase.
    create_sale(database, lot["id"], {"sale_date": "2025-07-01", "quantity": "7", "sale_price": "1000"},
                date(2025, 8, 1))
    assert get_lot_details(database, lot["id"], today=date(2025, 8, 1))["closed"]
    assert len(database.list_sales(lot["id"])) == 2
    assert not get_lot_details(database, neighbor["id"], today=date(2025, 8, 1))["closed"]


@pytest.mark.parametrize("values", [
    {"sale_date": "2024-02-14", "quantity": "1", "sale_price": "1000"},
    {"sale_date": "2027-01-01", "quantity": "1", "sale_price": "1000"},
    {"sale_date": "2025-01-01", "quantity": "0", "sale_price": "1000"},
    {"sale_date": "2025-01-01", "quantity": "1.5", "sale_price": "1000"},
    {"sale_date": "2025-01-01", "quantity": "1", "sale_price": "0"},
    {"sale_date": "2025-01-01", "quantity": "1", "sale_price": "1000.010"},
    {"sale_date": "invalid", "quantity": "1", "sale_price": "1000"},
])
def test_invalid_sale_is_rejected_without_writes(database, lot, values):
    with pytest.raises(SaleValidationError):
        create_sale(database, lot["id"], values, date(2026, 1, 1))
    assert database.list_sales() == []


def test_backdated_sale_cannot_invalidate_later_sale(app, database, lot):
    # A backdated operation must leave enough bonds for an already recorded later sale.
    database.add_sale(lot_id=lot["id"], sale_date="2025-06-01", sale_price="1000", quantity=8)
    with app.test_client() as client:
        response = client.post(f'/purchases/{lot["id"]}/sales/new', data={
            "sale_date": "2025-01-01", "quantity": "3", "sale_price": "1050",
        })
    assert response.status_code == 400
    assert "превышает доступный остаток" in response.get_data(as_text=True)
    assert len(database.list_sales(lot["id"])) == 1


@pytest.mark.parametrize("quantity, message", [
    ("0", "Количество должно быть положительным целым числом."),
    ("11", "Количество продажи превышает доступный остаток с учётом истории покупки."),
])
def test_invalid_quantities_use_the_same_error_presentation(app, database, lot, quantity, message):
    # Disable browser validation so both invalid quantities reach the same error display.
    with app.test_client() as client:
        path = f'/purchases/{lot["id"]}/sales/new'
        assert "novalidate" in client.get(path).get_data(as_text=True)
        response = client.post(path, data={"sale_date": "2025-01-01", "quantity": quantity, "sale_price": "1000"})

    # Preserve the submitted quantity and present the error in the page alert without saving.
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert '<p role="alert" class="error">' in html
    assert message in html
    assert f'name="quantity" value="{quantity}"' in html
    assert database.list_sales(lot["id"]) == []


def test_redemption_blocks_later_sale_but_not_sale_on_same_day(database, lot):
    # A confirmed redemption closes the remaining bonds after sales on the same date.
    event = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="redemption", planned_date=lot["maturity_date"], amount="1000"
    )
    database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=event["id"],
                                actual_date="2025-06-01", amount="7000")
    values = {"sale_date": "2025-06-02", "quantity": "3", "sale_price": "1050"}
    with pytest.raises(SaleValidationError):
        create_sale(database, lot["id"], values, date(2025, 6, 2))
    create_sale(database, lot["id"], {**values, "sale_date": "2025-06-01"}, date(2025, 6, 2))
    assert len(database.list_sales(lot["id"])) == 1


def test_sale_quantity_limit(database, lot):
    # Accept the maximum operation size and reject a larger sale without changing history.
    database.update_lot(lot["id"], purchase_date=lot["purchase_date"], purchase_price=lot["purchase_price"],
                        quantity=10000, bank_id=lot["bank_id"])
    values = {"sale_date": "2025-01-01", "quantity": "10001", "sale_price": "1000"}
    with pytest.raises(SaleValidationError, match="10000"):
        create_sale(database, lot["id"], values, date(2025, 2, 1))
    assert database.list_sales(lot["id"]) == []
    create_sale(database, lot["id"], {**values, "quantity": "10000"}, date(2025, 2, 1))
    assert database.list_sales(lot["id"])[0]["quantity"] == 10000


@pytest.mark.parametrize("closure", ["sale", "redemption"])
def test_closed_purchase_cannot_open_sale_form(app, database, lot, closure):
    # Close the purchase through a full sale or a confirmed redemption.
    if closure == "sale":
        database.add_sale(lot_id=lot["id"], sale_date="2025-01-01", sale_price="1000", quantity=lot["quantity"])
    else:
        event = database.add_scheduled_payment(
            issue_id=lot["issue_id"], kind="redemption", planned_date=lot["maturity_date"], amount="1000"
        )
        database.add_actual_payment(
            lot_id=lot["id"], scheduled_payment_id=event["id"], actual_date="2025-01-01", amount="10000"
        )
    before = database.list_sales(lot["id"])

    # Hide the action and redirect direct requests without recording another sale.
    with app.test_client() as client:
        card_path = f'/purchases/{lot["id"]}'
        sale_path = f'{card_path}/sales/new'
        card = client.get(card_path).get_data(as_text=True)
        assert 'Добавить продажу' not in card
        assert 'не осталось облигаций для продажи' in card
        assert client.get(sale_path).location == card_path
        response = client.post(sale_path, data={"sale_date": "2024-12-01", "quantity": "1", "sale_price": "1000"})
        assert response.status_code == 302
        assert response.location == card_path
    assert database.list_sales(lot["id"]) == before
