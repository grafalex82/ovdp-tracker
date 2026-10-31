"""Verify fresh NBU lookups, bond parsing and protected purchase drafts without network access."""

from copy import deepcopy
from decimal import Decimal
from io import BytesIO
from urllib.error import URLError

import pytest

from ovdp_calculator.nbu import NbuClient, NbuError, NbuNotFound, parse_bond_data
from ovdp_calculator.purchases import IssueValidationError, validate_imported_issue


@pytest.fixture
def security():
    return {
        "cpcode": "UA4000228381", "cptype": "DCP", "nominal": 1000, "val_code": "UAH",
        "razm_date": "2023-08-29", "pgs_date": "2026-11-04", "auk_proc": Decimal("19.18"),
        "pay_period": 182, "cpdescr": "Середньострокові",
        "payments": [
            {"pay_type": "1", "pay_date": day, "pay_val": Decimal("95.90")}
            for day in ("2023-11-08", "2024-05-08", "2024-11-06", "2025-05-07", "2025-11-05",
                        "2026-05-06", "2026-11-04")
        ] + [{"pay_type": "2", "pay_date": "2026-11-04", "pay_val": 1000}],
    }


@pytest.mark.parametrize("currency", ["UAH", "USD", "EUR"])
def test_exact_source_mapping_and_seven_historical_coupons(security, currency):
    security["val_code"] = currency
    result = parse_bond_data(security)
    issue, events = result["issue"], result["payments"]
    assert issue == {
        "isin": "UA4000228381", "currency": currency, "nominal": "1000", "issue_date": "2023-08-29",
        "maturity_date": "2026-11-04", "nominal_rate": "19.18", "coupon_period": 182,
        "bond_type": "Середньострокові",
    }
    assert len([event for event in events if event["kind"] == "coupon"]) == 7
    assert events[0]["planned_date"] == "2023-11-08"
    assert events[0]["amount"] == "95.90"
    assert events[-1]["kind"] == "redemption"
    assert events[-1]["planned_date"] == events[-2]["planned_date"]


def test_variable_and_zero_coupons_are_not_generated_from_rate(security):
    security["payments"][0]["pay_val"] = 0
    security["payments"][1]["pay_val"] = Decimal("80.12")
    security["payments"][-1]["pay_val"] = 1100
    events = parse_bond_data(security)["payments"]
    assert [events[index]["amount"] for index in (0, 1, 2, -1)] == ["0", "80.12", "95.90", "1100"]


@pytest.mark.parametrize("redemption_type", ["2", "3"])
def test_no_coupon_calendar_and_early_redemption(security, redemption_type):
    security["payments"] = [security["payments"][-1]]
    security["payments"][0]["pay_type"] = redemption_type
    events = parse_bond_data(security)["payments"]
    assert len(events) == 1 and events[0]["kind"] == "redemption"


@pytest.mark.parametrize("problem", [
    "missing-metadata", "missing-amount", "negative-coupon", "fractional-money", "missing-redemption",
    "duplicate-redemption", "unknown-type", "duplicate-coupon", "late-coupon", "wrong-maturity",
    "invalid-period", "invalid-date", "negative-rate", "invalid-nominal", "boolean-amount",
])
def test_malformed_source_and_unsupported_issue_rules_are_checked_separately(security, problem):
    # Prepare malformed source structure or bond values unsupported by the purchase form.
    if problem == "missing-metadata":
        del security["razm_date"]
    elif problem == "missing-amount":
        del security["payments"][0]["pay_val"]
    elif problem == "negative-coupon":
        security["payments"][0]["pay_val"] = -1
    elif problem == "fractional-money":
        security["payments"][0]["pay_val"] = Decimal("80.123")
    elif problem == "missing-redemption":
        security["payments"].pop()
    elif problem == "duplicate-redemption":
        security["payments"].append(deepcopy(security["payments"][-1]))
    elif problem == "unknown-type":
        security["payments"][0]["pay_type"] = "9"
    elif problem == "duplicate-coupon":
        security["payments"].append(deepcopy(security["payments"][0]))
    elif problem == "late-coupon":
        security["payments"][0]["pay_date"] = "2027-01-01"
    elif problem == "wrong-maturity":
        security["pgs_date"] = "2026-11-05"
    elif problem == "invalid-period":
        security["pay_period"] = Decimal("182.5")
    elif problem == "invalid-date":
        security["payments"][0]["pay_date"] = "2025-02-30"
    elif problem == "negative-rate":
        security["auk_proc"] = -1
    elif problem == "invalid-nominal":
        security["nominal"] = 0
    else:
        security["payments"][0]["pay_val"] = True
    # Parse structure first and defer domain rules to the independent issue validator.
    if problem in ("missing-metadata", "missing-amount", "boolean-amount"):
        with pytest.raises(NbuError, match="incomplete or malformed"):
            parse_bond_data(security)
    else:
        data = parse_bond_data(security)
        with pytest.raises(IssueValidationError):
            validate_imported_issue(data)


