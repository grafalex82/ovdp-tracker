"""Exercise purchase forms, exact values, schedules, and atomic persistence through HTTP."""

from datetime import date
from html import escape

import pytest

from ovdp_calculator import create_app
from ovdp_calculator.db import Database, DatabaseError
from ovdp_calculator.money import operation_amount, parse_money


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    # Freeze the local date used for form defaults and operation validation.
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return a fixed local date for deterministic purchase form tests."""
            return cls(2026, 10, 31)

    # Replace only the HTTP boundary's clock; persistence receives that date explicitly.
    monkeypatch.setattr("ovdp_calculator.web.date", LocalDate)


@pytest.fixture
def purchase_form():
    return {
        "isin": "UA4000000001",
        "purchase_date": "2024-02-15",
        "nominal": "1000",
        "currency": "UAH",
        "purchase_price": "1024",
        "quantity": "10",
        "bank_name": "Банк А",
        "coupon_amount": "80",
        "coupon_dates": "2027-06-01\n2024-08-15",
        "maturity_date": "",
        "maturity_manual": "0",
    }


def test_purchase_form_defaults_and_navigation_do_not_create_records(app, database):
    # Open the purchase form through its visible home page link.
    with app.test_client() as client:
        home = client.get("/").get_data(as_text=True)
        assert 'href="/purchases/new"' in home
        response = client.get("/purchases/new")
        script = client.get("/static/purchase.js")

    # Check editable defaults and the locally served maturity suggestion script.
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'name="purchase_date" type="date" value="2026-10-31"' in html
    assert 'name="nominal" type="text" value="1000"' in html
    assert '<option value="UAH" selected>' in html
    assert 'name="coupon_amount" type="text" value="0"' in html
    assert script.status_code == 200
    assert database.list_lots() == []
    assert database.list_banks() == []
    assert database.list_scheduled_payments() == []


def test_coupon_purchase_saves_all_fields_and_separate_events(app, database, purchase_form):
    # Save a historical purchase without JavaScript or an explicitly supplied maturity date.
    with app.test_client() as client:
        response = client.post("/purchases/new", data=purchase_form)
        assert response.status_code == 302
        assert "Покупка сохранена." in client.get(response.location).get_data(as_text=True)

    # Verify every persisted field and the exact total purchase cost.
    lot, = database.list_lots()
    bank, = database.list_banks()
    assert bank["name"] == "Банк А"
    assert lot == {
        "id": lot["id"], "issue_id": lot["issue_id"], "isin": "UA4000000001", "purchase_date": "2024-02-15",
        "nominal": "1000", "currency": "UAH", "purchase_price": "1024", "quantity": 10,
        "bank_id": bank["id"], "coupon_amount": "80", "maturity_date": "2027-06-01",
    }
    assert operation_amount(parse_money(lot["purchase_price"]), lot["quantity"]) == parse_money("10240.00")

    # Keep the historical coupon, final coupon, and redemption as distinct unconfirmed events.
    events = database.list_scheduled_payments(lot["issue_id"])
    assert [(event["kind"], event["planned_date"]) for event in events] == [
        ("coupon", "2024-08-15"), ("coupon", "2027-06-01"), ("redemption", "2027-06-01"),
    ]
    assert len({event["id"] for event in events}) == 3
    assert database.list_actual_payments() == []


@pytest.mark.parametrize("currency", ["UAH", "USD", "EUR"])
def test_manual_maturity_currency_and_nominal_override_defaults(app, database, purchase_form, currency):
    # Supply explicit settings, a manual maturity override, and comma-separated money.
    purchase_form.update({
        "currency": currency, "nominal": "500", "purchase_price": "1000,01", "quantity": "3",
        "maturity_date": "2028-02-15", "maturity_manual": "1",
    })
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == 302

    # Verify that user choices replace the defaults without rounding the price.
    lot, = database.list_lots()
    assert lot["purchase_date"] == "2024-02-15"
    assert lot["currency"] == currency
    assert lot["nominal"] == "500"
    assert lot["purchase_price"] == "1000.01"
    assert operation_amount(parse_money(lot["purchase_price"]), lot["quantity"]) == parse_money("3000.03")
    redemption, = [event for event in database.list_scheduled_payments() if event["kind"] == "redemption"]
    assert lot["maturity_date"] == redemption["planned_date"] == "2028-02-15"


def test_zero_coupon_purchase_saves_only_redemption_and_survives_restart(app, app_config, database, purchase_form):
    # Create an already matured zero-coupon purchase without inventing actual payments.
    purchase_form.update({
        "coupon_dates": "", "coupon_amount": "0", "purchase_price": "900", "maturity_date": "2025-02-15",
    })
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == 302

    # Reinitialize the application and inspect its saved data through a fresh storage instance.
    create_app(app_config)
    with Database(app_config["DATABASE"]) as reopened:
        lot, = reopened.list_lots()
        event, = reopened.list_scheduled_payments()
        assert event["kind"] == "redemption"
        assert event["issue_id"] == lot["issue_id"]
        assert event["planned_date"] == lot["maturity_date"] == "2025-02-15"
        assert operation_amount(parse_money(lot["purchase_price"]), lot["quantity"]) == parse_money("9000.00")
        assert operation_amount(parse_money(lot["nominal"]), lot["quantity"]) == parse_money("10000.00")
        assert reopened.list_actual_payments() == []


@pytest.mark.parametrize("day", ["2024-02-15", "2026-10-31"])
def test_purchase_and_coupon_dates_allow_inclusive_boundaries(app, database, purchase_form, day):
    # Allow a purchase today or historically and coupons on both interval boundaries.
    purchase_form.update({"purchase_date": day, "coupon_dates": day, "maturity_date": day, "coupon_amount": "0"})
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == 302

    # Preserve the zero coupon schedule without creating any factual receipt.
    assert len(database.list_scheduled_payments()) == 2
    assert database.list_actual_payments() == []


@pytest.mark.parametrize("field, value, message", [
    ("isin", " ", "This field is required."),
    ("bank_name", " ", "This field is required."),
    ("currency", "GBP", "Currency must be UAH, USD, or EUR."),
    ("quantity", "0", "Quantity must be a positive integer"),
    ("quantity", "-1", "Quantity must be a positive integer"),
    ("quantity", "1,5", "Quantity must be a positive integer"),
    ("quantity", "1.0", "Quantity must be a positive integer"),
    ("quantity", "abc", "Quantity must be a positive integer"),
    ("quantity", "", "Quantity must be a positive integer"),
    ("quantity", "9223372036854775808", "Quantity must be a positive integer"),
    ("nominal", "0", "Amount must be positive."),
    ("nominal", "-1000", "Amount must be positive."),
    ("purchase_price", "0", "Amount must be positive."),
    ("purchase_price", "-0.01", "Amount must be positive."),
    ("coupon_amount", "-80", "Amount must be nonnegative."),
    ("purchase_date", "2026-11-01", "planned purchases are not supported"),
    ("purchase_date", "2025-02-29", "Enter a valid date"),
    ("purchase_date", "20240215", "Enter a valid date"),
    ("purchase_date", "", "Enter a valid date"),
    ("maturity_date", "2023-01-01", "Maturity date cannot be before the purchase date."),
    ("maturity_date", "2027-02-30", "Enter a valid date"),
    ("coupon_dates", "2027-06-01\n2027-06-01", "Coupon dates must be unique."),
    ("coupon_dates", "2027-06-02", "Coupon dates must not be after maturity"),
    ("coupon_dates", "2024-02-30", "Enter a valid date"),
    ("coupon_dates", "20240601", "Enter a valid date"),
])
def test_invalid_purchase_keeps_inputs_and_creates_no_records(app, database, purchase_form, field, value, message):
    # Submit one invalid field alongside a valid explicit maturity date.
    purchase_form["maturity_date"] = "2027-06-01"
    purchase_form[field] = value
    with app.test_client() as client:
        response = client.post("/purchases/new", data=purchase_form)

    # Attach the error to its field and preserve all raw values for correction.
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert message in html
    assert f'id="{field}-error"' in html
    for name, raw in purchase_form.items():
        if name == "coupon_dates":
            assert escape(raw) in html
        elif name == "currency":
            assert f'<option value="{raw}" selected>' in html
        else:
            assert f'value="{escape(raw)}"' in html
    assert database.list_lots() == []
    assert database.list_banks() == []
    assert database.list_scheduled_payments() == []
    assert database.list_issues() == []


@pytest.mark.parametrize("field", ["nominal", "purchase_price", "coupon_amount"])
@pytest.mark.parametrize("value", ["1000,005", "1000.010", "NaN", "Infinity", "abc", ""])
def test_invalid_money_is_rejected_in_every_purchase_field(app, database, purchase_form, field, value):
    purchase_form[field] = value
    with app.test_client() as client:
        response = client.post("/purchases/new", data=purchase_form)
        assert response.status_code == 400
        assert f'id="{field}-error"' in response.get_data(as_text=True)
    assert database.list_lots() == []
    assert database.list_banks() == []


@pytest.mark.parametrize("overrides", [
    {"coupon_dates": "", "maturity_date": ""},
    {"maturity_date": "", "maturity_manual": "1"},
])
def test_missing_maturity_requires_manual_input_when_default_is_unavailable(app, database, purchase_form, overrides):
    purchase_form.update(overrides)
    with app.test_client() as client:
        response = client.post("/purchases/new", data=purchase_form)
        assert response.status_code == 400
        assert 'id="maturity_date-error"' in response.get_data(as_text=True)
    assert database.list_lots() == []


def test_failed_schedule_write_rolls_back_whole_purchase(app, database, purchase_form, monkeypatch):
    # Fail after storing one coupon to prove that partially saved schedules cannot survive.
    original = Database.add_scheduled_payment

    def fail_redemption(self, **parameters):
        if parameters["kind"] == "redemption":
            raise DatabaseError("Simulated schedule storage failure.")
        return original(self, **parameters)

    monkeypatch.setattr(Database, "add_scheduled_payment", fail_redemption)

    # Keep the user's inputs available after rolling back the bank, lot, and coupon events.
    with app.test_client() as client:
        response = client.post("/purchases/new", data=purchase_form)
    html = response.get_data(as_text=True)
    assert response.status_code == 503
    assert "No changes were saved" in html
    assert 'value="UA4000000001"' in html
    assert 'value="Банк А"' in html
    assert database.list_lots() == []
    assert database.list_banks() == []
    assert database.list_scheduled_payments() == []
    assert database.list_issues() == []


def test_purchase_error_escapes_user_content(app, purchase_form):
    purchase_form.update({"isin": '<script>alert("x")</script>', "quantity": "0"})
    with app.test_client() as client:
        html = client.post("/purchases/new", data=purchase_form).get_data(as_text=True)
        assert '<script>alert("x")</script>' not in html
        assert "&lt;script&gt;" in html


def test_repeat_purchase_reuses_issue_and_complete_calendar(app, database, purchase_form):
    # Save the first purchase and remember its common issue and calendar.
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == 302
        first, = database.list_lots()
        issues = database.list_issues()
        events = database.list_scheduled_payments()

        # Buy the same ISIN later at an independent price through another bank.
        purchase_form.update({
            "purchase_date": "2025-01-01", "purchase_price": "1000,01", "quantity": "3", "bank_name": "Банк Б",
            "nominal": "1000,00", "coupon_amount": "80.00", "coupon_dates": "2024-08-15\n2027-06-01",
        })
        assert client.post("/purchases/new", data=purchase_form).status_code == 302

    # Preserve the first purchase and all calendar dates, including those before the second purchase.
    assert database.list_issues() == issues
    assert database.list_scheduled_payments() == events
    assert database.get_lot(first["id"]) == first
    second = database.list_lots()[1]
    assert second["issue_id"] == first["issue_id"]
    assert second["purchase_date"] == "2025-01-01"
    assert second["purchase_price"] == "1000.01"
    assert second["quantity"] == 3
    assert second["bank_id"] != first["bank_id"]
    assert database.list_actual_payments() == []


def test_new_issue_keeps_coupon_dates_before_its_first_purchase(app, database, purchase_form):
    # Add a new issue whose calendar begins before this user's first purchase.
    purchase_form["purchase_date"] = "2025-01-01"
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == 302

    # Retain the complete calendar without inventing actual payments for past dates.
    assert database.list_scheduled_payments()[0]["planned_date"] == "2024-08-15"
    assert database.list_actual_payments() == []


@pytest.mark.parametrize("field, value", [
    ("currency", "USD"), ("nominal", "500"), ("coupon_amount", "25"),
    ("maturity_date", "2028-01-01"), ("coupon_dates", "2027-06-01"),
])
def test_conflicting_repeat_purchase_does_not_save_partial_records(app, database, purchase_form, field, value):
    # Save the common issue and capture every record that a repeat submission might affect.
    purchase_form["maturity_date"] = "2027-06-01"
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == 302
        issues = database.list_issues()
        lots = database.list_lots()
        events = database.list_scheduled_payments()
        banks = database.list_banks()

        # Submit another purchase with one valid but conflicting common field.
        purchase_form.update({"bank_name": "Банк Б", field: value})
        response = client.post("/purchases/new", data=purchase_form)

    # Keep the existing ISIN intact and report the conflict next to the submitted field.
    assert response.status_code == 400
    assert f'id="{field}-error"' in response.get_data(as_text=True)
    assert database.list_issues() == issues
    assert database.list_lots() == lots
    assert database.list_scheduled_payments() == events
    assert database.list_banks() == banks
