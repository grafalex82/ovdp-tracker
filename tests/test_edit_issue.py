"""Verify shared issue corrections, stable calendar identities and atomic history protection."""

from html import unescape
import json
import re

import pytest

from ovdp_tracker.db import Database, DatabaseError
from ovdp_tracker.issues import edit_issue
from ovdp_tracker.portfolio import get_lot_details
from ovdp_tracker.purchases import IssueValidationError, issue_form_values


@pytest.fixture
def shared_issue(database, lot, lot_data):
    # Build two independent purchases and a calendar with a confirmed event on only the second purchase.
    bank = database.add_bank("Neighbor bank")
    neighbor = database.add_lot(issue_id=lot["issue_id"], bank_id=bank["id"],
                                **{**lot_data, "purchase_date": "2024-03-01", "purchase_price": "990", "quantity": 5})
    events = [database.add_scheduled_payment(issue_id=lot["issue_id"], kind=kind, planned_date=day, amount=amount)
              for kind, day, amount in [("coupon", "2025-01-01", "80"), ("coupon", "2026-01-01", "90"),
                                        ("redemption", "2027-06-01", "1000")]]
    actual = database.add_actual_payment(lot_id=neighbor["id"], scheduled_payment_id=events[0]["id"],
                                         actual_date="2025-01-03", amount="795")
    sale = database.add_sale(lot_id=lot["id"], sale_date="2025-02-01", sale_price="1050", quantity=3)
    return {"lot": lot, "neighbor": neighbor, "events": events, "actual": actual, "sale": sale}


def snapshot(database):
    return [reader() for reader in (database.list_issues, database.list_scheduled_payments, database.list_lots,
                                   database.list_banks, database.list_actual_payments, database.list_sales)]


def form_values(database, shared_issue):
    return issue_form_values(database.get_issue(shared_issue["lot"]["issue_id"]), shared_issue["events"])


def post_values(values):
    return {**values, "coupons": json.dumps(values["coupons"])}


def test_correction_recalculates_both_purchases_and_preserves_facts(database, shared_issue, database_path):
    # Change one unit amount while keeping every other calendar date and independent operation intact.
    values = form_values(database, shared_issue)
    values["coupons"][0]["amount"] = "85"
    before = snapshot(database)
    edit_issue(database, shared_issue["lot"]["issue_id"], values)

    # Reuse event identities and preserve neighboring amounts, actual receipts, purchases and sales exactly.
    events = database.list_scheduled_payments()
    assert events == [{**event, "amount": "85"} if index == 0 else event
                      for index, event in enumerate(shared_issue["events"])]
    assert snapshot(database)[2:] == before[2:]
    first = get_lot_details(database, shared_issue["lot"]["id"])["payments"]
    second = get_lot_details(database, shared_issue["neighbor"]["id"])["payments"]
    assert first[0]["planned_amount"] == "850.00" and first[1]["planned_amount"] == "630.00"
    assert second[0]["actual"] == shared_issue["actual"]
    assert second[0]["planned_amount"] == "425.00" and second[1]["planned_amount"] == "450.00"
    assert database.list_sales() == [shared_issue["sale"]]

    # Reopening compatible storage retains the corrected calendar and the original confirmation link.
    with Database(database_path) as reopened:
        assert reopened.list_scheduled_payments() == events
        assert reopened.list_actual_payments() == [shared_issue["actual"]]


@pytest.mark.parametrize("change", ["remove", "move"])
def test_neighbor_history_conflict_rolls_back_every_shared_change(database, shared_issue, change):
    # Propose an otherwise valid metadata correction together with a conflict in the neighboring purchase.
    values = form_values(database, shared_issue)
    values.update(nominal="1200", currency="EUR", bond_type="Corrected type")
    if change == "remove":
        values["coupons"].pop(0)
    else:
        values["coupons"][0]["date"] = "2025-01-02"
    before = snapshot(database)

    # Reject the whole correction and retain every saved field and identity.
    with pytest.raises(IssueValidationError):
        edit_issue(database, shared_issue["lot"]["issue_id"], values)
    assert snapshot(database) == before


def test_neighbor_sale_history_conflict_rolls_back_after_reconciliation(database, shared_issue):
    # Simulate inconsistent neighboring history to ensure the shared editor rechecks every sale.
    database.add_sale(lot_id=shared_issue["neighbor"]["id"], sale_date="2025-02-01", sale_price="1100", quantity=6)
    values = form_values(database, shared_issue)
    values.update(nominal="1200", currency="USD")
    values["coupons"][0]["amount"] = "85"
    before = snapshot(database)
    with pytest.raises(IssueValidationError):
        edit_issue(database, shared_issue["lot"]["issue_id"], values)
    assert snapshot(database) == before


def test_all_purchase_histories_are_checked_inside_the_transaction(database, lot, lot_data):
    # Use an unconfirmed calendar so the neighboring purchase's maturity boundary is the only conflict.
    database.add_scheduled_payment(issue_id=lot["issue_id"], kind="redemption",
                                   planned_date=lot["maturity_date"], amount="1000")
    neighbor = database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"],
                                **{**lot_data, "purchase_date": "2026-01-01"})
    values = issue_form_values(lot, database.list_scheduled_payments())
    values.update(maturity_date="2025-01-01", nominal="1200", currency="USD")
    before = snapshot(database)
    with pytest.raises(IssueValidationError, match="invalid fields"):
        edit_issue(database, lot["issue_id"], values)
    assert snapshot(database) == before
    assert database.get_lot(neighbor["id"]) == neighbor


