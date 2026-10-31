"""Test storage behavior through the public API without SQL or SQLite objects."""

import pytest

from ovdp_calculator.db import Database, DatabaseError, DataIntegrityError
from ovdp_calculator.money import operation_amount, parse_money


def add_history(database, issue_data, lot_data):
    # Store the shared issue and its purchase with a coupon and a redemption.
    bank = database.add_bank("Банк А")
    issue = database.add_issue(**issue_data)
    lot = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], **lot_data)
    event = database.add_scheduled_payment(amount="80", issue_id=issue["id"], kind="coupon", planned_date="2024-08-15")
    database.add_scheduled_payment(
        amount="1000", issue_id=issue["id"], kind="redemption", planned_date=issue["maturity_date"]
    )

    # Record actual amounts and a sale independently of the shared plan.
    database.add_actual_payment(
        lot_id=lot["id"], scheduled_payment_id=event["id"], actual_date="2024-08-16", amount="795.00"
    )
    database.add_sale(lot_id=lot["id"], sale_date="2025-01-01", sale_price="1050", quantity=3)
    return lot, event


def snapshot(database):
    return {
        "banks": database.list_banks(), "issues": database.list_issues(), "lots": database.list_lots(),
        "events": database.list_scheduled_payments(), "actuals": database.list_actual_payments(),
        "sales": database.list_sales(),
    }


def assert_empty(database):
    assert all(records == [] for records in snapshot(database).values())


def test_first_start_creates_empty_version_three_database(database, database_path):
    assert database_path.is_file()
    assert database.get_schema_version() == 3
    assert_empty(database)


@pytest.mark.parametrize("grouped", [False, True])
def test_restart_preserves_complete_history(database, database_path, issue_data, lot_data, grouped):
    # Save all records independently or in an explicit transaction.
    if grouped:
        with database.transaction() as handle:
            assert handle is None
            lot, _ = add_history(database, issue_data, lot_data)
    else:
        lot, _ = add_history(database, issue_data, lot_data)
    saved = snapshot(database)
    database.close()

    # Verify every entity and relationship through a new connection.
    with Database(database_path) as restarted:
        assert snapshot(restarted) == saved
        assert restarted.get_issue(lot["issue_id"]) == saved["issues"][0]
        assert restarted.get_issue_by_isin(" UA4000000001 ") == saved["issues"][0]
        assert restarted.get_bank(lot["bank_id"]) == saved["banks"][0]
        assert restarted.get_lot(lot["id"]) == lot
        assert restarted.list_scheduled_payments(lot["issue_id"]) == saved["events"]
        assert restarted.list_actual_payments(lot["id"]) == saved["actuals"]
        assert restarted.list_sales(lot["id"]) == saved["sales"]


def test_bank_names_are_trimmed_and_reused_case_insensitively(database):
    # Create a bank with surrounding whitespace.
    bank = database.add_bank("  Банк А  ")

    # Reuse its normalized name instead of creating a duplicate.
    assert bank["name"] == "Банк А"
    assert database.add_bank(" банк а ") == bank
    assert database.list_banks() == [bank]


@pytest.mark.parametrize("name", ["", " ", "\t\n"])
def test_empty_bank_name_is_rejected(database, name):
    # Reject names that become empty after trimming.
    with pytest.raises(ValueError, match="Bank name must not be empty"):
        database.add_bank(name)

    # Leave no partial record after invalid input.
    assert_empty(database)


@pytest.mark.parametrize("field, value", [
    ("currency", "USD"), ("nominal", "500"), ("nominal_rate", "25"), ("maturity_date", "2028-01-20"),
    ("isin", " UA4000000001 "),
])
def test_isin_alone_is_unique_even_when_other_fields_differ(database, issue, issue_data, field, value):
    # Attempt to store another version of the same ISIN.
    with pytest.raises(DataIntegrityError):
        database.add_issue(**{**issue_data, field: value})

    # Preserve the original shared record without creating a variant.
    assert database.list_issues() == [issue]


@pytest.mark.parametrize("field, value", [("currency", "GBP"), ("isin", " ")])
def test_issue_constraints_reject_invalid_records(database, issue_data, field, value):
    # Reject unsupported currencies and empty identifiers.
    with pytest.raises(DataIntegrityError):
        database.add_issue(**{**issue_data, field: value})

    # Preserve empty storage after the rejected insertion.
    assert_empty(database)


