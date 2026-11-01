"""Exercise atomic editing of independent purchase and sale history."""


def test_purchase_edit_checks_history_and_preserves_shared_data(app, database, lot, lot_data):
    # Prepare an independent neighbor, a shared calendar and confirmed payment.
    database.add_scheduled_payment(issue_id=lot["issue_id"], kind="redemption",
                                   planned_date=lot["maturity_date"], amount="1000")
    coupon = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon",
                                            planned_date="2025-01-01", amount="80")
    actual = database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=coupon["id"],
                                         actual_date="2025-01-02", amount="795")
    neighbor = database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **lot_data)
    database.add_sale(lot_id=lot["id"], sale_date="2025-02-01", sale_price="1000", quantity=6)
    database.add_sale(lot_id=lot["id"], sale_date="2025-03-01", sale_price="1000", quantity=4)
    values = {"purchase_date": "2024-02-15", "purchase_price": "1010", "quantity": "10", "bank_name": "New bank"}
    path = f'/purchases/{lot["id"]}/edit'

    # Reject an oversold purchase and shared-field tampering without creating a bank.
    with app.test_client() as client:
        page = client.get(path).get_data(as_text=True)
        assert "Редактирование покупки" in page and "data-unsaved-form" in page
        assert client.post(path, data={**values, "quantity": "9"}).status_code == 400
        assert client.post(path, data={**values, "quantity": "10001"}).status_code == 400
        assert client.post(path, data={**values, "nominal": "2000"}).status_code == 400
        assert client.post(path, data={**values, "purchase_date": "2025-01-02"}).status_code == 400
        assert database.get_lot(lot["id"]) == lot
        assert len(database.list_banks()) == 1
        assert client.post(path, data=values).status_code == 302
        assert "10100,00 UAH" in client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)

    # Confirm neighboring records, calendar and actual receipt remain untouched.
    assert database.get_lot(neighbor["id"]) == neighbor
    assert database.list_actual_payments(lot["id"]) == [actual]
    assert database.get_issue(lot["issue_id"])["nominal"] == "1000"


def test_edit_sale_validates_all_sales_and_keeps_original_on_error(app, database, lot):
    # Include later history and allow editing even after the purchase is fully sold.
    first = database.add_sale(lot_id=lot["id"], sale_date="2025-01-01", sale_price="1000", quantity=6)
    database.add_sale(lot_id=lot["id"], sale_date="2025-02-01", sale_price="1000", quantity=4)
    path = f'/purchases/{lot["id"]}/sales/{first["id"]}/edit'
    values = {"sale_date": "2025-01-01", "sale_price": "1050", "quantity": "6"}
    with app.test_client() as client:
        assert "data-unsaved-form" in client.get(path).get_data(as_text=True)
        assert client.post(path, data={**values, "sale_date": "2024-01-01"}).status_code == 400
        assert client.post(path, data={**values, "quantity": "7"}).status_code == 400
        assert client.post(path, data={**values, "quantity": "10001"}).status_code == 400
        assert database.list_sales(lot["id"])[0] == first
        assert client.post(path, data=values).status_code == 302
        assert "6300,00 UAH" in client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
        assert client.get(f'/purchases/{lot["id"]}/sales/999/edit').status_code == 404