def test_parser_preserves_source_data_beyond_purchase_model_limits(security):
    # Include precision, currency and payment types that a future bond viewer may need to display.
    security.update({"val_code": "JPY", "nominal": Decimal("1000.123"), "pay_period": Decimal("182.5")})
    security["payments"] = [
        {"pay_type": "2", "pay_date": "2027-01-01", "pay_val": Decimal("500.123")},
        {"pay_type": "9", "pay_date": "2026-01-01", "pay_val": Decimal("1.234")},
        {"pay_type": "3", "pay_date": "2027-02-01", "pay_val": 500},
    ]
    original = deepcopy(security)

    # Parsing must preserve all events in source order without rounding or altering the source record.
    result = parse_bond_data(security)
    assert result["issue"]["currency"] == "JPY"
    assert result["issue"]["nominal"] == "1000.123"
    assert result["issue"]["coupon_period"] == Decimal("182.5")
    assert result["payments"] == [
        {"kind": "redemption", "planned_date": "2027-01-01", "amount": "500.123"},
        {"kind": "9", "planned_date": "2026-01-01", "amount": "1.234"},
        {"kind": "redemption", "planned_date": "2027-02-01", "amount": "500"},
    ]
    assert security == original


def test_future_bond_can_be_parsed_and_validated_without_any_purchase(security, monkeypatch):
    # Make purchase validation unavailable and supply a bond issued beyond today's date.
    def unexpected_purchase_validation(*args, **kwargs):
        raise AssertionError("Bond processing must not validate a purchase.")

    monkeypatch.setattr("ovdp_calculator.purchases.validate_purchase", unexpected_purchase_validation)
    security.update({"razm_date": "2040-01-01", "pgs_date": "2041-01-01"})
    security["payments"] = [{"pay_type": "2", "pay_date": "2041-01-01", "pay_val": 1000}]

    # Neither parsing nor supported-issue validation requires a price, quantity, bank or purchase date.
    data = parse_bond_data(security)
    assert validate_imported_issue(data) == data
    assert set(data) == {"issue", "payments"}
    assert not {"purchase_date", "purchase_price", "quantity", "bank_name"}.intersection(data["issue"])


@pytest.mark.parametrize("problem", ["multiple-redemptions", "missing-redemption", "unknown-type", "fractional-money"])
def test_purchase_lookup_rejects_unsupported_parsed_data_without_partial_import(
        app, database, security, monkeypatch, problem):
    # Feed structurally valid data that cannot be saved by the current purchase model.
    if problem == "multiple-redemptions":
        security["payments"].append(deepcopy(security["payments"][-1]))
    elif problem == "missing-redemption":
        security["payments"].pop()
    elif problem == "unknown-type":
        security["payments"][0]["pay_type"] = "9"
    else:
        security["payments"][0]["pay_val"] = Decimal("80.123")
    monkeypatch.setattr(app.extensions["nbu"], "_download", lambda: [security])

    # Reject the purchase draft without issuing a token or writing any source records.
    with app.test_client() as client:
        response = client.get("/issues/lookup?isin=UA4000228381")
    assert response.status_code == 502
    assert "Unsupported NBU bond data" in response.json["error"]
    assert "values" not in response.json and "issue_token" not in response.json
    assert database.list_issues() == database.list_scheduled_payments() == []


def test_each_lookup_downloads_fresh_data_and_matches_exact_domestic_issue(security, monkeypatch):
    # Include a foreign issue and a partial identifier that must not match the requested domestic ISIN.
    records = [security, {**security, "cptype": "OZDP"}, {**security, "cpcode": "UA400022838"}]
    client = NbuClient()
    calls = []
    monkeypatch.setattr(client, "_download", lambda: calls.append(1) or records)
    first = client.lookup(" ua4000228381 ")
    first["payments"][0]["amount"] = "1"
    assert len(calls) == 1

    # Fetch updated source amounts immediately and keep earlier results independent.
    security["payments"][0]["pay_val"] = Decimal("80.12")
    assert client.lookup("UA4000228381")["payments"][0]["amount"] == "80.12"
    assert first["payments"][0]["amount"] == "1"
    assert len(calls) == 2

    # Reject missing issues after a new request and invalid identifiers before any request.
    with pytest.raises(NbuNotFound):
        client.lookup("UA4000228382")
    assert len(calls) == 3
    with pytest.raises(NbuError, match="complete"):
        client.lookup("UA4000")
    assert len(calls) == 3


