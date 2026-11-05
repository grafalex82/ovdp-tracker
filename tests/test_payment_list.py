"""Verify the payments section, its independent receipts, filters and navigation."""

from datetime import date
import re

import pytest

from ovdp_tracker.payments import confirm_payment
from ovdp_tracker.portfolio import list_payment_events


TODAY = date(2026, 7, 1)


@pytest.fixture
def local_today(monkeypatch):
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return the local date used for the payments page and receipt forms."""
            return cls(2026, 7, 1)

    monkeypatch.setattr("ovdp_tracker.portfolio.date", LocalDate)
    monkeypatch.setattr("ovdp_tracker.web.date", LocalDate)


@pytest.fixture
def payment_data(database, lot, lot_data):
    # Two purchases share a historical coupon and separate same-day coupon and redemption events.
    other_bank = database.add_bank("Банк Б")
    neighbor = database.add_lot(issue_id=lot["issue_id"], bank_id=other_bank["id"], **{**lot_data, "quantity": 5})
    past = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date="2026-06-30", amount="80"
    )
    coupon = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date="2027-06-01", amount="85"
    )
    redemption = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="redemption", planned_date="2027-06-01", amount="1000"
    )
    zero = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date="2026-07-01", amount="0"
    )
    before = database.add_scheduled_payment(
        issue_id=lot["issue_id"], kind="coupon", planned_date="2024-01-01", amount="80"
    )
    return lot, neighbor, past, coupon, redemption, zero, before


def rows(html):
    return [row.split("</tr>", 1)[0] for row in html.split('<tr data-lot-id="')[1:]]


def test_list_retains_distinct_pairs_and_actual_date_filter(database, payment_data):
    # A receipt in July replaces only the first purchase's June expectation.
    lot, neighbor, past, coupon, redemption, zero, before = payment_data
    actual = confirm_payment(database, lot["id"], past["id"], {"actual_date": "2026-07-01", "amount": "795.00"}, TODAY)
    groups = list_payment_events(database, currency="UAH", today=TODAY)
    assert [(item["lot_id"], item["planned_amount"]) for item in groups["overdue"]] == [(neighbor["id"], "400.00")]
    assert groups["overdue"][0]["bank_name"] == "Банк Б" and groups["history"][0]["bank_name"] == "Банк А"
    assert [(item["lot_id"], item["actual"]["amount"]) for item in groups["history"]] == [(lot["id"], "795.00")]
    assert groups["history"][0]["actual"] == actual
    assert all(item["id"] != before["id"] for events in groups.values() for item in events)
    assert len([item for item in groups["upcoming"] if item["id"] in (coupon["id"], redemption["id"])]) == 4
    assert all(item["status"] == "zero" for item in groups["upcoming"] if item["id"] == zero["id"])

    # Both boundaries are inclusive and the actual date selects the receipt independently of its plan.
    june = list_payment_events(database, currency="UAH", start=date(2026, 6, 30), end=date(2026, 6, 30), today=TODAY)
    july = list_payment_events(database, currency="UAH", start=TODAY, end=TODAY, today=TODAY)
    assert len(june["overdue"]) == 1 and not june["history"]
    assert len(july["history"]) == 1 and not july["overdue"]
    assert len(july["upcoming"]) == 2


def test_currency_and_closed_purchase_history(app, database, payment_data, issue_data, bank, lot_data, local_today):
    # Close one purchase after its coupon and add a separate USD issue with its own entitlement.
    lot, neighbor, past, _, _, _, _ = payment_data
    database.add_sale(lot_id=lot["id"], sale_date="2026-06-30", sale_price="1000", quantity=10)
    usd = database.add_issue(**{**issue_data, "isin": "UA4000000002", "currency": "USD"})
    usd_lot = database.add_lot(issue_id=usd["id"], bank_id=bank["id"], **lot_data)
    usd_event = database.add_scheduled_payment(issue_id=usd["id"], kind="coupon", planned_date="2026-06-30", amount="2")
    uah = list_payment_events(database, currency="UAH", today=TODAY)
    dollars = list_payment_events(database, currency="USD", today=TODAY)
    assert {(item["lot_id"], item["id"]) for item in uah["overdue"]} == {
        (lot["id"], past["id"]), (neighbor["id"], past["id"])
    }
    assert [(item["lot_id"], item["id"]) for item in dollars["overdue"]] == [(usd_lot["id"], usd_event["id"])]
    assert all(not events for events in list_payment_events(database, currency="EUR", today=TODAY).values())
    with app.test_client() as client:
        page = client.get("/payments?currency=USD").get_data(as_text=True)
    assert usd["isin"] in page and lot["isin"] not in page
    assert 'value="USD" selected' in page and "Продажа" not in page


def test_http_date_boundaries_closed_history_and_today(
    app, database, payment_data, local_today, issue_data, bank, lot_data,
):
    # Close the neighboring purchase after its June coupon and record the first receipt in July.
    lot, neighbor, past, _, _, _, _ = payment_data
    database.add_sale(lot_id=neighbor["id"], sale_date="2026-06-30", sale_price="1000", quantity=5)
    confirm_payment(database, lot["id"], past["id"], {"actual_date": "2026-07-01", "amount": "795"}, TODAY)
    with app.test_client() as client:
        june = client.get("/payments?currency=UAH&start=2026-06-30&end=2026-06-30").get_data(as_text=True)
        july = client.get("/payments?currency=UAH&start=2026-07-01&end=2026-07-01").get_data(as_text=True)
        assert len(rows(june)) == 1 and f'Покупка №{neighbor["id"]}' in june
        assert "400,00" in june and "Требует подтверждения" in june and "795,00" not in june
        assert "795,00" in july and f'Покупка №{lot["id"]}' in july
        assert "400,00" not in july and "Нулевой купон — подтверждение не требуется" in july

    # A positive event planned today is upcoming, not overdue, and is included at both boundaries.
    issue = database.add_issue(**{**issue_data, "isin": "UA4000000002"})
    today_lot = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], **lot_data)
    event = database.add_scheduled_payment(issue_id=issue["id"], kind="coupon", planned_date="2026-07-01", amount="80")
    groups = list_payment_events(database, currency="UAH", start=TODAY, end=TODAY, today=TODAY)
    today_event, = [item for item in groups["upcoming"] if item["id"] == event["id"]]
    assert today_event["lot_id"] == today_lot["id"] and today_event["status"] == "pending"


def test_page_navigation_confirmation_and_correction(app, database, payment_data, local_today):
    # Follow the payments tab and confirm only one purchase of the shared event.
    lot, neighbor, past, coupon, redemption, zero, _ = payment_data
    with app.test_client() as client:
        page = client.get("/payments").get_data(as_text=True)
        assert '<span aria-current="page">Выплаты</span>' in page
        assert '<span aria-current="page">Выплаты</span></li>' in page
        assert '<a href="/">Портфель</a>' in page
        assert f'href="/issues/{lot["isin"]}"' in page and f'href="/purchases/{lot["id"]}"' in page
        assert len([row for row in rows(page) if f'data-payment-id="{coupon["id"]}"' in row]) == 2
        assert len([row for row in rows(page) if f'data-payment-id="{redemption["id"]}"' in row]) == 2
        zero_rows = [row for row in rows(page) if f'data-payment-id="{zero["id"]}"' in row]
        assert len(zero_rows) == 2 and all("подтверждение не требуется" in row for row in zero_rows)
        assert all("Подтвердить выплату</a>" not in row for row in zero_rows)
        card = client.get(f'/purchases/{lot["id"]}').get_data(as_text=True)
        assert 'aria-current="true">Портфель</a>' in card
        assert f'<a href="/issues/{lot["isin"]}">{lot["isin"]}</a>' in card
        path = f'/purchases/{lot["id"]}/payments/{past["id"]}/confirm'
        assert path in page
        assert client.post(path, data={"actual_date": "2026-07-01", "amount": "795"}).status_code == 302
        page = client.get("/payments").get_data(as_text=True)
        first = next(row for row in rows(page) if row.startswith(f'{lot["id"]}" data-payment-id="{past["id"]}"'))
        second = next(row for row in rows(page) if row.startswith(f'{neighbor["id"]}" data-payment-id="{past["id"]}"'))
        assert "795,00" in first and "Подтверждено" in first and "Исправить выплату" in first
        assert "400,00" in second and "Требует подтверждения" in second and "Подтвердить выплату" in second
        assert "Банк А" in first and "Банк Б" in second and '<th scope="col">Банк</th>' in page
        cells = re.findall(r"<td(?:\s[^>]*)?>(.*?)</td>", first, re.DOTALL)
        assert len(cells) == 9
        assert "2026-06-30" in cells[4] and "800,00" in cells[5]
        assert "2026-07-01" in cells[6] and "795,00" in cells[7]
        assert "Подтверждено" in cells[8] and "Исправить выплату" in cells[8]
        second_cells = re.findall(r"<td(?:\s[^>]*)?>(.*?)</td>", second, re.DOTALL)
        assert "Требует подтверждения" in second_cells[8] and "Подтвердить выплату" in second_cells[8]
        assert len(database.list_actual_payments()) == 1
        actual, = database.list_actual_payments()
        assert f'/purchases/{lot["id"]}/payments/{actual["id"]}/edit' in first
        assert f'/purchases/{neighbor["id"]}/payments/{past["id"]}/confirm' in second


@pytest.mark.parametrize("query", ["start=2026-08-01&end=2026-07-01", "start=2026-02-30", "end=bad"])
def test_invalid_range_does_not_evaluate_payments(app, local_today, monkeypatch, query):
    # An invalid range must not call the domain listing even if saved data exists.
    def unexpected(*args, **kwargs):
        raise AssertionError("Invalid range must not be evaluated.")

    monkeypatch.setattr("ovdp_tracker.web.list_payment_events", unexpected)
    with app.test_client() as client:
        response = client.get(f"/payments?{query}")
    html = response.get_data(as_text=True)
    assert response.status_code == 400 and 'role="alert"' in html
    assert "Ближайшие выплаты</h2>" not in html and 'aria-invalid="true"' in html


def test_empty_result_and_filter_values(app, payment_data, local_today):
    # An empty currency or date range keeps the selected values and shows an empty state in each section.
    with app.test_client() as client:
        response = client.get("/payments?currency=EUR&start=2026-07-01&end=2026-07-01")
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'value="EUR" selected' in page and page.count('value="2026-07-01"') == 2
    assert page.count("Выплат за выбранный период нет.") == 3
    assert not rows(page)