@pytest.mark.parametrize("entity", ["lot-bank", "lot-issue", "event", "actual-lot", "actual-event", "sale"])
def test_missing_parent_is_rejected_without_exposing_sqlite_errors(database, lot, lot_data, entity):
    # Prepare writes with a missing parent and otherwise valid references.
    operations = {
        "lot-bank": lambda: database.add_lot(issue_id=lot["issue_id"], bank_id=999, **lot_data),
        "lot-issue": lambda: database.add_lot(issue_id=999, bank_id=lot["bank_id"], **lot_data),
        "event": lambda: database.add_scheduled_payment(
            amount="80", issue_id=999, kind="coupon", planned_date="2024-08-15"
        ),
        "actual-lot": lambda: database.add_actual_payment(
            lot_id=999, scheduled_payment_id=999, actual_date="2024-08-15", amount="800"
        ),
        "actual-event": lambda: database.add_actual_payment(
            lot_id=lot["id"], scheduled_payment_id=999, actual_date="2024-08-15", amount="800"
        ),
        "sale": lambda: database.add_sale(lot_id=999, sale_date="2024-08-15", sale_price="1050", quantity=1),
    }
    saved = snapshot(database)

    # Translate the constraint failure and leave the existing records unchanged.
    with pytest.raises(DataIntegrityError):
        operations[entity]()
    assert snapshot(database) == saved


def test_referenced_bank_cannot_be_deleted(database, lot):
    # Reject deletion while a purchase still references its bank.
    with pytest.raises(DataIntegrityError):
        database.delete_bank(lot["bank_id"])

    # Preserve the bank and its purchase.
    assert database.get_bank(lot["bank_id"]) is not None
    assert database.get_lot(lot["id"]) == lot


def test_delete_unreferenced_bank_and_missing_records(database):
    # Check absent records and empty filtered lists.
    assert database.get_bank(999) is None
    assert database.get_issue(999) is None
    assert database.get_issue_by_isin("missing") is None
    assert database.get_lot(999) is None
    assert database.delete_bank(999) is False
    assert database.delete_lot(999) is False
    assert database.list_scheduled_payments(999) == []
    assert database.list_actual_payments(999) == []
    assert database.list_sales(999) == []

    # Delete an unreferenced bank and safely repeat its deletion.
    bank = database.add_bank("Банк А")
    assert database.delete_bank(bank["id"]) is True
    assert database.get_bank(bank["id"]) is None
    assert database.delete_bank(bank["id"]) is False


@pytest.mark.parametrize("duplicate", ["coupon", "redemption", "confirmation"])
def test_payment_uniqueness_and_coupon_with_redemption_on_same_date(database, lot, duplicate):
    # Store a final coupon and redemption with one purchase confirmation.
    fields = {"issue_id": lot["issue_id"], "planned_date": lot["maturity_date"]}
    coupon = database.add_scheduled_payment(amount="80", **fields, kind="coupon")
    redemption = database.add_scheduled_payment(amount="1000", **fields, kind="redemption")
    actual_fields = {
        "lot_id": lot["id"], "scheduled_payment_id": coupon["id"],
        "actual_date": "2027-06-03", "amount": "795",
    }
    actual = database.add_actual_payment(**actual_fields)
    operations = {
        "coupon": lambda: database.add_scheduled_payment(amount="80", **fields, kind="coupon"),
        "redemption": lambda: database.add_scheduled_payment(amount="1000", **fields, kind="redemption"),
        "confirmation": lambda: database.add_actual_payment(**actual_fields),
    }

    # Reject duplicates while preserving the two distinct event kinds.
    with pytest.raises(DataIntegrityError):
        operations[duplicate]()
    assert database.list_scheduled_payments(lot["issue_id"]) == [coupon, redemption]
    assert database.list_actual_payments(lot["id"]) == [actual]


@pytest.mark.parametrize("kind, day", [
    ("invalid", "2027-06-01"), ("redemption", "2027-05-31"), ("coupon", "2027-06-02"),
])
def test_event_constraints_reject_invalid_kind_or_maturity_conflict(database, issue, kind, day):
    # Reject unknown event kinds and dates conflicting with shared maturity.
    with pytest.raises(DataIntegrityError):
        database.add_scheduled_payment(
            amount="1000" if kind == "redemption" else "80", issue_id=issue["id"], kind=kind, planned_date=day
        )

    # Leave the calendar unchanged after a rejected event.
    assert database.list_scheduled_payments() == []