def test_download_preserves_decimal_literals_and_timeout(monkeypatch):
    # Supply the response bytes exactly as a server would send them.
    calls = []

    def open_response(url, timeout):
        calls.append((url, timeout))
        return BytesIO(b'[{"amount":80.12}]')

    monkeypatch.setattr("ovdp_calculator.nbu.urlopen", open_response)

    # Decimal parsing must avoid any binary float intermediate.
    assert NbuClient(timeout=3)._download() == [{"amount": Decimal("80.12")}]
    assert calls == [("https://bank.gov.ua/depo_securities?json", 3)]


@pytest.mark.parametrize("payload", [b"bad JSON", b"{}", b"[1]", b"\xff"])
def test_invalid_json_or_directory_shape_is_rejected(monkeypatch, payload):
    monkeypatch.setattr("ovdp_calculator.nbu.urlopen", lambda *args, **kwargs: BytesIO(payload))
    with pytest.raises(NbuError):
        NbuClient().lookup("UA4000228381")


def test_lookup_can_be_retried_after_network_failure(monkeypatch, security):
    # Fail the first attempt and then make a complete response available.
    client = NbuClient()
    calls = []

    def download():
        calls.append(1)
        if len(calls) == 1:
            raise NbuError("Temporary failure.")
        return [security]

    monkeypatch.setattr(client, "_download", download)

    # Make a new request after the previous failure.
    with pytest.raises(NbuError):
        client.lookup("UA4000228381")
    assert client.lookup("UA4000228381")["issue"]["isin"] == "UA4000228381"
    assert len(calls) == 2


def test_lookup_populates_only_a_draft_then_saves_atomically(app, database, security, monkeypatch):
    monkeypatch.setattr(app.extensions["nbu"], "_download", lambda: [security])
    with app.test_client() as client:
        response = client.get("/issues/lookup?isin=ua4000228381")
        assert response.status_code == 200
        values = response.json["values"]
        values["issue_token"] = response.json["issue_token"]
        assert database.list_issues() == database.list_scheduled_payments() == []
        values.update({
            "issue_mode": "auto", "loaded_isin": "UA4000228381", "purchase_date": "2024-01-01",
            "purchase_price": "1001.01", "quantity": "3", "bank_name": "Bank A",
        })
        assert client.post("/purchases/new", data=values).status_code == 302
    assert len(database.list_scheduled_payments()) == 8
    assert len(database.list_lots()) == len(database.list_issues()) == 1
    assert database.list_lots()[0]["purchase_price"] == "1001.01"


def test_known_issue_and_actual_receipts_survive_changed_or_unavailable_source(app, database, security, monkeypatch):
    # Save a complete issue and an independent factual payment first.
    normalized = parse_bond_data(security)
    issue = database.add_issue(**normalized["issue"])
    events = [database.add_scheduled_payment(issue_id=issue["id"], **event) for event in normalized["payments"]]
    lot = database.add_lot(
        issue_id=issue["id"], bank_id=database.add_bank("Bank A")["id"],
        purchase_date="2023-09-01", purchase_price="999", quantity=10,
    )
    actual = database.add_actual_payment(
        lot_id=lot["id"], scheduled_payment_id=events[0]["id"], actual_date="2023-11-08", amount="950",
    )
    security["payments"][0]["pay_val"] = 1
    monkeypatch.setattr(app.extensions["nbu"], "_download", lambda: [security])

    # Show discrepancies and saved details without rewriting plans or facts.
    with app.test_client() as client:
        response = client.get("/issues/lookup?isin=UA4000228381")
        assert response.json["existing"] is True
        assert "differs" in response.json["warning"]
        assert "95.90" in response.json["values"]["coupon_schedule"]
        assert response.json["details_url"] == f'/issues/{lot["isin"]}'
    assert database.list_scheduled_payments() == events
    assert database.list_actual_payments() == [actual]

    # Unsupported remote calendars must also fall back to the complete saved issue.
    security["payments"].append(deepcopy(security["payments"][-1]))
    with app.test_client() as client:
        response = client.get("/issues/lookup?isin=UA4000228381")
    assert response.status_code == 200 and response.json["existing"] is True
    assert "Unsupported NBU bond data" in response.json["warning"]
    assert "95.90" in response.json["values"]["coupon_schedule"]
    assert database.list_scheduled_payments() == events
    assert database.list_actual_payments() == [actual]

    # A failed remote lookup must still allow the known issue to be purchased locally.
    def unavailable(isin):
        raise NbuError("NBU is unavailable.")

    monkeypatch.setattr(app.extensions["nbu"], "lookup", unavailable)
    with app.test_client() as client:
        assert client.get("/issues/lookup?isin=UA4000228381").json["existing"] is True
        assert client.get("/issues/lookup?isin=UA4000000002").status_code == 502
        assert client.post("/purchases/new", data={
            "isin": issue["isin"], "issue_mode": "existing", "purchase_date": "2024-01-01",
            "purchase_price": "1000", "quantity": "2", "bank_name": "Bank B",
        }).status_code == 302
    assert database.list_scheduled_payments() == events
    assert database.list_actual_payments() == [actual]


