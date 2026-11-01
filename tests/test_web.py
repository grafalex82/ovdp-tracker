"""Verify explicit database injection and cleanup around HTTP handlers."""

import pytest
from flask import abort

from ovdp_tracker.db import Database, DatabaseError
from ovdp_tracker.web import with_database


def test_application_opens_home_page_and_local_styles(app):
    with app.test_client() as client:
        # Verify that the home page renders through the database-aware handler.
        with client.get("/") as response:
            assert response.status_code == 200
            assert "ОВДП Трекер" in response.get_data(as_text=True)

        # Verify that the application serves its local stylesheet.
        with client.get("/static/style.css") as response:
            assert response.status_code == 200


def test_each_request_receives_a_separate_instance_closed_after_response(app):
    # Collect the database instances received by the test handler.
    received = []

    # Register a handler that records the supplied instance and reads its banks.
    @app.get("/test-database")
    @with_database
    def probe(database):
        assert isinstance(database, Database)
        received.append(database)
        return {"banks": database.list_banks()}

    # Exercise two separate requests to the same handler.
    with app.test_client() as client:
        assert client.get("/test-database").json == {"banks": []}
        assert client.get("/test-database").json == {"banks": []}

    # Verify instance isolation and cleanup after both responses.
    assert len(received) == 2
    assert received[0] is not received[1]
    for database in received:
        with pytest.raises(DatabaseError, match="database is closed"):
            database.list_banks()


def test_database_is_injected_alongside_route_parameters(app, database):
    # Persist a bank that the parameterized HTTP route can retrieve.
    bank = database.add_bank("Банк А")

    # Register a handler that accepts both the database and the route's bank ID.
    @app.get("/test-banks/<int:bank_id>")
    @with_database
    def show_bank(database, bank_id):
        return database.get_bank(bank_id)

    # Verify that route parameters reach the decorated handler unchanged.
    with app.test_client() as client:
        assert client.get(f"/test-banks/{bank['id']}").json == bank


def test_handler_failure_closes_database_and_rolls_back_transaction(app, database):
    # Collect the database instance supplied to the failing handler.
    received = []

    # Register a handler that fails after writing inside an explicit transaction.
    @app.get("/test-failure")
    @with_database
    def fail(database):
        # Save the received instance so its lifetime can be checked after the failure.
        received.append(database)

        # Interrupt a pending bank write before its transaction can commit.
        with database.transaction():
            database.add_bank("Банк А")
            raise ValueError("Handler failed.")

    # Let the handler exception propagate through the test client.
    with app.test_client() as client:
        with pytest.raises(ValueError, match="Handler failed"):
            client.get("/test-failure")

    # Verify that the failed request closed its database and discarded the pending bank.
    assert len(received) == 1
    with pytest.raises(DatabaseError, match="database is closed"):
        received[0].list_banks()
    assert database.list_banks() == []


def test_internal_error_page_hides_the_original_description(app):
    # Trigger an internal HTTP error containing a diagnostic message.
    @app.get("/test-internal-error")
    def internal_error():
        abort(500, description="Internal diagnostic details.")

    # Show a Russian message without exposing the internal description.
    with app.test_client() as client:
        response = client.get("/test-internal-error")
    assert response.status_code == 500
    assert "Произошла внутренняя ошибка" in response.get_data(as_text=True)
    assert "Internal diagnostic details" not in response.get_data(as_text=True)