def test_two_purchases_share_calendar_and_keep_confirmations_independent(database, lot, payment, lot_data):
    # Add another purchase with independent date, price, quantity and bank.
    second = database.add_lot(
        issue_id=lot["issue_id"], bank_id=database.add_bank("Банк Б")["id"],
        **{**lot_data, "purchase_date": "2024-08-01", "purchase_price": "1024", "quantity": 5},
    )
    first_actual = database.add_actual_payment(
        lot_id=lot["id"], scheduled_payment_id=payment["id"], actual_date="2024-08-16", amount="795"
    )
    assert database.list_actual_payments(second["id"]) == []

    # Confirm the same event for the second purchase without duplicating the calendar.
    second_actual = database.add_actual_payment(
        lot_id=second["id"], scheduled_payment_id=payment["id"], actual_date="2024-08-17", amount="400"
    )
    assert second["issue_id"] == lot["issue_id"]
    assert len(database.list_issues()) == 1
    assert database.list_scheduled_payments(second["issue_id"]) == [payment]
    assert database.list_actual_payments(lot["id"]) == [first_actual]
    assert database.list_actual_payments(second["id"]) == [second_actual]
    assert second["purchase_price"] != lot["purchase_price"]
    assert second["bank_id"] != lot["bank_id"]


def test_confirmation_cannot_reference_another_issue(database, lot, issue_data):
    # Create a foreign issue event on a date valid for the purchase.
    foreign = database.add_issue(**{**issue_data, "isin": "UA4000000002"})
    event = database.add_scheduled_payment(
        amount="80", issue_id=foreign["id"], kind="coupon", planned_date="2024-08-15"
    )
    saved = snapshot(database)

    # Reject the mismatched relationship and preserve every entity.
    with pytest.raises(DataIntegrityError):
        database.add_actual_payment(
            lot_id=lot["id"], scheduled_payment_id=event["id"], actual_date="2024-08-16", amount="795"
        )
    assert snapshot(database) == saved


def test_delete_purchases_keeps_issue_calendar_and_neighbor_history(database, lot, payment, lot_data):
    # Give two purchases their own confirmations and sales against one shared event.
    second = database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **lot_data)
    for purchase in (lot, second):
        database.add_actual_payment(
            lot_id=purchase["id"], scheduled_payment_id=payment["id"], actual_date="2024-08-16", amount="795"
        )
        database.add_sale(lot_id=purchase["id"], sale_date="2025-01-01", sale_price="1050", quantity=3)
    neighbor_actuals = database.list_actual_payments(second["id"])
    neighbor_sales = database.list_sales(second["id"])
    saved_issue = database.get_issue(lot["issue_id"])

    # Delete only one purchase's own history, keeping its common event and neighbor.
    assert database.delete_lot(lot["id"]) is True
    assert database.get_lot(lot["id"]) is None
    assert database.list_lots() == [second]
    assert database.list_actual_payments() == neighbor_actuals
    assert database.list_sales() == neighbor_sales
    assert database.list_actual_payments(lot["id"]) == []
    assert database.list_sales(lot["id"]) == []
    assert database.list_scheduled_payments() == [payment]

    # Delete the last purchase without erasing the issue, calendar or bank.
    assert database.delete_lot(second["id"]) is True
    assert database.list_lots() == []
    assert database.list_actual_payments() == []
    assert database.list_sales() == []
    assert database.list_issues() == [saved_issue]
    assert database.list_scheduled_payments() == [payment]
    assert database.get_bank(lot["bank_id"]) is not None


@pytest.mark.parametrize("failure", ["integrity", "application"])
def test_related_records_roll_back_before_successful_retry(database, issue_data, lot_data, failure):
    # Interrupt a history write through a constraint or an application error.
    expected = DataIntegrityError if failure == "integrity" else ValueError
    with pytest.raises(expected):
        with database.transaction():
            lot, event = add_history(database, issue_data, lot_data)
            if failure == "application":
                raise ValueError("Operation incomplete.")
            database.add_actual_payment(
                lot_id=lot["id"], scheduled_payment_id=event["id"], actual_date="2024-08-17", amount="800"
            )

    # Retry after verifying that no partial entity survived.
    assert_empty(database)
    with database.transaction():
        lot, _ = add_history(database, issue_data, lot_data)
    assert database.list_lots() == [lot]


def test_transaction_keeps_writes_private_until_commit(database, database_path):
    # Open a reader before acquiring the writer's transaction lock.
    with Database(database_path) as observer:
        # Hide uncommitted writes from the second connection.
        with database.transaction():
            assert_empty(database)
            bank = database.add_bank("Банк А")
            assert observer.list_banks() == []

        # Expose the bank only after commit.
        assert observer.list_banks() == [bank]


