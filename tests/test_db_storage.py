"""Low-level checks for schema compatibility and connection ownership.

Direct SQLite access is confined to these tests to simulate unsupported files
and verify storage internals that the public data API intentionally hides.
"""

from contextlib import closing
import sqlite3

import pytest

from ovdp_calculator import db
from ovdp_calculator import create_app
from ovdp_calculator.db import Database


def test_instance_owns_connection_and_context_exit_closes_it(database_path):
    # Check connection reuse and enabled foreign keys within one instance.
    with Database(database_path) as database:
        first = database._get_connection()
        assert first is database._get_connection()
        assert first.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    # Verify that leaving the context closes the owned connection.
    with pytest.raises(sqlite3.ProgrammingError):
        _ = first.in_transaction

    # Check that a new instance owns a separate, fully configured connection.
    with Database(database_path) as database:
        second = database._get_connection()
        assert first is not second
        assert second.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_close_rolls_back_a_write_left_without_commit(database, database_path):
    # Leave a bank write pending when the database instance is closed.
    database._get_connection().execute("BEGIN IMMEDIATE")
    database.add_bank("Банк А")
    database.close()

    # Verify that reopening cannot recover the uncommitted bank.
    with Database(database_path) as reopened:
        assert reopened.list_banks() == []


@pytest.mark.parametrize("version", [-1, 1, 2, 99])
def test_unknown_versions_leave_database_byte_for_byte_unchanged(database, database_path, version):
    # Prepare a populated file carrying an unsupported schema version.
    database.add_bank("Банк А")
    database.close()
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute(f"PRAGMA user_version = {version}")
    original = database_path.read_bytes()

    # Reject the incompatible file and verify that every byte is preserved.
    with pytest.raises(db.SchemaVersionError, match=str(version)):
        Database(database_path)
    assert database_path.read_bytes() == original


def test_unversioned_existing_schema_is_not_overwritten(database, database_path):
    # Remove the version marker from an existing schema and save its bytes.
    database.close()
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute("PRAGMA user_version = 0")
    original = database_path.read_bytes()

    # Reject the unversioned file without altering its existing schema or data.
    with pytest.raises(db.SchemaVersionError, match="unversioned schema"):
        Database(database_path)
    assert database_path.read_bytes() == original


def test_failed_schema_creation_leaves_no_partial_tables_or_version(tmp_path, monkeypatch):
    # Inject a repeated CREATE statement to fail partway through initialization.
    path = tmp_path / "failed.sqlite3"
    monkeypatch.setattr(db, "_SCHEMA_STATEMENTS", (db._SCHEMA_STATEMENTS[0], db._SCHEMA_STATEMENTS[0]))

    # Trigger the incomplete schema creation through the database constructor.
    with pytest.raises(db.DatabaseError, match="database operation failed"):
        Database(path)

    # Verify that the failed initialization committed neither tables nor a version marker.
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_application_rejects_legacy_schema_without_relabeling_or_deleting_it(database_path):
    # Build a legacy file with a purchase carrying its own issue fields.
    database_path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute("CREATE TABLE bond_lots (id INTEGER PRIMARY KEY, isin TEXT, currency TEXT)")
            connection.execute("INSERT INTO bond_lots VALUES (1, 'UA4000000001', 'USD')")
            connection.execute("PRAGMA user_version = 1")
    original = database_path.read_bytes()

    # Fail during startup while retaining both the old version and all file contents.
    with pytest.raises(db.SchemaVersionError, match="stop the application and delete the database file"):
        create_app({"TESTING": True, "DATABASE": str(database_path)})
    assert database_path.read_bytes() == original


@pytest.mark.parametrize("bridge", ["lot", "event"])
def test_composite_foreign_keys_reject_cross_issue_confirmations(database, lot, issue_data, bridge):
    # Prepare a foreign event and choose either side's issue for a forged confirmation.
    foreign = database.add_issue(**{**issue_data, "isin": "UA4000000002"})
    event = database.add_scheduled_payment(
        amount="80", issue_id=foreign["id"], kind="coupon", planned_date="2024-08-15"
    )
    issue_id = lot["issue_id"] if bridge == "lot" else foreign["id"]

    # Bypass the public writer to prove that SQLite itself enforces both relationships.
    connection = database._get_connection()
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with connection:
            connection.execute(
                """INSERT INTO actual_payments (lot_id, issue_id, scheduled_payment_id, actual_date, amount)
                   VALUES (?, ?, ?, '2024-08-16', '795')""", (lot["id"], issue_id, event["id"])
            )
    assert database.list_actual_payments() == []


def test_one_redemption_per_issue_even_for_different_dates(database, issue):
    # Store a valid redemption and temporarily move maturity before a second insertion.
    event = database.add_scheduled_payment(
        amount="1000", issue_id=issue["id"], kind="redemption", planned_date=issue["maturity_date"]
    )
    connection = database._get_connection()

    # Exercise the partial unique index independently of the duplicate-date constraint.
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        with connection:
            connection.execute("UPDATE bond_issues SET maturity_date = '2028-01-01' WHERE id = ?", (issue["id"],))
            connection.execute(
                "INSERT INTO scheduled_payments (issue_id, kind, planned_date, amount) "
                "VALUES (?, 'redemption', '2028-01-01', '1000')",
                (issue["id"],),
            )
    assert database.get_issue(issue["id"]) == issue
    assert database.list_scheduled_payments() == [event]


def test_shared_event_with_confirmation_cannot_be_deleted(database, lot, payment):
    # Attach a purchase-specific fact to its shared event.
    actual = database.add_actual_payment(
        lot_id=lot["id"], scheduled_payment_id=payment["id"], actual_date="2024-08-16", amount="795"
    )

    # Reject raw deletion rather than cascading away the purchase's fact.
    connection = database._get_connection()
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with connection:
            connection.execute("DELETE FROM scheduled_payments WHERE id = ?", (payment["id"],))
    assert database.list_scheduled_payments() == [payment]
    assert database.list_actual_payments() == [actual]


def test_schema_has_event_amounts_without_a_shared_coupon_column(database):
    # Inspect only the storage internals needed to enforce the schema contract.
    connection = database._get_connection()
    issue_columns = {row[1] for row in connection.execute("PRAGMA table_info(bond_issues)")}
    event_columns = {row[1] for row in connection.execute("PRAGMA table_info(scheduled_payments)")}
    assert "coupon_amount" not in issue_columns
    assert {"issue_date", "nominal_rate", "coupon_period", "bond_type"} <= issue_columns
    assert "amount" in event_columns
    assert database.get_schema_version() == 3
