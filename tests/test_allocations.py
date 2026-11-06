"""Verify exact currency-specific portfolio allocations and their presentation."""

from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from ovdp_tracker.analytics import calculate_allocations
from ovdp_tracker.portfolio import list_positions
from ovdp_tracker.web import format_portfolio_share


@pytest.fixture
def fixed_today(monkeypatch):
    # Apply the same date to the HTTP handler and explicit portfolio calculations.
    class LocalDate(date):
        @classmethod
        def today(cls):
            """Return the local date used by the allocation scenarios."""
            return cls(2026, 7, 1)

    monkeypatch.setattr("ovdp_tracker.web.date", LocalDate)
    return LocalDate.today()


def add_position(database, bank, *, isin, currency="UAH", price="950", quantity=10):
    # Save an issue, its purchase and a complete redemption calendar for return calculations.
    issue = database.add_issue(isin=isin, nominal="1000", currency=currency, maturity_date="2027-01-01")
    lot = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2025-01-01",
                           purchase_price=price, quantity=quantity)
    database.add_scheduled_payment(issue_id=issue["id"], kind="redemption", planned_date="2027-01-01", amount="1000")
    return issue, lot


def position_row(html, isin):
    # Isolate the rendered cells for a single ISIN.
    return html.split(f'data-isin="{isin}"', 1)[1].split("</tr>", 1)[0]


def test_allocation_uses_remaining_cost_and_excludes_other_currency_and_closed_purchases(app, database, bank,
                                                                                          fixed_today):
    # Leave seven bonds costing 6650, then add a fully sold purchase to the same ISIN.
    first, lot = add_position(database, bank, isin="UA4000000001")
    database.add_sale(lot_id=lot["id"], sale_date="2025-06-01", sale_price="980", quantity=3)
    closed_lot = database.add_lot(issue_id=first["id"], bank_id=bank["id"], purchase_date="2025-02-01",
                                  purchase_price="1100", quantity=5)
    database.add_sale(lot_id=closed_lot["id"], sale_date="2025-06-01", sale_price="1150", quantity=5)
    second, _ = add_position(database, bank, isin="UA4000000002", price="3350", quantity=1)
    third, closed = add_position(database, bank, isin="UA4000000003", price="5000", quantity=1)
    database.add_sale(lot_id=closed["id"], sale_date="2025-06-01", sale_price="5000", quantity=1)
    dollar, _ = add_position(database, bank, isin="UA4000000004", currency="USD", price="90000", quantity=1)

    # Exact ratios use only active UAH balances, even when other positions are passed to the calculator.
    positions = (list_positions(database, currency="UAH", today=fixed_today)
                 + list_positions(database, currency="UAH", closed=True, today=fixed_today)
                 + list_positions(database, currency="USD", today=fixed_today))
    allocations = calculate_allocations(positions, currency="UAH")
    assert allocations == {
        first["isin"]: {"cost": Decimal("6650.00"), "share": Fraction(133, 200)},
        second["isin"]: {"cost": Decimal("3350.00"), "share": Fraction(67, 200)},
    }
    assert third["isin"] not in allocations and dollar["isin"] not in allocations

    # Display each percentage in its own row and keep closed history free of portfolio shares.
    with app.test_client() as client:
        home = client.get("/?currency=UAH").get_data(as_text=True)
        usd = client.get("/?currency=USD").get_data(as_text=True)
        history = client.get("/history?currency=UAH").get_data(as_text=True)
    assert "66,50%" in position_row(home, first["isin"])
    assert "33,50%" in position_row(home, second["isin"])
    assert "100,00%" in position_row(usd, dollar["isin"])
    assert 'data-isin="' + third["isin"] + '"' in history and "Доля в портфеле" not in history
    assert "себестоимости всех действующих" in home and "не рыночная стоимость" in home


def test_equal_shares_preserve_exact_amounts_without_balancing_rounded_percentages(app, database, bank, fixed_today):
    # Use three equal exact cent amounts in separate active positions.
    issues = [add_position(database, bank, isin=f"UA400000000{index}", price="1000.01", quantity=1)[0]
              for index in (1, 2, 3)]
    positions = list_positions(database, currency="UAH", today=fixed_today)
    with localcontext() as context:
        context.prec = 3
        allocations = calculate_allocations(positions, currency="UAH")
    assert all(allocation == {"cost": Decimal("1000.01"), "share": Fraction(1, 3)}
               for allocation in allocations.values())

    # Each independently rounded share is 33.33%, without changing the stored values or the exact ratios.
    with app.test_client() as client:
        page = client.get("/").get_data(as_text=True)
    assert all("33,33%" in position_row(page, issue["isin"]) for issue in issues)
    assert [lot["purchase_price"] for lot in database.list_lots()] == ["1000.01"] * 3
    assert sum(allocation["share"] for allocation in allocations.values()) == 1


def test_multiple_active_purchases_use_each_exact_unit_price(database, bank, fixed_today):
    # Combine two active purchases after a partial sale and compare them with another active ISIN.
    issue, _ = add_position(database, bank, isin="UA4000000001", price="1000.01", quantity=2)
    second = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2025-02-01",
                              purchase_price="1500.03", quantity=3)
    database.add_sale(lot_id=second["id"], sale_date="2025-06-01", sale_price="1500", quantity=1)
    add_position(database, bank, isin="UA4000000002", price="4999.92", quantity=1)
    positions = list_positions(database, currency="UAH", today=fixed_today)
    allocation = calculate_allocations(positions, currency="UAH")[issue["isin"]]
    assert allocation == {"cost": Decimal("5000.08"), "share": Fraction(62501, 125000)}
    assert format_portfolio_share(allocation["share"]) == "50,00%"


def test_redeemed_purchase_does_not_increase_position_allocation(database, bank, fixed_today):
    # Redeem one purchase while another purchase of the same issue remains active.
    issue, redeemed = add_position(database, bank, isin="UA4000000001", price="5000", quantity=1)
    database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2025-02-01",
                     purchase_price="1000", quantity=1)
    event, = database.list_scheduled_payments(issue["id"])
    database.add_actual_payment(lot_id=redeemed["id"], scheduled_payment_id=event["id"],
                                actual_date="2026-06-01", amount="1000")
    add_position(database, bank, isin="UA4000000002", price="1000", quantity=1)
    positions = list_positions(database, currency="UAH", today=fixed_today)
    allocation = calculate_allocations(positions, currency="UAH")[issue["isin"]]
    assert allocation == {"cost": Decimal("1000.00"), "share": Fraction(1, 2)}


def test_zero_denominator_has_reason_and_no_fabricated_share(app, database, bank, fixed_today):
    # Simulate a saved zero-cost position through the storage API to exercise the undefined denominator.
    issue, _ = add_position(database, bank, isin="UA4000000001", price="0", quantity=1)
    allocations = calculate_allocations(list_positions(database, today=fixed_today), currency="UAH")
    assert allocations[issue["isin"]] == {"cost": Decimal("0.00"), "share": None}
    with app.test_client() as client:
        row = position_row(client.get("/").get_data(as_text=True), issue["isin"])
    assert "Не определена: нулевая себестоимость" in row and "0,00%" not in row


def test_share_rounding_uses_exact_rational_values():
    # Round ties to even without a decimal precision or floating-point boundary error.
    assert format_portfolio_share(Fraction(1, 8)) == "12,50%"
    assert format_portfolio_share(Fraction(1, 20000)) == "0,00%"
    assert format_portfolio_share(Fraction(3, 20000)) == "0,02%"
    assert format_portfolio_share(Fraction(1, 3)) == "33,33%"
