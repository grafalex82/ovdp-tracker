"""Test storage behavior through the public API without SQL or SQLite objects."""

import pytest

from ovdp_calculator.db import Database, DatabaseError, DataIntegrityError
from ovdp_calculator.money import operation_amount, parse_money


def add_history(database, bank_id, lot_data):
    # Create the purchase and its planned coupon event.
    lot = database.add_lot(bank_id=bank_id, **lot_data)
    payment = database.add_scheduled_payment(lot_id=lot["id"], kind="coupon", planned_date="2024-08-15")

    # Record the received coupon and a partial sale.
    actual = database.add_actual_payment(scheduled_payment_id=payment["id"], actual_date="2024-08-16", amount="795.00")
    sale = database.add_sale(lot_id=lot["id"], sale_date="2025-01-01", sale_price="1050", quantity=3)

    # Return all records so callers can verify persistence and relationships.
    return lot, payment, actual, sale


def assert_empty(database):
    assert database.list_banks() == []
    assert database.list_lots() == []
    assert database.list_scheduled_payments() == []
    assert database.list_actual_payments() == []
    assert database.list_sales() == []


def test_first_start_creates_empty_version_one_database(database, database_path):
    assert database_path.is_file()
    assert database.get_schema_version() == 1
    assert_empty(database)


def test_restart_preserves_complete_history(database, database_path, lot_data):
    # Save a complete history in one transaction.
    with database.transaction() as handle:
        assert handle is None
        bank = database.add_bank("Банк А")
        lot, payment, actual, sale = add_history(database, bank["id"], lot_data)

    # Close the original connection before simulating an application restart.
    database.close()
    restarted = Database(database_path)

    # Verify that every record and relationship survives reopening.
    with restarted as database:
        assert database.get_bank(bank["id"]) == bank
        assert database.get_lot(lot["id"]) == {"id": lot["id"], "bank_id": bank["id"], **lot_data}
        assert database.list_banks() == [bank]
        assert database.list_lots() == [lot]
        assert database.list_scheduled_payments(lot["id"]) == [payment]
        assert database.list_actual_payments(lot["id"]) == [actual]
        assert database.list_sales(lot["id"]) == [sale]


def test_individual_writes_commit_without_explicit_transaction(database, database_path, lot_data):
    # Save each record independently without an enclosing transaction.
    bank = database.add_bank("Банк А")
    lot, payment, actual, sale = add_history(database, bank["id"], lot_data)

    # Reopen storage to observe only changes that were actually committed.
    database.close()
    restarted = Database(database_path)

    # Verify persistence across all independently saved entities.
    with restarted as database:
        assert database.list_banks() == [bank]
        assert database.list_lots() == [lot]
        assert database.list_scheduled_payments() == [payment]
        assert database.list_actual_payments() == [actual]
        assert database.list_sales() == [sale]


def test_bank_names_are_trimmed_and_reused_case_insensitively(database):
    # Create a bank whose input name includes surrounding whitespace.
    bank = database.add_bank("  Банк А  ")

    # Verify normalization and reuse instead of creating a duplicate.
    assert bank["name"] == "Банк А"
    assert database.add_bank(" банк а ") == bank
    assert database.list_banks() == [bank]


@pytest.mark.parametrize("name", ["", " ", "\t\n"])
def test_empty_bank_name_is_rejected(database, name):
    # Reject input that becomes empty after trimming whitespace.
    with pytest.raises(ValueError, match="Bank name must not be empty"):
        database.add_bank(name)

    # Verify that rejected input leaves storage empty.
    assert_empty(database)


@pytest.mark.parametrize("entity", ["lot", "scheduled-payment", "actual-payment", "sale"])
def test_missing_parent_is_rejected_without_exposing_sqlite_errors(database, lot_data, entity):
    # Prepare entity-specific writes that reference a nonexistent parent.
    operations = {
        "lot": lambda: database.add_lot(bank_id=999, **lot_data),
        "scheduled-payment": lambda: database.add_scheduled_payment(
            lot_id=999, kind="coupon", planned_date="2024-08-15"
        ),
        "actual-payment": lambda: database.add_actual_payment(
            scheduled_payment_id=999, actual_date="2024-08-15", amount="800"
        ),
        "sale": lambda: database.add_sale(lot_id=999, sale_date="2024-08-15", sale_price="1050", quantity=1),
    }

    # Check that the public API translates the storage constraint failure.
    with pytest.raises(DataIntegrityError):
        operations[entity]()

    # Verify that the failed write did not leave partial records.
    assert_empty(database)


def test_referenced_bank_cannot_be_deleted(database, lot_data):
    # Create a purchase that keeps its bank referenced.
    bank = database.add_bank("Банк А")
    lot = database.add_lot(bank_id=bank["id"], **lot_data)

    # Reject deletion while a purchase still depends on the bank.
    with pytest.raises(DataIntegrityError):
        database.delete_bank(bank["id"])

    # Preserve both the referenced bank and the purchase.
    assert database.get_bank(bank["id"]) == bank
    assert database.get_lot(lot["id"]) == lot


