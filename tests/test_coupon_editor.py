"""Verify the row editor's server contract, recovery data and shared calendar boundaries."""

from html import unescape
import json
import re

import pytest

from ovdp_tracker.purchases import IssueValidationError, validate_issue


def rendered_coupons(html):
    match = re.search(r'id="coupons"[^>]*value="([^"]*)"', html, re.DOTALL)
    return json.loads(unescape(match[1]))


@pytest.fixture
def calendar_form():
    return {
        "isin": "UA4000000001", "entry_mode": "manual", "nominal": "1000", "currency": "UAH",
        "purchase_date": "2024-02-15", "purchase_price": "950", "quantity": "10", "bank_name": "Calendar bank",
        "coupons": json.dumps([
            {'date': '2027-06-01', 'amount': '85,50'},
            {'date': '2023-06-01', 'amount': '0'},
            {'date': '2026-06-01', 'amount': '80'},
        ]), "maturity_date": "2025-01-01",
    }


def test_manual_calendar_sorts_pairs_and_infers_maturity_independently_of_purchase(app, database, calendar_form):
    # Submit an unordered schedule with a historical and zero coupon and an obsolete manual maturity draft.
    with app.test_client() as client:
        response = client.post("/purchases/new", data=calendar_form)
    assert response.status_code == 302

    # Retain every amount with its own date and derive redemption from the last coupon, not the row order.
    issue, = database.list_issues()
    assert issue["maturity_date"] == "2027-06-01"
    assert [(event["kind"], event["planned_date"], event["amount"]) for event in
            database.list_scheduled_payments()] == [
        ("coupon", "2023-06-01", "0"), ("coupon", "2026-06-01", "80"),
        ("coupon", "2027-06-01", "85.50"), ("redemption", "2027-06-01", "1000"),
    ]


@pytest.mark.parametrize("rows", [
    [["2027-06-01", "85,50"], ["", "80"], ["2026-06-01", ""]],
    [["2027-06-01", "85"], ["2027-06-01", "80"]],
    [["2027-06-01", "85"], ["2026-06-01", "-1"]],
    [["2027-06-01", "80.123"], ["", ""]],
    [["invalid-date", "80"], ["2027-06-01", '<script>alert("coupon")</script>']],
])
def test_rejected_editor_preserves_every_raw_pair_and_order(app, database, calendar_form, rows):
    # Submit structured controls directly, including missing values and unsafe display text.
    coupons = [{"date": day, "amount": amount} for day, amount in rows]
    calendar_form["coupons"] = json.dumps(coupons)
    with app.test_client() as client:
        response = client.post("/purchases/new", data=calendar_form)
    html = response.get_data(as_text=True)

    # Preserve even unfinished pairs and unsafe text as escaped data without persisting any partial purchase.
    assert response.status_code == 400
    assert rendered_coupons(html) == coupons
    assert '<script>alert("coupon")</script>' not in html
    assert 'data-unsaved-error="true"' in html
    assert database.list_issues() == database.list_scheduled_payments() == database.list_banks() == []
    assert database.list_lots() == []


def test_disabled_manual_draft_cannot_override_submitted_calendar(app, database, calendar_form):
    # Treat the alternate mode's draft as presentation metadata even when it claims a different calendar.
    calendar_form["_manual_draft"] = json.dumps({"coupons": [{"date": "2099-01-01", "amount": "999999"}]})
    with app.test_client() as client:
        response = client.post("/purchases/new", data=calendar_form)
    assert response.status_code == 302
    assert database.list_issues()[0]["maturity_date"] == "2027-06-01"
    assert all(event["amount"] != "999999" for event in database.list_scheduled_payments())


def test_explicit_issue_maturity_rejects_later_coupon_without_inference(calendar_form):
    # Reuse issue validation for the future shared edit form with its independent explicit maturity.
    calendar_form.update({
        "issue_mode": "manual", "infer_maturity_from_coupons": "0", "coupons": json.loads(calendar_form["coupons"]),
    })
    with pytest.raises(IssueValidationError) as caught:
        validate_issue(calendar_form)
    assert caught.value.errors["coupons"] == "Coupon dates must not be after maturity."


