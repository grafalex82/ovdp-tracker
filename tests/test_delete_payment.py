"""Verify deletion of individual purchase receipts and the resulting ownership history."""

from datetime import date

import pytest

from ovdp_tracker.db import Database, DatabaseError
from ovdp_tracker.payments import PaymentValidationError, confirm_payment, delete_payment
from ovdp_tracker.portfolio import get_lot_details, list_payment_events, list_positions


TODAY = date(2027, 6, 2)


def test_delete_coupon_and_redemption_independently(database, database_path, lot, lot_data):
    # Create two purchases with separate facts for same-day coupon and redemption events.
    neighbor = database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **lot_data)
    coupon = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon",
                                            planned_date="2027-06-01", amount="80")
    redemption = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="redemption",
                                                planned_date="2027-06-01", amount="1000")
    sale = database.add_sale(lot_id=lot["id"], sale_date="2027-05-01", sale_price="1000", quantity=3)
    coupon_actual = confirm_payment(database, lot["id"], coupon["id"],
                                    {"actual_date": "2027-06-01", "amount": "560"}, TODAY)
    redemption_actual = confirm_payment(database, lot["id"], redemption["id"],
                                        {"actual_date": "2027-06-01", "amount": "7000"}, TODAY)
    neighbor_actual = confirm_payment(database, neighbor["id"], coupon["id"],
                                      {"actual_date": "2027-06-01", "amount": "800"}, TODAY)
    calendar = database.list_scheduled_payments(lot["issue_id"])

    # Deleting the coupon restores its overdue status without touching the same-day redemption or neighbor.
    delete_payment(database, lot["id"], coupon_actual["id"])
    purchase = get_lot_details(database, lot["id"], today=TODAY)
    payments = {payment["id"]: payment for payment in purchase["payments"]}
    assert purchase["closed"] and payments[coupon["id"]]["status"] == "overdue"
    assert payments[redemption["id"]]["actual"] == redemption_actual
    assert database.list_actual_payments(neighbor["id"]) == [neighbor_actual]
    overdue = list_payment_events(database, currency="UAH", today=TODAY)["overdue"]
    assert any(item["lot_id"] == lot["id"] and item["id"] == coupon["id"] for item in overdue)
    replacement = confirm_payment(database, lot["id"], coupon["id"],
                                  {"actual_date": "2027-06-02", "amount": "555"}, TODAY)
    assert replacement["amount"] == "555"

    # Removing redemption restores the unsold balance and persists all other records after reopening.
    delete_payment(database, lot["id"], redemption_actual["id"])
    purchase = get_lot_details(database, lot["id"], today=TODAY)
    assert purchase["remaining_quantity"] == 7 and not purchase["closed"]
    assert any(position["isin"] == lot["isin"] for position in list_positions(database, today=TODAY))
    assert next(item for item in purchase["payments"] if item["id"] == redemption["id"])["status"] == "overdue"
    with Database(database_path) as reopened:
        assert reopened.list_scheduled_payments(lot["issue_id"]) == calendar
        assert reopened.list_sales(lot["id"]) == [sale]
        assert reopened.list_actual_payments(lot["id"]) == [replacement]
        assert reopened.list_actual_payments(neighbor["id"]) == [neighbor_actual]


def test_delete_later_coupon_preserves_early_redemption(database, lot):
    # Confirm the later coupon before recording an early redemption.
    coupon = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon",
                                            planned_date="2027-05-01", amount="80")
    redemption = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="redemption",
                                                planned_date="2027-06-01", amount="1000")
    actual = confirm_payment(database, lot["id"], coupon["id"],
                             {"actual_date": "2027-04-01", "amount": "800"}, TODAY)
    redeemed = confirm_payment(database, lot["id"], redemption["id"],
                               {"actual_date": "2027-04-02", "amount": "10000"}, TODAY)

    # The unconfirmed later coupon stays hidden until the redemption itself is removed.
    delete_payment(database, lot["id"], actual["id"])
    purchase = get_lot_details(database, lot["id"], today=TODAY)
    assert purchase["closed"] and coupon["id"] not in {item["id"] for item in purchase["payments"]}
    assert database.list_actual_payments(lot["id"]) == [redeemed]
    delete_payment(database, lot["id"], redeemed["id"])
    payments = {item["id"]: item for item in get_lot_details(database, lot["id"], today=TODAY)["payments"]}
    assert payments[coupon["id"]]["status"] == "overdue"