def test_known_lookup_ignores_equivalent_decimal_formatting(app, database, security, monkeypatch):
    # Store semantically equal values with different decimal formatting from the source.
    normalized = parse_bond_data(security)
    normalized['issue']['nominal'] = '1000.00'
    normalized['issue']['nominal_rate'] = '19.180'
    issue = database.add_issue(**normalized['issue'])
    for event in normalized['payments']:
        database.add_scheduled_payment(issue_id=issue['id'], **event)
    monkeypatch.setattr(app.extensions['nbu'], '_download', lambda: [security])

    # Formatting differences must not imply a financial discrepancy.
    with app.test_client() as client:
        response = client.get('/issues/lookup?isin=UA4000228381')
    assert response.status_code == 200
    assert response.json['existing'] is True
    assert response.json['warning'] == ''


@pytest.mark.parametrize("failure", [URLError("Offline"), TimeoutError("Timed out")])
def test_network_errors_become_readable_lookup_errors(monkeypatch, failure):
    # Raise transport errors at the actual HTTP boundary without opening a connection.
    def unavailable(*args, **kwargs):
        raise failure

    monkeypatch.setattr("ovdp_calculator.nbu.urlopen", unavailable)

    # Both failures must support the same retry/manual-entry flow rather than producing HTTP 500.
    with pytest.raises(NbuError, match="Could not load the NBU directory"):
        NbuClient().lookup("UA4000228381")


def test_signed_source_values_cannot_be_replaced_by_posted_manual_fields(app, database, security, monkeypatch):
    # Load an authoritative source snapshot and submit conflicting fields alongside its signature.
    monkeypatch.setattr(app.extensions["nbu"], "_download", lambda: [security])
    with app.test_client() as client:
        response = client.get("/issues/lookup?isin=UA4000228381").json
        form = {
            "isin": "UA4000228381", "entry_mode": "nbu", "issue_mode": "auto", "loaded_isin": "UA4000228381",
            "issue_token": response["issue_token"], "nominal": "1", "currency": "USD", "coupon_schedule": "",
            "purchase_date": "2024-01-01", "purchase_price": "999", "quantity": "3", "bank_name": "Bank A",
        }
        assert client.post("/purchases/new", data=form).status_code == 302

    # Save source data exactly while keeping the independently supplied purchase parameters.
    issue, = database.list_issues()
    assert issue["currency"] == "UAH" and issue["nominal"] == "1000"
    assert len(database.list_scheduled_payments()) == 8
    assert database.list_lots()[0]["purchase_price"] == "999"


@pytest.mark.parametrize("token, isin", [("invalid", "UA4000228381"), (None, "UA4000228382")])
def test_invalid_or_different_issue_snapshot_is_rejected(app, database, security, monkeypatch, token, isin):
    monkeypatch.setattr(app.extensions["nbu"], "_download", lambda: [security])
    with app.test_client() as client:
        response = client.get("/issues/lookup?isin=UA4000228381").json
        form = {
            "isin": isin, "entry_mode": "nbu", "issue_mode": "auto", "loaded_isin": isin,
            "issue_token": token or response["issue_token"], "purchase_date": "2024-01-01",
            "purchase_price": "999", "quantity": "3", "bank_name": "Bank A",
        }
        result = client.post("/purchases/new", data=form)
    assert result.status_code == 400
    assert 'id="isin-error"' in result.get_data(as_text=True)
    assert database.list_issues() == database.list_lots() == database.list_banks() == []


