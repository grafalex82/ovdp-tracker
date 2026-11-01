"""Verify that protected form navigation never persists drafts or clears failure recovery metadata."""

import json
import re
from html import unescape

import pytest

from ovdp_calculator.db import Database, DatabaseError


def storage_snapshot(database):
    return [reader() for reader in (
        database.list_issues, database.list_scheduled_payments, database.list_lots,
        database.list_banks, database.list_actual_payments, database.list_sales,
    )]


def test_leaving_new_form_and_looking_up_saved_issue_do_not_write_drafts(app, database, lot, payment):
    # Include existing receipts and sales so leaving a draft must preserve every kind of stored history.
    database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="redemption", planned_date="2027-06-01", amount="1000",
    )
    database.add_actual_payment(
        lot_id=lot["id"], scheduled_payment_id=payment["id"], actual_date="2024-08-16", amount="795",
    )
    database.add_sale(lot_id=lot["id"], sale_date="2025-01-01", sale_price="1050", quantity=3)
    before = storage_snapshot(database)

    # Follow the form's cancel and parent links after local preview requests without submitting a purchase.
    with app.test_client() as client:
        html = client.get("/purchases/new").get_data(as_text=True)
        assert "data-unsaved-form" in html and 'data-unsaved-error="false"' in html
        assert 'name="_unsaved_baseline"' in html
        assert "Остаться" in html and "Выйти без сохранения" in html
        assert '>Отмена</a>' in html
        assert client.get("/static/unsaved-form.js").status_code == 200
        assert client.get(f'/issues/lookup?isin={lot["isin"]}&source=local').status_code == 200
        assert client.get('/issues/lookup?isin=UNSAVED&source=local').json == {"existing": False}
        for href in re.findall(r'<a\b[^>]*href="([^"]+)"', html):
            assert client.get(unescape(href)).status_code == 200

    # Navigation and preview must not create an issue, bank, purchase or operation.
    assert storage_snapshot(database) == before


@pytest.mark.parametrize("failure, expected_status", [("validation", 400), ("storage", 503)])
def test_failed_submission_preserves_original_baseline_and_complete_draft(
    app, database, monkeypatch, failure, expected_status,
):
    # Carry the original baseline and a separate disabled manual draft through a rejected submission.
    baseline = json.dumps([["quantity", ""], ["bank_name", '<script>alert("baseline")</script>']])
    draft = json.dumps({"coupon_schedule": "2027-06-01 85\nunfinished", "nominal": "500", "currency": "EUR"})
    values = {
        "isin": "UA4000000001", "entry_mode": "manual", "issue_mode": "manual", "nominal": "1000",
        "currency": "USD", "coupon_schedule": "2027-06-01 80\n2028-06-01 85", "maturity_date": "",
        "purchase_date": "2024-02-15", "purchase_price": "999,01", "quantity": "0", "bank_name": "Draft bank",
        "_unsaved_baseline": baseline, "_manual_draft": draft,
    }
    if failure == "storage":
        values["quantity"] = "3"

        def fail_write(self, **parameters):
            raise DatabaseError("Simulated purchase storage failure.")

        monkeypatch.setattr(Database, "add_lot", fail_write)

    # Failed saves and subsequent cancellation must leave every storage table unchanged.
    with app.test_client() as client:
        response = client.post("/purchases/new", data=values)
        html = response.get_data(as_text=True)
        assert response.status_code == expected_status
        assert 'data-unsaved-error="true"' in html
        assert baseline in unescape(html) and draft in unescape(html)
        assert '<script>alert("baseline")</script>' not in html
        assert 'value="999,01"' in html and 'value="Draft bank"' in html
        assert "2027-06-01 80\n2028-06-01 85" in html
        assert client.get("/").status_code == 200
    assert storage_snapshot(database) == [[], [], [], [], [], []]


def test_successful_submission_returns_to_unprotected_portfolio_without_repeating_write(app, database):
    # Save only on an explicit valid POST, even when UI metadata is absent or supplied by the client.
    with app.test_client() as client:
        response = client.post("/purchases/new", data={
            "isin": "UA4000000001", "entry_mode": "manual", "nominal": "1000", "currency": "EUR",
            "coupon_schedule": "", "maturity_date": "2027-06-01", "purchase_date": "2024-02-15",
            "purchase_price": "950", "quantity": "3", "bank_name": "Test bank", "_unsaved_baseline": "arbitrary",
        })
        assert response.status_code == 302 and "currency=EUR" in response.location
        for _ in range(2):
            html = client.get(response.location).get_data(as_text=True)
            assert "Покупка сохранена." in html and "data-unsaved-form" not in html

    # Refreshing the destination must show the one purchase that was explicitly saved.
    lot, = database.list_lots()
    assert lot["quantity"] == 3 and lot["currency"] == "EUR"
