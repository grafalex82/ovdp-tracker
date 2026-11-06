"""Exercise the complete portfolio lifecycle through HTTP and the public storage API."""

from datetime import date
from decimal import Decimal
from fractions import Fraction
import json

import pytest

from ovdp_tracker import create_app
from ovdp_tracker.analytics import (
    calculate_allocations, get_position_returns, get_purchase_cash_flows, monthly_payment_totals, summarize_balances,
)
from ovdp_tracker.db import Database
from ovdp_tracker.portfolio import get_lot_details, get_position_details, list_payment_events, list_positions
from ovdp_tracker.purchases import issue_form_values


TODAY = date(2026, 11, 6)
ISIN = "UA4000000001"


@pytest.fixture
def historical_directory(monkeypatch, app):
    # Keep historical confirmations and current balances independent of the execution date.
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return the fixed local day for the complete lifecycle scenario."""
            return cls(2026, 11, 6)

    monkeypatch.setattr("ovdp_tracker.web.date", LocalDate)
    monkeypatch.setattr("ovdp_tracker.portfolio.date", LocalDate)

    # Replace only transport so lookup still parses and validates an actual NBU-shaped directory.
    security = {
        "cpcode": ISIN, "cptype": "DCP", "nominal": 1000, "val_code": "UAH", "razm_date": "2023-01-01",
        "pgs_date": "2026-10-31", "auk_proc": 16, "pay_period": 365, "cpdescr": "Historical test issue",
        "payments": [
            {"pay_type": "1", "pay_date": "2023-12-01", "pay_val": 70},
            {"pay_type": "1", "pay_date": "2025-06-30", "pay_val": 80},
            {"pay_type": "1", "pay_date": "2026-06-30", "pay_val": 85},
            {"pay_type": "1", "pay_date": "2026-10-31", "pay_val": 90},
            {"pay_type": "2", "pay_date": "2026-10-31", "pay_val": 1000},
        ],
    }
    monkeypatch.setattr(app.extensions["nbu"], "_download", lambda: [security])
    return security


def payment_months(database):
    # Apply the same inclusive date filtering as the payment page before monthly aggregation.
    start, end = date(2025, 6, 1), date(2026, 10, 31)
    groups = list_payment_events(database, currency="UAH", start=start, end=end, today=TODAY)
    return {row["month"]: row for row in monthly_payment_totals(groups, start=start, end=end, today=TODAY)}


def test_complete_shared_issue_lifecycle(app, app_config, database, database_path, historical_directory):
    # Load a signed NBU draft and save two independent historical purchases of the same issue.
    client = app.test_client()
    lookup = client.get(f"/issues/lookup?isin={ISIN}")
    assert lookup.status_code == 200 and lookup.json["existing"] is False
    assert database.list_issues() == database.list_scheduled_payments() == database.list_lots() == []
    first_values = {
        "isin": ISIN, "issue_mode": "auto", "loaded_isin": ISIN, "issue_token": lookup.json["issue_token"],
        "purchase_date": "2024-01-01", "purchase_price": "950", "quantity": "10", "bank_name": "Bank A",
    }
    assert client.post("/purchases/new", data=first_values).location == "/?saved=1&currency=UAH"
    saved_events = database.list_scheduled_payments()
    second_values = {
        "isin": ISIN, "issue_mode": "existing", "purchase_date": "2025-08-01", "purchase_price": "990",
        "quantity": "5", "bank_name": "Bank B",
    }
    assert client.post("/purchases/new", data=second_values).status_code == 302
    first, second = database.list_lots()
    neighbor_before = database.get_lot(second["id"])
    issue = database.list_issues()[0]
    assert first["issue_id"] == second["issue_id"] == issue["id"]
    assert len(database.list_issues()) == 1 and len(saved_events) == 5
    assert database.list_scheduled_payments() == saved_events
    assert len(database.list_banks()) == 2

    # Check one portfolio row, the unchanged full calendar and date-specific entitlements.
    home = client.get("/").get_data(as_text=True)
    assert home.count(f'data-isin="{ISIN}"') == 1
    assert "100,00%" in home
    details = get_position_details(database, ISIN, today=TODAY)
    assert details["remaining_quantity"] == 15
    assert [event["quantity"] for event in details["calendar"]] == [0, 10, 15, 15, 15]
    assert len(get_lot_details(database, first["id"], today=TODAY)["payments"]) == 4
    assert len(get_lot_details(database, second["id"], today=TODAY)["payments"]) == 3
    edit_page = client.get(f'/purchases/{second["id"]}/edit').get_data(as_text=True)
    crumbs = edit_page.split('<nav aria-label="Хлебные крошки"', 1)[1].split("</nav>", 1)[0]
    assert f'<a href="/issues/{ISIN}">{ISIN}</a>' in crumbs
    assert f'<a href="/purchases/{second["id"]}">Покупка №{second["id"]}</a>' in crumbs
    assert '<span aria-current="page">Редактирование покупки</span>' in crumbs

    # Record a late coupon, a partial sale and separate confirmations of a shared coupon event.
    coupons = [event for event in saved_events if event["kind"] == "coupon"]
    for lot_id, event, day, amount in (
        (first["id"], coupons[1], "2025-07-01", "795"),
        (first["id"], coupons[2], "2026-07-01", "595"),
        (second["id"], coupons[2], "2026-06-30", "400"),
    ):
        response = client.post(f'/purchases/{lot_id}/payments/{event["id"]}/confirm',
                               data={"actual_date": day, "amount": amount})
        assert response.status_code == 302
    response = client.post(f'/purchases/{first["id"]}/sales/new',
                           data={"sale_date": "2025-07-01", "sale_price": "1050", "quantity": "3"})
    assert response.status_code == 302
    facts = database.list_actual_payments()
    sales = database.list_sales()
    months = payment_months(database)
    assert months["2025-06"]["expected_coupon"] == Decimal("0.00")
    assert months["2025-07"]["actual_coupon"] == months["2025-07"]["actual_total"] == Decimal("795.00")
    assert months["2026-06"]["actual_coupon"] == Decimal("400.00")
    assert months["2026-07"]["actual_coupon"] == Decimal("595.00")
    assert months["2026-08"]["expected_total"] == months["2026-08"]["actual_total"] == Decimal("0.00")

    # Correct the purchase cost and shared coupon amounts while preserving every fact and neighboring purchase.
    response = client.post(f'/purchases/{first["id"]}/edit', data={
        "purchase_date": "2024-01-01", "purchase_price": "960", "quantity": "10", "bank_name": "Bank A",
    })
    assert response.status_code == 302
    values = issue_form_values(issue, saved_events)
    values["nominal_rate"] = "17"
    values["coupons"][2]["amount"] = "87"
    values["coupons"][3]["amount"] = "95"
    response = client.post(f"/issues/{ISIN}/edit", data={**values, "coupons": json.dumps(values["coupons"])})
    assert response.status_code == 302
    corrected_issue = database.get_issue(issue["id"])
    corrected_events = database.list_scheduled_payments()
    assert corrected_issue["nominal_rate"] == "17"
    assert [event["id"] for event in corrected_events] == [event["id"] for event in saved_events]
    assert [event["amount"] for event in corrected_events] == ["70", "80", "87", "95", "1000"]
    neighbor_after = {**neighbor_before, "nominal_rate": "17"}
    assert database.get_lot(second["id"]) == neighbor_after
    assert database.list_actual_payments() == facts and database.list_sales() == sales
    updated_months = payment_months(database)
    assert updated_months["2026-10"]["expected_coupon"] == Decimal("1140.00")
    assert updated_months["2026-10"]["expected_redemption"] == Decimal("12000.00")
    assert all(updated_months[month] == row for month, row in months.items() if month != "2026-10")

    # Compare exact balances and full-horizon yields against independent arithmetic reference values.
    position = get_position_details(database, ISIN, today=TODAY)
    assert summarize_balances(position["lots"]) == {
        "quantity": 12, "cost": Decimal("11670.00"), "nominal": Decimal("12000.00"),
    }
    allocation = calculate_allocations(list_positions(database, today=TODAY), currency="UAH")
    assert allocation[ISIN] == {"cost": Decimal("11670.00"), "share": Fraction(1)}
    returns = get_position_returns(database, ISIN, today=TODAY)
    assert returns["expected_profit"] == Decimal("3530.00")
    assert returns["flows"] == [
        {"date": day, "amount": Decimal(amount)} for day, amount in (
            ("2024-01-01", "-9600.00"), ("2025-07-01", "3945.00"), ("2025-08-01", "-4950.00"),
            ("2026-06-30", "400.00"), ("2026-07-01", "595.00"), ("2026-10-31", "13140.00"),
        )
    ]
    assert returns["xirr"]["rate"] == pytest.approx(0.1148517206921239, abs=1e-8)
    assert returns["simple_return"]["cost_days"] == Decimal("10781040.00")
    assert returns["simple_return"]["rate"] == pytest.approx(Decimal("0.11951073365834836"))
    page = client.get(f"/issues/{ISIN}").get_data(as_text=True)
    assert "11,49%" in page and "11,95%" in page and "3530,00 UAH" in page

    # Redeem only the first purchase, keeping earned coupons and the neighbor's outstanding position.
    redemption = saved_events[-1]
    response = client.post(f'/purchases/{first["id"]}/payments/{redemption["id"]}/confirm',
                           data={"actual_date": "2026-10-31", "amount": "7000"})
    assert response.status_code == 302
    assert get_lot_details(database, first["id"], today=TODAY)["closed"] is True
    assert get_position_details(database, ISIN, today=TODAY)["remaining_quantity"] == 5
    neighbor_facts = database.list_actual_payments(second["id"])
    neighbor_flows = get_purchase_cash_flows(database, second["id"], today=TODAY)
    remaining_returns = get_position_returns(database, ISIN, today=TODAY)
    assert remaining_returns["purchase_ids"] == [second["id"]]
    assert remaining_returns["expected_profit"] == Decimal("925.00")
    assert payment_months(database)["2026-10"]["actual_redemption"] == Decimal("7000.00")
    assert payment_months(database)["2026-10"]["expected_redemption"] == Decimal("5000.00")

    # Recreate the Flask application and storage connections before deleting the closed purchase explicitly.
    database.close()
    restarted = create_app(app_config)
    restarted_client = restarted.test_client()
    assert restarted_client.get("/").get_data(as_text=True).count(f'data-isin="{ISIN}"') == 1
    assert restarted_client.get(f'/purchases/{first["id"]}').status_code == 200
    with Database(database_path) as reopened:
        assert reopened.get_issue(issue["id"]) == corrected_issue
        assert reopened.list_scheduled_payments() == corrected_events
        assert len(reopened.list_actual_payments()) == 4 and reopened.list_sales() == sales
        assert get_position_returns(reopened, ISIN, today=TODAY) == remaining_returns
    response = restarted_client.post(f'/purchases/{first["id"]}/delete', data={"confirm": "delete"})
    assert response.location == "/?deleted=1&currency=UAH"

    # Verify cascading removal excludes only that purchase from history, totals and calculations after reopening.
    with Database(database_path) as reopened:
        assert reopened.get_lot(first["id"]) is None
        assert reopened.get_lot(second["id"]) == neighbor_after
        assert reopened.list_actual_payments() == neighbor_facts and reopened.list_sales() == []
        assert reopened.list_issues() == [corrected_issue]
        assert reopened.list_scheduled_payments() == corrected_events and len(reopened.list_banks()) == 2
        assert get_purchase_cash_flows(reopened, second["id"], today=TODAY) == neighbor_flows
        assert get_position_returns(reopened, ISIN, today=TODAY) == remaining_returns
        assert summarize_balances(get_position_details(reopened, ISIN, today=TODAY)["lots"]) == {
            "quantity": 5, "cost": Decimal("4950.00"), "nominal": Decimal("5000.00"),
        }
        months = payment_months(reopened)
        assert months["2025-07"]["actual_coupon"] == Decimal("0.00")
        assert months["2026-06"]["actual_coupon"] == Decimal("400.00")
        assert months["2026-07"]["actual_coupon"] == months["2026-10"]["actual_redemption"] == Decimal("0.00")
        assert months["2026-10"]["expected_coupon"] == Decimal("475.00")
    assert restarted_client.get(f'/purchases/{first["id"]}').status_code == 404
