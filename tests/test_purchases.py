"""Exercise exact per-event purchase calendars, validation and atomic persistence."""

from datetime import date

import pytest

from ovdp_calculator import create_app
from ovdp_calculator.db import Database, DatabaseError


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    # Freeze the HTTP boundary's clock for deterministic form validation.
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return the fixed local day used by purchase tests."""
            return cls(2026, 10, 31)

    # Domain calls receive the same date explicitly where needed.
    monkeypatch.setattr("ovdp_calculator.web.date", LocalDate)


@pytest.fixture
def purchase_form():
    return {
        "isin": "UA4000000001", "purchase_date": "2024-02-15", "nominal": "1000", "currency": "UAH",
        "purchase_price": "1024", "quantity": "10", "bank_name": "Банк А", "issue_mode": "manual",
        "coupon_schedule": "2027-06-01 85\n2024-08-15 80", "maturity_date": "", "infer_maturity_from_coupons": "1",
        "issue_date": "2023-01-01", "nominal_rate": "16.5", "coupon_period": "182", "bond_type": "Середньострокові",
    }


def test_form_defaults_do_not_write_data(app, database):
    with app.test_client() as client:
        html = client.get("/purchases/new").get_data(as_text=True)
        assert 'value="2026-10-31"' in html
        assert 'name="nominal" type="text" value="1000"' in html
        assert 'id="coupon_schedule"' in html
        assert 'id="coupon_amount"' not in html
        assert client.get("/static/purchase.js").status_code == 200
    assert database.list_issues() == database.list_lots() == database.list_banks() == []


def test_variable_coupons_and_metadata_survive_restart(app, app_config, database, purchase_form):
    # Persist historical and final coupons with different unit amounts.
    with app.test_client() as client:
        response = client.post("/purchases/new", data=purchase_form)
        assert response.status_code == 302
        assert "Покупка сохранена." in client.get(response.location).get_data(as_text=True)
    lot, = database.list_lots()
    assert "coupon_amount" not in lot
    assert lot["quantity"] == 10
    assert lot["issue_date"] == "2023-01-01"
    assert lot["nominal_rate"] == "16.5"
    events = database.list_scheduled_payments()
    assert [(event["kind"], event["planned_date"], event["amount"]) for event in events] == [
        ("coupon", "2024-08-15", "80"), ("coupon", "2027-06-01", "85"), ("redemption", "2027-06-01", "1000"),
    ]

    # Reopen storage and confirm that read-only pages show each distinct amount.
    restarted = create_app(app_config)
    with restarted.test_client() as client:
        html = client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
        assert "80,00 UAH" in html and "85,00 UAH" in html
    with Database(app_config["DATABASE"]) as reopened:
        assert reopened.list_scheduled_payments() == events
        assert reopened.list_actual_payments() == []


@pytest.mark.parametrize("currency", ["UAH", "USD", "EUR"])
def test_manual_values_and_redemption_override_defaults(app, database, purchase_form, currency):
    purchase_form.update({
        "currency": currency, "nominal": "500", "redemption_amount": "600,01", "purchase_price": "1000,01",
        "quantity": "3", "maturity_date": "2028-02-15", "infer_maturity_from_coupons": "0",
    })
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == 302
    lot, = database.list_lots()
    assert lot["currency"] == currency and lot["purchase_price"] == "1000.01"
    assert database.list_scheduled_payments()[-1]["amount"] == "600.01"


def test_zero_coupon_issue_requires_only_redemption(app, database, purchase_form):
    purchase_form.update({"coupon_schedule": "", "maturity_date": "2025-02-15"})
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == 302
    event, = database.list_scheduled_payments()
    assert event["kind"] == "redemption" and event["amount"] == "1000"


@pytest.mark.parametrize("day", ["2024-02-15", "2026-10-31"])
def test_purchase_and_coupon_boundaries_include_zero_events(app, database, purchase_form, day):
    purchase_form.update({"purchase_date": day, "coupon_schedule": f"{day} 0", "maturity_date": day})
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == 302
    assert database.list_scheduled_payments()[0]["amount"] == "0"
    assert database.list_actual_payments() == []


@pytest.mark.parametrize("field, value, message", [
    ("isin", " ", "This field is required."), ("bank_name", " ", "This field is required."),
    ("currency", "GBP", "Currency must be"),
    *[("quantity", value, "Quantity must be") for value in ("0", "-1", "1,5", "1.0", "abc", "", "9223372036854775808")],
    ("nominal", "0", "Amount must be positive"), ("purchase_price", "-1", "Amount must be positive"),
    ("redemption_amount", "0", "Amount must be positive"),
    ("purchase_date", "2026-11-01", "planned purchases are not supported"),
    ("purchase_date", "2025-02-29", "Enter a valid date"), ("purchase_date", "20240215", "Enter a valid date"),
    ("maturity_date", "2023-01-01", "Maturity date cannot be before"),
    ("maturity_date", "2027-02-30", "Enter a valid date"),
    ("coupon_schedule", "2027-06-01 80\n2027-06-01 85", "Coupon dates must be unique"),
    ("coupon_schedule", "2027-06-02 80", "Coupon dates must not be after maturity"),
    ("coupon_schedule", "2024-02-30 80", "Enter a valid date"),
    ("coupon_schedule", "2024-08-15 -1", "Amount must be nonnegative"),
    ("coupon_schedule", "2024-08-15", "Enter each coupon"),
    ("nominal_rate", "NaN", "Nominal rate must be"), ("coupon_period", "1.5", "Coupon period must be"),
    ("issue_mode", "pending", "Load issue data"),
])
def test_invalid_purchase_keeps_inputs_and_creates_no_records(app, database, purchase_form, field, value, message):
    purchase_form.update({"maturity_date": "2027-06-01", field: value})
    with app.test_client() as client:
        response = client.post("/purchases/new", data=purchase_form)
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    error_field = "isin" if field == "issue_mode" else field
    assert message in html and f'id="{error_field}-error"' in html
    assert f'value="{purchase_form["purchase_price"]}"' in html
    assert database.list_lots() == database.list_issues() == database.list_banks() == []
    assert database.list_scheduled_payments() == []


@pytest.mark.parametrize("field", ["nominal", "purchase_price", "redemption_amount", "coupon_schedule"])
@pytest.mark.parametrize("value", ["1000,005", "1000.010", "NaN", "Infinity", "abc", ""])
def test_invalid_money_is_rejected_in_every_field(app, database, purchase_form, field, value):
    if field == "redemption_amount" and value == "":
        value = "-1"
    purchase_form[field] = f"2027-06-01 {value}" if field == "coupon_schedule" else value
    with app.test_client() as client:
        response = client.post("/purchases/new", data=purchase_form)
    assert response.status_code == 400
    assert f'id="{field}-error"' in response.get_data(as_text=True)
    assert database.list_issues() == []


@pytest.mark.parametrize("overrides", [
    {"coupon_schedule": "", "maturity_date": ""}, {"maturity_date": "", "infer_maturity_from_coupons": "0"},
    {"issue_mode": "auto", "loaded_isin": "UA4000000002"},
])
def test_missing_maturity_or_stale_loaded_issue_is_rejected(app, database, purchase_form, overrides):
    purchase_form.update(overrides)
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == 400
    assert database.list_issues() == []


def test_failed_schedule_write_rolls_back_every_record(app, database, purchase_form, monkeypatch):
    # Fail after storing coupons to verify the entire issue and calendar transaction.
    original = Database.add_scheduled_payment

    def fail_redemption(self, **parameters):
        if parameters["kind"] == "redemption":
            raise DatabaseError("Simulated schedule storage failure.")
        return original(self, **parameters)

    monkeypatch.setattr(Database, "add_scheduled_payment", fail_redemption)

    # Retain the complete user draft on failure without a partial issue or bank.
    with app.test_client() as client:
        response = client.post("/purchases/new", data=purchase_form)
    assert response.status_code == 503
    assert "No changes were saved" in response.get_data(as_text=True)
    assert database.list_issues() == database.list_scheduled_payments() == database.list_banks() == []


def test_repeat_purchase_uses_saved_fields_without_resending_calendar(app, database, purchase_form):
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == 302
        events = database.list_scheduled_payments()
        keys = ("isin", "purchase_date", "purchase_price", "quantity", "bank_name")
        second = {key: purchase_form[key] for key in keys}
        second.update({"issue_mode": "existing", "purchase_price": "999", "bank_name": "Bank B"})
        assert client.post("/purchases/new", data=second).status_code == 302
    assert len(database.list_lots()) == 2
    assert len(database.list_issues()) == 1
    assert database.list_scheduled_payments() == events


@pytest.mark.parametrize("field, value", [
    ("currency", "USD"), ("nominal", "500"), ("nominal_rate", "25"),
    ("maturity_date", "2028-01-01"), ("coupon_schedule", "2027-06-01 80"),
    ("coupon_schedule", "2027-06-01 85\n2024-08-15 81"), ("redemption_amount", "1100"),
])
def test_conflicting_repeat_purchase_preserves_saved_calendar(app, database, purchase_form, field, value):
    purchase_form["maturity_date"] = "2027-06-01"
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == 302
        events, issues = database.list_scheduled_payments(), database.list_issues()
        purchase_form.update({"bank_name": "Bank B", field: value})
        assert client.post("/purchases/new", data=purchase_form).status_code == 400
    assert database.list_issues() == issues
    assert database.list_scheduled_payments() == events
    assert len(database.list_lots()) == len(database.list_banks()) == 1


def test_manual_form_derives_maturity_and_uses_only_requested_issue_fields(app, database, purchase_form):
    # Submit just nominal, currency and coupon rows, without optional source metadata or maturity.
    form = {key: purchase_form[key] for key in (
        "isin", "purchase_date", "purchase_price", "quantity", "bank_name", "nominal", "currency", "coupon_schedule",
    )}
    form["entry_mode"] = "manual"
    with app.test_client() as client:
        assert client.post("/purchases/new", data=form).status_code == 302

    # Infer redemption from the latest coupon and nominal, leaving unavailable metadata empty.
    issue, = database.list_issues()
    assert issue["maturity_date"] == "2027-06-01"
    assert issue["issue_date"] == issue["nominal_rate"] == issue["bond_type"] == ""
    assert issue["coupon_period"] is None
    assert database.list_scheduled_payments()[-1]["amount"] == issue["nominal"]


@pytest.mark.parametrize("maturity, status", [("", 400), ("2027-07-01", 302)])
def test_manual_zero_coupon_form_requires_an_explicit_maturity(app, database, purchase_form, maturity, status):
    purchase_form.update({"entry_mode": "manual", "coupon_schedule": "", "maturity_date": maturity})
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == status
    assert len(database.list_lots()) == (1 if status == 302 else 0)


def test_form_separates_purchase_fields_and_does_not_offer_source_metadata_inputs(app):
    # Check the rendered form contract independently of JavaScript implementation details.
    with app.test_client() as client:
        html = client.get("/purchases/new").get_data(as_text=True)
    purchase = html.split('<fieldset id="purchase-fields">', 1)[1].split("</fieldset>", 1)[0]
    manual = html.split('<fieldset id="manual-fields"', 1)[1].split("</fieldset>", 1)[0]
    preview = html.split('<section id="issue-preview"', 1)[1].split("</section>", 1)[0]
    for name in ("purchase_date", "purchase_price", "quantity", "bank_name"):
        assert f'name="{name}"' in purchase
    assert 'name="purchase_date"' not in manual
    for name in ("nominal", "currency", "coupon_schedule"):
        assert f'name="{name}"' in manual
    for name in ("issue_date", "nominal_rate", "coupon_period", "bond_type", "redemption_amount"):
        assert f'name="{name}"' not in html
    assert '<input' not in preview and '<textarea' not in preview and '<select' not in preview
    assert 'value="nbu" checked' in html and 'value="manual"' in html
    assert '<tbody id="source-payments">' in preview