def test_deleting_redemption_reopens_a_closed_position(database, lot):
    # Close the only purchase after selling three bonds and redeeming the remaining seven.
    redemption = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="redemption",
                                                planned_date="2027-06-01", amount="1000")
    database.add_sale(lot_id=lot["id"], sale_date="2027-05-01", sale_price="1000", quantity=3)
    actual = confirm_payment(database, lot["id"], redemption["id"],
                             {"actual_date": "2027-06-01", "amount": "7000"}, TODAY)
    assert not list_positions(database, today=TODAY)
    assert len(list_positions(database, closed=True, today=TODAY)) == 1

    # The same position becomes active with the unsold remainder after the fact is removed.
    delete_payment(database, lot["id"], actual["id"])
    position, = list_positions(database, today=TODAY)
    assert position["remaining_quantity"] == 7 and not position["closed"]
    assert not list_positions(database, closed=True, today=TODAY)


def test_deleted_future_payment_becomes_pending(database, lot):
    # A receipt entered ahead of its planned date can be removed without making the event overdue.
    coupon = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon",
                                            planned_date="2027-05-01", amount="80")
    today = date(2027, 4, 1)
    actual = confirm_payment(database, lot["id"], coupon["id"],
                             {"actual_date": "2027-04-01", "amount": "800"}, today)
    delete_payment(database, lot["id"], actual["id"])
    payment, = get_lot_details(database, lot["id"], today=today)["payments"]
    assert payment["status"] == "pending" and payment["actual"] is None


def test_delete_rejects_missing_and_foreign_receipts(database, lot, lot_data, payment):
    # Attempt deletion under the wrong purchase and with unknown identifiers through both APIs.
    neighbor = database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **lot_data)
    actual = database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=payment["id"],
                                         actual_date="2024-08-15", amount="800")
    assert not database.delete_actual_payment(actual["id"], lot_id=neighbor["id"])
    assert not database.delete_actual_payment(999999, lot_id=lot["id"])
    for purchase_id, actual_id in ((neighbor["id"], actual["id"]), (lot["id"], 999999), (999999, actual["id"])):
        with pytest.raises(PaymentValidationError, match="invalid fields"):
            delete_payment(database, purchase_id, actual_id)
    assert database.list_actual_payments(lot["id"]) == [actual]


def test_delete_rolls_back_when_history_or_storage_fails(database, lot, payment, monkeypatch):
    # An invalid remaining sale causes history validation to reject and roll back deletion.
    actual = database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=payment["id"],
                                         actual_date="2024-08-15", amount="800")
    sale = database.add_sale(lot_id=lot["id"], sale_date="2024-08-16", sale_price="1000", quantity=11)
    with pytest.raises(PaymentValidationError):
        delete_payment(database, lot["id"], actual["id"])
    assert database.list_actual_payments(lot["id"]) == [actual]
    database.delete_sale(sale["id"], lot_id=lot["id"])

    # A storage error after deletion must roll back the same transaction.
    def fail_after_delete(_lot_id):
        raise DatabaseError("Simulated storage failure.")

    monkeypatch.setattr(database, "list_sales", fail_after_delete)
    with pytest.raises(DatabaseError, match="Simulated storage failure"):
        delete_payment(database, lot["id"], actual["id"])
    assert database.list_actual_payments(lot["id"]) == [actual]