def test_nbu_redemption_stays_independent_of_manual_rows(app, database, calendar_form, monkeypatch):
    # Supply a source whose redemption is later than its final coupon and a conflicting manual draft.
    source = {
        "issue": {
            "isin": calendar_form["isin"], "currency": "UAH", "nominal": "1000", "maturity_date": "2029-07-01",
            "issue_date": "2023-01-01", "nominal_rate": "", "coupon_period": None, "bond_type": "",
        },
        "payments": [
            {"kind": "coupon", "planned_date": "2027-06-01", "amount": "80"},
            {"kind": "redemption", "planned_date": "2029-07-01", "amount": "1000"},
        ],
    }
    monkeypatch.setattr(app.extensions["nbu"], "lookup", lambda isin: source)

    # Submit the signed source and assert that UI row metadata cannot infer a new source maturity.
    with app.test_client() as client:
        loaded = client.get(f'/issues/lookup?isin={calendar_form["isin"]}').json
        calendar_form.update({
            "entry_mode": "nbu", "issue_mode": "auto", "loaded_isin": calendar_form["isin"],
            "issue_token": loaded["issue_token"], "coupons": json.dumps([{"date": "2030-01-01", "amount": "500"}]),
        })
        response = client.post("/purchases/new", data=calendar_form)
    assert response.status_code == 302
    assert database.list_issues()[0]["maturity_date"] == "2029-07-01"
    assert [(event["planned_date"], event["amount"]) for event in database.list_scheduled_payments()] == [
        ("2027-06-01", "80"), ("2029-07-01", "1000"),
    ]


def test_editor_assets_and_structured_input_are_available_without_writes(app, database):
    # Load the shared editor before purchase initialization and dirty-form protection.
    with app.test_client() as client:
        html = client.get("/purchases/new").get_data(as_text=True)
        assert "data-coupon-editor" in html and "data-coupon-template" in html
        assert rendered_coupons(html) == []
        assert "<textarea" not in html
        assert html.index("coupon-calendar.js") < html.index("purchase.js") < html.index("unsaved-form.js")
        assert client.get("/static/coupon-calendar.js").status_code == 200

    # Rendering the editor and its assets must not create a draft in storage.
    assert database.list_issues() == database.list_lots() == []


@pytest.mark.parametrize("payload", [
    "not-json", "2027-06-01 80", "", "null", "true", "{}", "123", "[null]", '[["2027-06-01", "80"]]',
    '[{"date": "2027-06-01", "amount": 80}]', '[{"date": 20270601, "amount": "80"}]',
    '[{"date": "2027-06-01", "amount": "80", "extra": true}]',
])
@pytest.mark.parametrize("issue_mode", ["manual", "auto"])
def test_malformed_coupon_payload_is_rejected_without_server_error(app, database, calendar_form, payload, issue_mode):
    # Reject malformed transport or object shapes without silently creating a zero-coupon issue.
    calendar_form.update({"coupons": payload, "issue_mode": issue_mode})
    with app.test_client() as client:
        response = client.post("/purchases/new", data=calendar_form)
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert 'id="coupons-error"' in html or (issue_mode == "auto" and 'id="isin-error"' in html)
    assert database.list_issues() == database.list_scheduled_payments() == database.list_lots() == []


@pytest.mark.parametrize("coupons", ["2027-06-01 80", "[]", {}, None, [{"date": "2027-06-01", "amount": 80}]])
def test_domain_validation_requires_coupon_objects_without_transport_parsing(calendar_form, coupons):
    # Keep JSON decoding at the HTTP boundary and require structures from domain callers.
    calendar_form["coupons"] = coupons
    with pytest.raises(IssueValidationError) as caught:
        validate_issue(calendar_form)
    assert "coupons" in caught.value.errors
