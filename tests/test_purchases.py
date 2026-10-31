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


@pytest.mark.parametrize("currency", ["USD", "EUR"])
@pytest.mark.parametrize("entry_mode", ["manual", "nbu"])
def test_repeat_purchase_retains_saved_currency_and_independent_parameters(
    app, app_config, database, purchase_form, monkeypatch, currency, entry_mode,
):
    # Save a foreign-currency issue and keep its complete calendar and original purchase for comparison.
    purchase_form["currency"] = currency
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == 302
    issues, events, lots = database.list_issues(), database.list_scheduled_payments(), database.list_lots()

    # Resolve the issue locally and purchase again without resending the initial UAH/manual defaults.
    def unexpected_request(isin):
        raise AssertionError("Repeat purchase saving must not request NBU data.")

    monkeypatch.setattr(app.extensions["nbu"], "lookup", unexpected_request)
    with app.test_client() as client:
        source = client.get(f'/issues/lookup?isin={purchase_form["isin"]}&source=local').json
        assert source["existing"] and source["values"]["currency"] == currency
        assert client.post("/purchases/new", data={
            "isin": f' {purchase_form["isin"].lower()} ', "entry_mode": entry_mode,
            "purchase_date": "2024-09-01", "purchase_price": "999.01", "quantity": "5", "bank_name": "Bank B",
        }).status_code == 302

    # Reopen storage and prove that purchase dates never trim the shared historical calendar.
    create_app(app_config)
    with Database(app_config["DATABASE"]) as reopened:
        first, second = reopened.list_lots()
        assert first == lots[0]
        assert second["issue_id"] == first["issue_id"] and second["currency"] == currency
        assert (second["purchase_date"], second["purchase_price"], second["quantity"]) == ("2024-09-01", "999.01", 5)
        assert reopened.get_bank(second["bank_id"])["name"] == "Bank B"
        assert reopened.list_issues() == issues and reopened.list_scheduled_payments() == events
        assert events[0]["planned_date"] < second["purchase_date"]


@pytest.mark.parametrize("entry_mode, issue_mode", [
    ("manual", "manual"), ("manual", "existing"), ("nbu", "auto"), ("nbu", "existing"),
])
@pytest.mark.parametrize("field, value", [
    ("currency", "USD"), ("nominal", "500"), ("issue_date", "2023-02-01"),
    ("nominal_rate", "17"), ("coupon_period", "183"), ("bond_type", "Different bond type"),
    ("maturity_date", "2028-01-01"), ("redemption_amount", "1100"),
    ("coupon_schedule", "2024-08-15 80\n2027-06-01 86"),
    ("coupon_schedule", "2024-08-16 80\n2027-06-01 85"),
    ("coupon_schedule", "2027-06-01 85"),
])
def test_repeat_purchase_rejects_conflicts_before_mode_normalization(
    app, database, purchase_form, entry_mode, issue_mode, field, value,
):
    # Save shared metadata, historical coupons and a fact that no repeat purchase may change.
    purchase_form["maturity_date"] = "2027-06-01"
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == 302
    issues, events, lots, banks = (
        database.list_issues(), database.list_scheduled_payments(), database.list_lots(), database.list_banks(),
    )
    actual = database.add_actual_payment(
        lot_id=lots[0]["id"], scheduled_payment_id=events[0]["id"], actual_date="2024-08-16", amount="795",
    )

    # Attempt to bypass shared-field checks through every source-selection path.
    submitted = {
        **purchase_form, "entry_mode": entry_mode, "issue_mode": issue_mode, "bank_name": "Unsaved bank", field: value,
    }
    with app.test_client() as client:
        response = client.post("/purchases/new", data=submitted)
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    error_field = "coupon_schedule" if field == "redemption_amount" else field
    preview = html.split('<section id="issue-preview"', 1)[1].split("</section>", 1)[0]
    assert f'id="{error_field}-error"' in preview and "conflict" in preview
    assert '<fieldset id="manual-fields" hidden disabled>' in html
    assert 'id="issue_mode" name="issue_mode" value="existing">' in html

    # Leave the complete issue, original purchase, bank list and confirmed receipt untouched.
    assert database.list_issues() == issues and database.list_scheduled_payments() == events
    assert database.list_lots() == lots and database.list_banks() == banks
    assert database.list_actual_payments() == [actual]


def test_invalid_repeat_purchase_renders_saved_foreign_issue_without_editable_fields(app, database, purchase_form):
    # Save a foreign issue, then submit only erroneous purchase parameters from manual mode.
    purchase_form["currency"] = "USD"
    with app.test_client() as client:
        assert client.post("/purchases/new", data=purchase_form).status_code == 302
        response = client.post("/purchases/new", data={
            "isin": purchase_form["isin"], "entry_mode": "manual", "purchase_date": "2024-09-01",
            "purchase_price": "999", "quantity": "0", "bank_name": "Unsaved bank",
        })

    # Keep saved values and the full calendar visible even before JavaScript initializes the page.
    html = response.get_data(as_text=True)
    preview = html.split('<section id="issue-preview"', 1)[1].split("</section>", 1)[0]
    assert response.status_code == 400 and 'id="quantity-error"' in html
    assert 'data-issue-field="currency">USD</dd>' in preview
    assert 'id="source-coupon-count">2</dd>' in preview
    assert "80,00 USD" in preview and "85,00 USD" in preview and "1000,00 USD" in preview
    assert '<fieldset id="manual-fields" hidden disabled>' in html
    assert 'value="999"' in html and 'value="Unsaved bank"' in html
    assert len(database.list_lots()) == len(database.list_banks()) == 1


@pytest.mark.parametrize("repeat", [False, True])
def test_failed_purchase_write_rolls_back_issue_calendar_and_new_bank(
    app, database, purchase_form, monkeypatch, repeat,
):
    # Optionally seed an existing issue before simulating a failure after the new bank is written.
    if repeat:
        with app.test_client() as client:
            assert client.post("/purchases/new", data=purchase_form).status_code == 302
    issues, events, lots, banks = (
        database.list_issues(), database.list_scheduled_payments(), database.list_lots(), database.list_banks(),
    )

    # Fail the last write to exercise rollback for both new and saved shared issues.
    def fail_purchase(self, **parameters):
        raise DatabaseError("Simulated purchase storage failure.")

    monkeypatch.setattr(Database, "add_lot", fail_purchase)
    purchase_form.update({"entry_mode": "manual", "bank_name": "Unsaved bank", "maturity_date": "2027-06-01"})
    if repeat:
        purchase_form = {key: purchase_form[key] for key in (
            "isin", "entry_mode", "bank_name", "purchase_date", "purchase_price", "quantity",
        )}
    with app.test_client() as client:
        response = client.post("/purchases/new", data=purchase_form)

    # Preserve all previous data without leaving the newly inserted bank or a partial new issue.
    assert response.status_code == 503
    assert database.list_issues() == issues and database.list_scheduled_payments() == events
    assert database.list_lots() == lots and database.list_banks() == banks


def test_nonexistent_saved_issue_cannot_bypass_explicit_creation_mode(app, database, purchase_form):
    purchase_form["issue_mode"] = "existing"
    with app.test_client() as client:
        response = client.post("/purchases/new", data=purchase_form)
    assert response.status_code == 400 and "Saved issue not found" in response.get_data(as_text=True)
    assert database.list_issues() == database.list_lots() == database.list_banks() == []