def test_nested_explicit_transaction_is_rejected_and_outer_write_is_rolled_back(database):
    # Reject nesting after an outer write has already started.
    with pytest.raises(DatabaseError, match="Nested transactions"):
        with database.transaction():
            database.add_bank("Банк А")
            with database.transaction():
                pass

    # Roll back the outer write when the nested context fails.
    assert_empty(database)


def test_closed_instance_cannot_be_used_or_reentered(database):
    # Verify idempotent cleanup.
    database.close()
    database.close()

    # Reject operations and attempts to reenter a closed instance.
    with pytest.raises(DatabaseError, match="database is closed"):
        database.list_banks()
    with pytest.raises(DatabaseError, match="database is closed"):
        with database:
            pass


def test_exact_monetary_values_survive_storage_restart(database, database_path, issue_data, lot_data):
    # Store cents exactly in every monetary entity with both decimal separators.
    issue = database.add_issue(**{**issue_data, "nominal": "1000.00"})
    lot = database.add_lot(
        issue_id=issue["id"], bank_id=database.add_bank("Bank A")["id"],
        **{**lot_data, "purchase_price": "1000,01", "quantity": 3},
    )
    event = database.add_scheduled_payment(
        amount="80,12", issue_id=issue["id"], kind="coupon", planned_date="2024-08-15"
    )
    database.add_actual_payment(
        lot_id=lot["id"], scheduled_payment_id=event["id"], actual_date="2024-08-15", amount="795,01"
    )
    database.add_sale(lot_id=lot["id"], sale_date="2024-08-16", sale_price="1050,10", quantity=3)
    database.close()

    # Reopen the file without converting through SQLite REAL or float.
    with Database(database_path) as restarted:
        stored = restarted.get_lot(lot["id"])
        assert stored["nominal"] == "1000.00"
        assert stored["purchase_price"] == "1000.01"
        assert restarted.list_scheduled_payments()[0]["amount"] == "80.12"
        assert restarted.list_actual_payments(lot["id"])[0]["amount"] == "795.01"
        assert restarted.list_sales(lot["id"])[0]["sale_price"] == "1050.10"
        assert str(operation_amount(parse_money(stored["purchase_price"]), stored["quantity"])) == "3000.03"


@pytest.mark.parametrize("field", ["nominal", "purchase_price", "event_amount", "sale_price", "amount"])
@pytest.mark.parametrize("value, message", [
    ("not-a-number", "finite decimal string"), ("NaN", "finite decimal string"),
    ("Infinity", "finite decimal string"), (1000.005, "finite decimal string"),
    ("1000,005", "at most two decimal places"), ("80.123", "at most two decimal places"),
    ("1000.010", "at most two decimal places"),
])
def test_invalid_monetary_input_is_rejected_before_storage(
    database, lot, issue_data, lot_data, payment, field, value, message,
):
    # Remember the complete valid history before attempting an invalid write.
    saved = snapshot(database)

    # Exercise the appropriate public API for each monetary field.
    with pytest.raises(ValueError, match=message):
        if field == "event_amount":
            database.add_scheduled_payment(
                issue_id=lot["issue_id"], kind="coupon", planned_date="2024-09-01", amount=value
            )
        elif field == "nominal":
            database.add_issue(**{**issue_data, "isin": "UA4000000002", field: value})
        elif field == "purchase_price":
            database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **{**lot_data, field: value})
        elif field == "sale_price":
            database.add_sale(lot_id=lot["id"], sale_date="2024-08-16", sale_price=value, quantity=3)
        else:
            database.add_actual_payment(
                lot_id=lot["id"], scheduled_payment_id=payment["id"], actual_date="2024-08-15", amount=value
            )

    # Preserve all valid records without inserting invalid money.
    assert snapshot(database) == saved


@pytest.mark.parametrize("entity", ["lot", "sale"])
@pytest.mark.parametrize("quantity", [0, -1, 1.5])
def test_stored_quantities_must_be_positive_integers(database, lot, lot_data, entity, quantity):
    # Reject nonpositive and fractional quantities through storage constraints.
    with pytest.raises(DataIntegrityError):
        if entity == "lot":
            database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **{**lot_data, "quantity": quantity})
        else:
            database.add_sale(lot_id=lot["id"], sale_date="2024-08-16", sale_price="1050.01", quantity=quantity)

    # Preserve the valid purchase without leaving a rejected operation behind.
    assert database.list_lots() == [lot]
    assert database.list_sales() == []