def test_unconfirmed_calendar_add_remove_and_separate_maturity(database, shared_issue):
    # Replace only an unconfirmed coupon and move the independent unconfirmed redemption date.
    values = form_values(database, shared_issue)
    kept = shared_issue["events"][0]
    values.update(maturity_date="2028-07-01", redemption_amount="1100", currency="USD", nominal="1100",
                  issue_date="2023-01-01", nominal_rate="12.5", coupon_period="182", bond_type="Test type")
    values["coupons"] = [values["coupons"][0], {"date": "2028-06-01", "amount": "0"}]
    issue = edit_issue(database, shared_issue["lot"]["issue_id"], values)

    # Preserve the confirmed event and allow zero coupons independently of explicit maturity.
    assert issue["maturity_date"] == "2028-07-01" and issue["currency"] == "USD"
    assert issue["issue_date"] == "2023-01-01" and issue["coupon_period"] == 182
    assert issue["nominal_rate"] == "12.5" and issue["bond_type"] == "Test type"
    assert database.list_scheduled_payments()[0] == kept
    assert [(event["kind"], event["planned_date"], event["amount"])
            for event in database.list_scheduled_payments()] == [
        ("coupon", "2025-01-01", "80"), ("coupon", "2028-06-01", "0"), ("redemption", "2028-07-01", "1100"),
    ]
    assert database.list_actual_payments() == [shared_issue["actual"]]


def test_confirmed_redemption_date_is_protected(database, shared_issue):
    # Confirm a redemption while keeping the earlier coupon fact on the neighboring purchase.
    event = shared_issue["events"][-1]
    database.add_actual_payment(lot_id=shared_issue["neighbor"]["id"], scheduled_payment_id=event["id"],
                                actual_date="2027-06-02", amount="5000")
    values = form_values(database, shared_issue)
    values["maturity_date"] = "2028-01-01"
    before = snapshot(database)
    with pytest.raises(IssueValidationError):
        edit_issue(database, shared_issue["lot"]["issue_id"], values)
    assert snapshot(database) == before


def test_issue_http_navigation_save_and_missing_position(app, database, shared_issue):
    # Open the explicit shared editor through its position and check its protected parent hierarchy.
    isin = shared_issue["lot"]["isin"]
    path = f"/issues/{isin}/edit"
    values = form_values(database, shared_issue)
    values.update(currency="EUR", nominal="1100")
    with app.test_client() as client:
        assert f'href="{path}"' in client.get(f"/issues/{isin}").get_data(as_text=True)
        page = client.get(path).get_data(as_text=True)
        assert f'href="/issues/{isin}"' in page
        assert '<span aria-current="page">Редактирование облигации</span>' in page
        assert 'data-unsaved-form data-unsaved-error="false"' in page
        assert "coupon-calendar.js" in page and "issue.js" in page
        assert client.get("/issues/UNKNOWN/edit").status_code == 404
        assert client.post("/issues/UNKNOWN/edit", data=post_values(values)).status_code == 404
        response = client.post(path, data=post_values(values), follow_redirects=True)
        assert response.status_code == 200
        assert response.request.path == "/" and response.request.args["currency"] == "EUR"
        assert "Общие реквизиты облигации сохранены." in response.get_data(as_text=True)


@pytest.mark.parametrize("coupons", [
    '[{"date":"2026-01-01","amount":"85"},{"date":"","amount":""}]',
    '[{"date":"2026-01-01","amount":"85"},{"date":"2026-01-01","amount":"90"}]',
    '[{"date":"2029-01-01","amount":"85"}]',
    '[{"date":"2026-01-01","amount":"-1"}]',
    '[{"date":"2026-01-01","amount":"80.010"}]',
    'malformed JSON',
])
def test_rejected_editor_keeps_raw_pairs_and_dirty_baseline(app, database, shared_issue, coupons):
    # Send invalid or unfinished calendar rows along with the original dirty-form baseline.
    values = {**post_values(form_values(database, shared_issue)), "coupons": coupons, "_unsaved_baseline": "original"}
    before = snapshot(database)
    with app.test_client() as client:
        response = client.post(f'/issues/{shared_issue["lot"]["isin"]}/edit', data=values)
    html = response.get_data(as_text=True)

    # Preserve every submitted pair and leave the full saved position unchanged.
    assert response.status_code == 400
    payload = unescape(re.search(r'id="coupons"[^>]*value="([^"]*)"', html, re.DOTALL)[1])
    assert payload == coupons if coupons == "malformed JSON" else json.loads(payload) == json.loads(coupons)
    assert 'data-unsaved-error="true"' in html and 'value="original"' in html
    assert snapshot(database) == before


def test_mid_write_failure_rolls_back_and_retains_editor(app, database, shared_issue, monkeypatch):
    # Fail after the storage reconciliation to prove rollback covers metadata and calendar changes together.
    original = Database.update_issue

    def fail_after_update(self, issue_id, **values):
        original(self, issue_id, **values)
        raise DatabaseError("Simulated issue write failure.")

    monkeypatch.setattr(Database, "update_issue", fail_after_update)
    values = form_values(database, shared_issue)
    values.update(nominal="1200", currency="EUR")
    values["coupons"][0]["amount"] = "85"
    before = snapshot(database)
    with app.test_client() as client:
        response = client.post(f'/issues/{shared_issue["lot"]["isin"]}/edit', data=post_values(values))
    assert response.status_code == 503 and 'value="1200"' in response.get_data(as_text=True)
    assert 'data-unsaved-error="true"' in response.get_data(as_text=True)
    assert snapshot(database) == before


def test_saved_isin_cannot_be_reassigned(database, shared_issue):
    values = {**form_values(database, shared_issue), "isin": "UA4000000002"}
    before = snapshot(database)
    with pytest.raises(IssueValidationError):
        edit_issue(database, shared_issue["lot"]["issue_id"], values)
    assert snapshot(database) == before
