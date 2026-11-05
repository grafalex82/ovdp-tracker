"""Verify purchase-level cash flows and exact realized and expected profits."""

from datetime import date
from decimal import Decimal, localcontext

from ovdp_tracker.analytics import get_purchase_cash_flows
from ovdp_tracker.payments import delete_payment


def test_disposal_costs_preserve_every_cent(database, lot):
    # Sell all three bonds in two parts at the original cent-precision purchase price.
    database.update_lot(lot["id"], purchase_date="2025-01-01", purchase_price="1000.01", quantity=3,
                        bank_id=lot["bank_id"])
    database.add_sale(lot_id=lot["id"], sale_date="2025-02-01", sale_price="1000.01", quantity=1)
    database.add_sale(lot_id=lot["id"], sale_date="2025-03-01", sale_price="1000.01", quantity=2)

    # Check exact multiplication and summation even under a restrictive Decimal context.
    with localcontext() as context:
        context.prec = 3
        result = get_purchase_cash_flows(database, lot["id"], today=date(2025, 3, 1))
    sales = [flow for flow in result["flows"] if flow["kind"] == "sale"]
    assert [sale["cost_basis"] for sale in sales] == [Decimal("1000.01"), Decimal("2000.02")]
    assert result["flows"][0]["amount"] == Decimal("-3000.03")
    assert result["remaining_quantity"] == 0 and result["remaining_cost"] == Decimal("0.00")
    assert result["realized_profit"] == result["expected_profit"] == Decimal("0.00")


def test_realized_and_expected_profits_keep_remaining_cost(database, lot):
    # Confirm a coupon, sell part of the purchase and retain later entitlements.
    database.update_lot(lot["id"], purchase_date="2025-01-01", purchase_price="950", quantity=10,
                        bank_id=lot["bank_id"])
    coupon = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date="2025-06-01",
                                            amount="50")
    database.add_scheduled_payment(issue_id=lot["issue_id"], kind="redemption", planned_date="2027-06-01",
                                   amount="1000")
    database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=coupon["id"], actual_date="2025-06-02",
                                amount="500")
    database.add_sale(lot_id=lot["id"], sale_date="2025-07-01", sale_price="980", quantity=3)

    # Charge only the sold part to realized profit and include the forecast in the full horizon.
    result = get_purchase_cash_flows(database, lot["id"], today=date(2025, 7, 1))
    assert result["realized_profit"] == Decimal("590.00")
    assert result["remaining_cost"] == Decimal("6650.00")
    assert result["expected_profit"] == Decimal("940.00")
    assert len([flow for flow in result["flows"] if flow["event_id"] == coupon["id"]]) == 1


def test_expected_profit_for_coupon_and_redemption_forecast(database, lot):
    # Preserve the full purchase cost while replacing only the confirmed coupon with its actual amount.
    database.update_lot(lot["id"], purchase_date="2025-01-01", purchase_price="950", quantity=10,
                        bank_id=lot["bank_id"])
    coupon = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date="2025-06-01",
                                            amount="40")
    database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date="2026-06-01", amount="40")
    database.add_scheduled_payment(issue_id=lot["issue_id"], kind="redemption", planned_date="2027-06-01",
                                   amount="1000")
    database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=coupon["id"], actual_date="2025-06-02",
                                amount="400")
    result = get_purchase_cash_flows(database, lot["id"], today=date(2025, 6, 2))
    assert result["realized_profit"] == Decimal("400.00")
    assert result["expected_profit"] == Decimal("1300.00")


def test_shared_event_confirmation_and_deletion_are_purchase_specific(database, lot, lot_data):
    # Confirm only the first purchase while a second purchase retains its own entitlement.
    other = database.add_lot(issue_id=lot["issue_id"], bank_id=lot["bank_id"], **{**lot_data, "quantity": 5})
    coupon = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date="2025-06-01",
                                            amount="80")
    actual = database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=coupon["id"],
                                        actual_date="2025-07-01", amount="795")

    # Count exactly one flow per purchase/event pair, without a third flow from the issue calendar.
    results = [get_purchase_cash_flows(database, item["id"], today=date(2025, 7, 2)) for item in (lot, other)]
    flows = [flow for result in results for flow in result["flows"] if flow["event_id"] == coupon["id"]]
    assert [(flow["status"], flow["amount"]) for flow in flows] == [
        ("actual", Decimal("795.00")), ("expected", Decimal("400.00"))
    ]
    assert sum(flow["amount"] for flow in flows) == Decimal("1195.00")

    # Removing the receipt restores its own entitlement and leaves the neighboring purchase unchanged.
    delete_payment(database, lot["id"], actual["id"])
    first = get_purchase_cash_flows(database, lot["id"], today=date(2025, 7, 2))
    restored, = [flow for flow in first["flows"] if flow["event_id"] == coupon["id"]]
    assert (restored["status"], restored["date"], restored["amount"]) == (
        "expected", "2025-06-01", Decimal("800.00")
    )
    assert first["realized_profit"] == Decimal("0.00")
    assert get_purchase_cash_flows(database, other["id"], today=date(2025, 7, 2)) == results[1]


