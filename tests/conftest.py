"""Independent storage and HTTP fixtures using a temporary database per test."""

import pytest

from ovdp_calculator import create_app
from ovdp_calculator.db import Database


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
def lot_data():
    return {
        "isin": "UA4000000001",
        "purchase_date": "2024-02-15",
        "nominal": "1000",
        "currency": "UAH",
        "purchase_price": "950.005",
        "quantity": 10,
        "coupon_amount": "80",
        "maturity_date": "2027-06-01",
    }
