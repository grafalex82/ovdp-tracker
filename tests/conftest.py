"""Independent storage and HTTP fixtures using a temporary database per test."""

import pytest

from ovdp_tracker import create_app
from ovdp_tracker.db import Database


@pytest.fixture
def database_path(tmp_path):
    return tmp_path / "nested" / "portfolio.sqlite3"


@pytest.fixture
def database(database_path):
    with Database(database_path) as database:
        yield database


@pytest.fixture
def app_config(database_path):
    return {"TESTING": True, "DATABASE": str(database_path)}


@pytest.fixture
def app(app_config):
    return create_app(app_config)


@pytest.fixture
def issue_data():
    return {
        "isin": "UA4000000001",
        "nominal": "1000",
        "currency": "UAH",
        "maturity_date": "2027-06-01",
    }


@pytest.fixture
def lot_data():
    return {"purchase_date": "2024-02-15", "purchase_price": "950.01", "quantity": 10}


@pytest.fixture
def issue(database, issue_data):
    return database.add_issue(**issue_data)


@pytest.fixture
def bank(database):
    return database.add_bank("Банк А")


@pytest.fixture
def lot(database, issue, bank, lot_data):
    return database.add_lot(issue_id=issue["id"], bank_id=bank["id"], **lot_data)


@pytest.fixture
def payment(database, issue):
    return database.add_scheduled_payment(amount="80", issue_id=issue["id"], kind="coupon", planned_date="2024-08-15")