def test_full_horizon_uses_each_calendar_amount_and_not_nominal_rate(database, bank):
    # Give the issue a deliberately unrelated reference rate and two different coupon amounts.
    issue = database.add_issue(isin="UA4000000002", nominal="1000", currency="UAH", maturity_date="2027-01-01",
                               nominal_rate="99")
    lot = database.add_lot(issue_id=issue["id"], bank_id=bank["id"], purchase_date="2025-01-01",
                           purchase_price="1000", quantity=10)
    for kind, day, amount in (("coupon", "2026-01-01", "80"), ("coupon", "2027-01-01", "85"),
                              ("redemption", "2027-01-01", "1000")):
        database.add_scheduled_payment(issue_id=issue["id"], kind=kind, planned_date=day, amount=amount)

    # Aggregate same-day flows only for the benchmark and independently check the simple-yield formula.
    result = get_purchase_cash_flows(database, lot["id"], today=date(2025, 1, 1))
    totals = {}
    for flow in result["flows"]:
        totals[flow["date"]] = totals.get(flow["date"], Decimal("0")) + flow["amount"]
    assert totals == {"2025-01-01": Decimal("-10000"), "2026-01-01": Decimal("800"),
                      "2027-01-01": Decimal("10850")}
    assert result["expected_profit"] == Decimal("1650.00")
    assert result["expected_profit"] / (Decimal("10000") * 2) * 100 == Decimal("8.25")


def test_redemption_cost_uses_quantity_at_actual_date_and_future_sales_are_not_realized(database, lot):
    # Sell after the planned maturity but before the actual redemption of the remaining bonds.
    redemption = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="redemption",
                                                planned_date="2027-06-01", amount="1000")
    database.add_sale(lot_id=lot["id"], sale_date="2027-06-02", sale_price="980", quantity=3)
    database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=redemption["id"],
                                actual_date="2027-06-03", amount="7000")

    # Realized profit follows the requested date; the redemption disposes only the seven remaining bonds.
    result = get_purchase_cash_flows(database, lot["id"], today=date(2027, 6, 1))
    assert result["realized_profit"] == Decimal("0.00")
    result = get_purchase_cash_flows(database, lot["id"], today=date(2027, 6, 3))
    redemption_flow, = [flow for flow in result["flows"] if flow["kind"] == "redemption"]
    assert redemption_flow["quantity"] == 7 and redemption_flow["cost_basis"] == Decimal("6650.07")
    assert result["remaining_quantity"] == 0
    assert result["realized_profit"] == result["expected_profit"] == Decimal("439.90")
    assert get_purchase_cash_flows(database, -1) is None


def test_deleted_confirmation_restores_quantity_at_event_date(database, lot):
    # Confirm a coupon after a prior sale, then sell the remaining bonds later.
    database.add_sale(lot_id=lot["id"], sale_date="2025-01-01", sale_price="1000", quantity=3)
    coupon = database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date="2025-06-01",
                                            amount="80")
    actual = database.add_actual_payment(lot_id=lot["id"], scheduled_payment_id=coupon["id"],
                                        actual_date="2025-06-02", amount="550")
    database.add_sale(lot_id=lot["id"], sale_date="2025-07-01", sale_price="1000", quantity=7)

    # Restore the seven-bond historical entitlement even though the purchase is now closed.
    delete_payment(database, lot["id"], actual["id"])
    result = get_purchase_cash_flows(database, lot["id"], today=date(2025, 7, 1))
    restored, = [flow for flow in result["flows"] if flow["event_id"] == coupon["id"]]
    assert restored["quantity"] == 7 and restored["amount"] == Decimal("560.00")
    assert restored["status"] == "expected" and result["remaining_quantity"] == 0


def test_old_and_zero_coupons_do_not_create_cash_flows(database, lot):
    # Include a coupon before purchase and a zero coupon within its ownership period.
    database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date="2024-01-01", amount="80")
    database.add_scheduled_payment(issue_id=lot["issue_id"], kind="coupon", planned_date="2025-01-01", amount="0")
    result = get_purchase_cash_flows(database, lot["id"], today=date(2025, 1, 2))
    assert [flow["kind"] for flow in result["flows"]] == ["purchase"]
