"""Low-level checks for schema compatibility and connection ownership.

Direct SQLite access is confined to these tests to simulate unsupported files
and verify storage internals that the public data API intentionally hides.
"""

from contextlib import closing
import sqlite3

import pytest

from ovdp_calculator import db
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


@pytest.mark.parametrize("version", [-1, 99])
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