def test_manual_lookup_never_calls_nbu_and_reuses_saved_issue(app, database, security, monkeypatch):
    # Fail any external request so both manual lookup paths must remain local.
    def unexpected_request(isin):
        raise AssertionError("Manual mode must not request NBU data.")

    monkeypatch.setattr(app.extensions["nbu"], "lookup", unexpected_request)

    # Unknown manual issues are editable, while known ones expose their protected saved calendar.
    with app.test_client() as client:
        assert client.get("/issues/lookup?isin=UA4000228381&source=local").json == {"existing": False}
        result = parse_bond_data(security)
        issue = database.add_issue(**result["issue"])
        for event in result["payments"]:
            database.add_scheduled_payment(issue_id=issue["id"], **event)
        response = client.get("/issues/lookup?isin=UA4000228381&source=local")
    assert response.status_code == 200
    assert response.json["existing"] is True
    assert "95.90" in response.json["values"]["coupon_schedule"]


def test_invalid_purchase_retains_read_only_source_preview(app, database, security, monkeypatch):
    monkeypatch.setattr(app.extensions["nbu"], "_download", lambda: [security])
    with app.test_client() as client:
        source = client.get("/issues/lookup?isin=UA4000228381").json
        response = client.post("/purchases/new", data={
            "isin": "UA4000228381", "entry_mode": "nbu", "issue_mode": "auto", "loaded_isin": "UA4000228381",
            "issue_token": source["issue_token"], "purchase_date": "2024-01-01", "purchase_price": "999",
            "quantity": "0", "bank_name": "Bank A",
        })
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert 'data-issue-field="currency">UAH</dd>' in html
    assert "Середньострокові" in html and source["issue_token"] in html
    assert 'value="999"' in html and 'id="quantity-error"' in html
    assert database.list_issues() == []


def test_repeat_purchase_rejects_posted_conflicts_even_with_a_valid_source_token(app, database, security, monkeypatch):
    # Keep the same signed draft open while saving the first purchase of its issue.
    monkeypatch.setattr(app.extensions["nbu"], "_download", lambda: [security])
    with app.test_client() as client:
        source = client.get("/issues/lookup?isin=UA4000228381").json
        form = {
            "isin": "UA4000228381", "entry_mode": "nbu", "issue_mode": "auto", "loaded_isin": "UA4000228381",
            "issue_token": source["issue_token"], "purchase_date": "2024-01-01", "purchase_price": "999",
            "quantity": "3", "bank_name": "Bank A",
        }
        assert client.post("/purchases/new", data=form).status_code == 302
        issues, events, lots = database.list_issues(), database.list_scheduled_payments(), database.list_lots()

        # A signed token must not hide conflicting fields once this ISIN has become a saved issue.
        response = client.post("/purchases/new", data={**form, "currency": "USD", "bank_name": "Unsaved bank"})
    assert response.status_code == 400 and 'id="currency-error"' in response.get_data(as_text=True)
    assert database.list_issues() == issues and database.list_scheduled_payments() == events
    assert database.list_lots() == lots and len(database.list_banks()) == 1


@pytest.mark.parametrize("changed", ["currency", "coupon_amount"])
def test_signed_draft_conflicting_with_an_issue_saved_after_lookup_is_rejected(
    app, database, security, monkeypatch, changed,
):
    # Load an unsaved draft, then save a different complete issue before that draft is submitted.
    monkeypatch.setattr(app.extensions["nbu"], "_download", lambda: [security])
    with app.test_client() as client:
        source = client.get("/issues/lookup?isin=UA4000228381").json
    data = parse_bond_data(security)
    if changed == "currency":
        data["issue"]["currency"] = "USD"
    else:
        data["payments"][0]["amount"] = "80"
    issue = database.add_issue(**data["issue"])
    events = [database.add_scheduled_payment(issue_id=issue["id"], **event) for event in data["payments"]]

    # Compare the signed calendar too, even though the browser sends no editable source fields.
    with app.test_client() as client:
        response = client.post("/purchases/new", data={
            "isin": issue["isin"], "entry_mode": "nbu", "issue_mode": "auto", "loaded_isin": issue["isin"],
            "issue_token": source["issue_token"], "purchase_date": "2024-01-01", "purchase_price": "999",
            "quantity": "3", "bank_name": "Unsaved bank",
        })
    assert response.status_code == 400 and "conflicts with the saved ISIN" in response.get_data(as_text=True)
    assert database.list_issues() == [issue] and database.list_scheduled_payments() == events
    assert database.list_lots() == database.list_banks() == []