def test_delete_unreferenced_bank_and_missing_records(database):
    # Verify lookup and deletion behavior for nonexistent identifiers.
    assert database.get_bank(999) is None
    assert database.get_lot(999) is None
    assert database.delete_bank(999) is False
    assert database.delete_lot(999) is False

    # Create and delete an unreferenced bank, then confirm repeated deletion is harmless.
    bank = database.add_bank("Банк А")
    assert database.delete_bank(bank["id"]) is True
    assert database.get_bank(bank["id"]) is None
    assert database.delete_bank(bank["id"]) is False


@pytest.mark.parametrize("duplicate", ["coupon", "redemption", "confirmation"])
def test_payment_uniqueness_and_coupon_with_redemption_on_same_date(database, lot_data, duplicate):
    # Create distinct coupon and redemption events on the same date.
    bank = database.add_bank("Банк А")
    lot, payment, actual, _ = add_history(database, bank["id"], lot_data)
    redemption = database.add_scheduled_payment(lot_id=lot["id"], kind="redemption", planned_date="2024-08-15")

    # Prepare the requested duplicate event or confirmation.
    operations = {
        "coupon": lambda: database.add_scheduled_payment(lot_id=lot["id"], kind="coupon", planned_date="2024-08-15"),
        "redemption": lambda: database.add_scheduled_payment(
            lot_id=lot["id"], kind="redemption", planned_date="2027-06-01"
        ),
        "confirmation": lambda: database.add_actual_payment(
            scheduled_payment_id=payment["id"], actual_date="2024-08-17", amount="800"
        ),
    }

    # Reject the duplicate through the public storage exception.
    with pytest.raises(DataIntegrityError):
        operations[duplicate]()

    # Preserve the original events and confirmed payment.
    assert database.list_scheduled_payments(lot["id"]) == [payment, redemption]
    assert database.list_actual_payments(lot["id"]) == [actual]


def test_delete_lot_removes_only_its_history_and_keeps_bank(database, lot_data):
    # Create two independent purchases of the same issue through one bank.
    bank = database.add_bank("Банк А")
    first_lot, _, _, _ = add_history(database, bank["id"], lot_data)
    second_lot, payment, actual, sale = add_history(database, bank["id"], lot_data)

    # Check that lot-specific queries exclude the neighboring purchase's history.
    assert database.list_scheduled_payments(second_lot["id"]) == [payment]
    assert database.list_actual_payments(second_lot["id"]) == [actual]
    assert database.list_sales(second_lot["id"]) == [sale]

    # Delete one purchase and verify that the other purchase and bank remain intact.
    assert database.delete_lot(first_lot["id"]) is True
    assert database.get_lot(first_lot["id"]) is None
    assert database.list_lots() == [second_lot]
    assert database.list_banks() == [bank]
    assert database.list_scheduled_payments() == [payment]
    assert database.list_actual_payments() == [actual]
    assert database.list_sales() == [sale]

    # Verify that the deleted purchase has no remaining related records.
    assert database.list_scheduled_payments(first_lot["id"]) == []
    assert database.list_actual_payments(first_lot["id"]) == []
    assert database.list_sales(first_lot["id"]) == []


def test_integrity_error_rolls_back_all_related_records(database, lot_data):
    # Fail the grouped write by confirming the same planned event twice.
    with pytest.raises(DataIntegrityError):
        with database.transaction():
            bank = database.add_bank("Банк А")
            _, payment, _, _ = add_history(database, bank["id"], lot_data)
            database.add_actual_payment(scheduled_payment_id=payment["id"], actual_date="2024-08-17", amount="800")

    # Verify that all records from the failed transaction were rolled back.
    assert_empty(database)

    # Verify that a subsequent write can commit outside the failed transaction.
    bank = database.add_bank("Банк Б")
    assert database.list_banks() == [bank]


def test_application_error_rolls_back_before_successful_retry(database, lot_data):
    # Interrupt a complete history write with an application-level exception.
    with pytest.raises(ValueError, match="Operation incomplete"):
        with database.transaction():
            add_history(database, database.add_bank("Банк А")["id"], lot_data)
            raise ValueError("Operation incomplete")

    # Verify the rollback before retrying the same operation.
    assert_empty(database)

    # Retry successfully and verify that the purchase is now committed.
    with database.transaction():
        lot, _, _, _ = add_history(database, database.add_bank("Банк А")["id"], lot_data)
    assert database.list_lots() == [lot]


def test_transaction_keeps_writes_private_until_commit(database, database_path):
    # Open a separate reader before the writer acquires its transaction lock.
    with Database(database_path) as observer:
        # Verify that the reader cannot observe an uncommitted bank.
        with database.transaction():
            assert_empty(database)
            bank = database.add_bank("Банк А")
            assert observer.list_banks() == []

        # Verify that the reader sees the bank after the transaction commits.
        assert observer.list_banks() == [bank]


def test_nested_explicit_transaction_is_rejected_and_outer_write_is_rolled_back(database):
    # Attempt a forbidden nested transaction after making an outer write.
    with pytest.raises(DatabaseError, match="Nested transactions"):
        with database.transaction():
            database.add_bank("Банк А")
            with database.transaction():
                pass

    # Verify that rejecting the nested context also rolls back the outer write.
    assert_empty(database)


def test_closed_instance_cannot_be_used_or_reentered(database):
    # Close the instance twice to verify that cleanup is idempotent.
    database.close()
    database.close()

    # Reject further operations and attempts to reenter the closed instance.
    with pytest.raises(DatabaseError, match="database is closed"):
        database.list_banks()
    with pytest.raises(DatabaseError, match="database is closed"):
        with database:
            pass


def test_exact_monetary_values_survive_storage_restart(database, database_path, lot_data):
    # Store exact cent amounts in every monetary field using both supported decimal separators.
    lot_data.update(nominal="1000.00", purchase_price="1000,01", coupon_amount="80.12", quantity=3)
    bank = database.add_bank("Bank A")
    lot = database.add_lot(bank_id=bank["id"], **lot_data)
    payment = database.add_scheduled_payment(lot_id=lot["id"], kind="coupon", planned_date="2024-08-15")
    database.add_actual_payment(scheduled_payment_id=payment["id"], actual_date="2024-08-15", amount="795,01")
    database.add_sale(lot_id=lot["id"], sale_date="2024-08-16", sale_price="1050,10", quantity=3)
    database.close()

    # Read the saved strings through a fresh connection, without converting through SQLite REAL or float.
    with Database(database_path) as restarted:
        stored = restarted.get_lot(lot["id"])
        assert stored["nominal"] == "1000.00"
        assert stored["purchase_price"] == "1000.01"
        assert stored["coupon_amount"] == "80.12"
        assert restarted.list_actual_payments(lot["id"])[0]["amount"] == "795.01"
        assert restarted.list_sales(lot["id"])[0]["sale_price"] == "1050.10"
        assert str(operation_amount(parse_money(stored["purchase_price"]), stored["quantity"])) == "3000.03"


@pytest.mark.parametrize("field", ["nominal", "purchase_price", "coupon_amount", "sale_price", "amount"])
@pytest.mark.parametrize("value, message", [
    ("not-a-number", "finite decimal string"),
    ("NaN", "finite decimal string"),
    ("Infinity", "finite decimal string"),
    (1000.005, "finite decimal string"),
    ("1000,005", "at most two decimal places"),
    ("80.123", "at most two decimal places"),
    ("1000.010", "at most two decimal places"),
])
def test_invalid_monetary_input_is_rejected_before_storage(database, lot_data, field, value, message):
    # Create a valid parent history for each kind of monetary record.
    bank = database.add_bank("Bank A")
    lot = database.add_lot(bank_id=bank["id"], **lot_data)
    payment = database.add_scheduled_payment(lot_id=lot["id"], kind="coupon", planned_date="2024-08-15")

    # Attempt the invalid write through its public storage method.
    with pytest.raises(ValueError, match=message):
        if field == "sale_price":
            database.add_sale(lot_id=lot["id"], sale_date="2024-08-16", sale_price=value, quantity=3)
        elif field == "amount":
            database.add_actual_payment(scheduled_payment_id=payment["id"], actual_date="2024-08-15", amount=value)
        else:
            database.add_lot(bank_id=bank["id"], **{**lot_data, field: value})

    # Confirm that rejecting a monetary value leaves the valid history intact.
    assert database.list_lots() == [lot]
    assert database.list_sales() == []
    assert database.list_actual_payments() == []


@pytest.mark.parametrize("entity", ["lot", "sale"])
@pytest.mark.parametrize("quantity", [0, -1, 1.5])
def test_stored_quantities_must_be_positive_integers(database, lot_data, entity, quantity):
    # Prepare a valid purchase so a sale can reference an existing lot.
    bank = database.add_bank("Bank A")
    lot = database.add_lot(bank_id=bank["id"], **lot_data)

    # Reject nonpositive and fractional quantities through storage constraints.
    with pytest.raises(DataIntegrityError):
        if entity == "lot":
            database.add_lot(bank_id=bank["id"], **{**lot_data, "quantity": quantity})
        else:
            database.add_sale(lot_id=lot["id"], sale_date="2024-08-16", sale_price="1050.01", quantity=quantity)

    # Preserve the valid purchase without leaving a rejected operation behind.
    assert database.list_lots() == [lot]
    assert database.list_sales() == []
