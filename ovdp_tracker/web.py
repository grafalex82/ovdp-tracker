"""Adapt the independent database API to Flask request handlers."""

from datetime import date
from functools import wraps
import json

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for
from itsdangerous import BadSignature

from .analytics import (get_portfolio_returns, get_position_returns, get_purchase_cash_flows, simple_annual_return,
                        summarize_receipts, xirr)
from .db import Database, DatabaseError
from .issues import edit_issue as save_issue_correction
from .money import operation_amount, parse_money
from .messages import user_message
from .payments import PaymentValidationError, confirm_payment, delete_payment as remove_payment
from .payments import edit_payment as save_payment_correction
from .portfolio import get_lot_details, get_position_details, list_payment_events, list_positions
from .nbu import NbuError, NbuNotFound
from .quantity import MAX_BOND_QUANTITY
from .sales import SaleValidationError, check_history, create_sale, validate_sale
from .purchases import (
    CURRENCIES, IssueValidationError, PurchaseValidationError, create_purchase, issue_form_values, issue_signature,
    parse_date, validate_imported_issue, validate_issue, validate_purchase,
)

purchase_pages = Blueprint("purchases", __name__)
PREVIEW_FIELDS = (
    ("nominal", "Номинал одной облигации"), ("currency", "Валюта"),
    ("issue_date", "Дата выпуска / первичного размещения"), ("maturity_date", "Дата погашения"),
    ("nominal_rate", "Номинальная ставка, %"), ("coupon_period", "Купонный период, дней"),
    ("bond_type", "Вид облигации"),
)


@purchase_pages.app_context_processor
def bond_quantity_limit():
    """Expose the supported per-operation bond quantity limit to templates."""
    return {"max_bond_quantity": MAX_BOND_QUANTITY}


def with_database(view):
    """Wrap a Flask view with a request-specific Database and return the decorated handler."""

    # Build a wrapper that preserves the handler's metadata and manages its database.
    @wraps(view)
    def wrapped(*args, **kwargs):
        with Database(current_app.config.get("DATABASE")) as database:
            return view(database, *args, **kwargs)

    # Return the decorated handler for Flask route registration.
    return wrapped


@purchase_pages.app_template_filter("money")
def format_money(value: str) -> str:
    """Format an exact monetary string with two decimal places and a comma separator."""
    return str(operation_amount(parse_money(str(value)))).replace(".", ",")


@purchase_pages.app_template_filter("annual_rate")
def format_annual_rate(value) -> str:
    """Format a fractional annual rate as a percentage with two decimal places."""
    return f"{value * 100:.2f}".replace(".", ",") + "%"


def describe_analytics(database: Database, lots: list[dict], today: date) -> dict:
    """Describe full-horizon flows and completed receipts for the provided purchases."""

    # Use the same date for all purchase histories and their presentation status.
    purchases = [get_purchase_cash_flows(database, lot["id"], today=today) for lot in lots]
    summary = summarize_receipts(purchases, today=today)
    active = [lot for lot in lots if not lot["closed"]]
    summary["status"] = "ожидаемые" if any(
        flow["status"] == "expected" or flow["date"] > today.isoformat()
        for purchase in purchases for flow in purchase["flows"] if flow["kind"] != "purchase"
    ) else "фактические"
    summary["preliminary"] = any(lot["has_overdue_payments"] for lot in lots)
    active_ids = {lot["id"] for lot in active}
    active_flows = [flow for purchase in purchases if purchase["lot_id"] in active_ids for flow in purchase["flows"]]
    summary["active_status"] = "ожидаемые" if any(
        flow["status"] == "expected" or flow["date"] > today.isoformat()
        for flow in active_flows if flow["kind"] != "purchase"
    ) else "фактические"
    if not active:
        summary["active_status"] = "нет действующих покупок"
    summary["active_preliminary"] = any(lot["has_overdue_payments"] for lot in active)
    return summary


@purchase_pages.route("/purchases/new", methods=["GET", "POST"])
@with_database
def new_purchase(database: Database):
    """Render purchase inputs or validate and save a submitted lot with its schedule."""

    # Apply local defaults only when first opening the form.
    today = date.today()
    values = {
        "purchase_date": today.isoformat(), "nominal": "1000", "currency": "UAH",
        "issue_mode": "pending", "entry_mode": "nbu", "coupons": [],
    }
    errors = {}
    status = 200

    # Preserve every submitted value on validation or storage failure.
    if request.method == "POST":
        values = request.form.to_dict()
        try:
            if "coupons" in values:
                try:
                    values["coupons"] = json.loads(values["coupons"])
                except (ValueError, RecursionError) as error:
                    raise PurchaseValidationError({"coupons": "Enter a valid JSON coupon array."}) from error
            values = _prepare_purchase_values(database, values)
            purchase = create_purchase(database, values, today)
        except PurchaseValidationError as error:
            errors = {field: user_message(message) for field, message in error.errors.items()}
            status = 400
        except DatabaseError:
            current_app.logger.exception("Could not save the purchase.")
            errors["form"] = "Не удалось сохранить покупку. Изменения не сохранены; повторите попытку."
            status = 503
        else:
            return redirect(url_for("index", saved="1", currency=purchase["currency"]))

    # Restore disabled saved fields for display after a rejected repeat purchase.
    values.setdefault("entry_mode", "manual" if values.get("issue_mode") == "manual" else "nbu")
    source_values = values if values.get("issue_mode") == "auto" and isinstance(values.get("coupons"), list) else {}
    issue = database.get_issue_by_isin(values.get("isin", ""))
    if issue:
        defaults = issue_form_values(issue, database.list_scheduled_payments(issue["id"]))
        values = {**defaults, **values, "issue_mode": "existing"}
        source_values = defaults
    else:
        values.setdefault("nominal", "1000")
        values.setdefault("currency", "UAH")

    # Render the same form with field errors and the original input strings.
    return render_template(
        "purchase_form.html", values=values, errors=errors, currencies=CURRENCIES, banks=database.list_banks(),
        today=today.isoformat(), source_values=source_values, preview_fields=PREVIEW_FIELDS,
    ), status


def _prepare_purchase_values(database: Database, values: dict) -> dict:
    # Check saved issues before source-specific normalization can discard conflicting submitted fields.
    issue = database.get_issue_by_isin(values.get("isin", ""))
    if issue and (values.get("issue_mode") != "auto" or not values.get("issue_token")):
        return {**values, "issue_mode": "existing"}

    # Restore read-only source values from a signed snapshot without another NBU request.
    if values.get("issue_mode") == "auto":
        try:
            source = current_app.extensions["issue_signer"].loads(values.get("issue_token", ""))
        except BadSignature as error:
            values["issue_mode"] = "pending"
            raise PurchaseValidationError({"isin": "Reload NBU data before saving this purchase."}) from error
        if source["isin"] != values.get("isin", "").strip().upper():
            values["issue_mode"] = "pending"
            raise PurchaseValidationError({"isin": "Loaded data belongs to a different ISIN. Reload the issue."})
        if issue:
            parsed = validate_issue(source)
            events = database.list_scheduled_payments(issue["id"])
            if issue_signature(parsed, parsed["payments"]) != issue_signature(issue, events):
                raise PurchaseValidationError({
                    "isin": "Loaded NBU data conflicts with the saved ISIN. Reload the issue.",
                })
            return {**source, **values, "issue_mode": "existing"}
        return {**values, **source}

    # Limit the explicit manual form to nominal, currency and coupon dates with their own amounts.
    if values.get("entry_mode") == "manual" and values.get("issue_mode") != "existing":
        coupons = values.get("coupons", [])
        return {
            **values, "issue_mode": "manual", "infer_maturity_from_coupons": "1" if coupons else "0",
            "maturity_date": "" if coupons else values.get("maturity_date", ""),
            "redemption_amount": values.get("nominal", ""), "issue_date": "", "nominal_rate": "",
            "coupon_period": "", "bond_type": "",
        }

    # Keep existing issue resolution and domain-level legacy inputs unchanged.
    return values


@purchase_pages.get("/issues/lookup")
@with_database
def lookup_issue(database: Database):
    """Return NBU draft data or protected saved issue data for the requested ISIN."""

    # Resolve persisted data before requesting the public directory.
    isin = request.args.get("isin", "").strip().upper()
    issue = database.get_issue_by_isin(isin)
    events = database.list_scheduled_payments(issue["id"]) if issue else []
    warning = ""
    local_only = request.args.get("source") == "local"
    if local_only and issue is None:
        return jsonify(existing=False)

    # Keep known issues usable even when the remote directory is unavailable.
    remote = None
    try:
        if not local_only:
            remote = validate_imported_issue(current_app.extensions["nbu"].lookup(isin))
    except (NbuError, IssueValidationError) as error:
        if isinstance(error, IssueValidationError):
            details = " ".join(dict.fromkeys(user_message(message) for message in error.errors.values()))
            message = f"Данные облигации НБУ не поддерживаются. {details}"
        elif isinstance(error, NbuNotFound):
            message = "ISIN не найден в справочнике облигаций НБУ."
        elif str(error).startswith("NBU returned an incomplete or malformed bond record:"):
            message = "НБУ вернул неполные или некорректные данные облигации."
        else:
            message = user_message(str(error))
        if issue is None:
            return jsonify(error=message), 404 if isinstance(error, NbuNotFound) else 502
        remote = None
        warning = f"{message} Используются сохранённые данные выпуска."

    # Return saved values without allowing NBU to rewrite an existing issue or any actual receipts.
    if issue:
        values = issue_form_values(issue, events)
        if remote and issue_signature(remote["issue"], remote["payments"]) != issue_signature(issue, events):
            warning = "Данные НБУ отличаются от сохранённых. Общие реквизиты исправляются отдельно."
        details_url = url_for("purchases.issue_details", isin=issue["isin"])
        if not any(lot["issue_id"] == issue["id"] for lot in database.list_lots()):
            details_url = None
        return jsonify(values=values, existing=True, warning=warning, details_url=details_url)

    # Populate a new unsaved draft only after the complete NBU record passes validation.
    values = issue_form_values(remote["issue"], remote["payments"])
    return jsonify(
        values=values, existing=False, warning="", issue_token=current_app.extensions["issue_signer"].dumps(values)
    )


@purchase_pages.get("/history")
@with_database
def purchase_history(database: Database):
    """Render fully closed ISIN positions in the selected currency."""
    return render_portfolio(database, closed=True)


@purchase_pages.get("/payments")
@with_database
def payment_list(database: Database):
    """Render purchase-specific payments for the selected currency and inclusive date range."""

    # Validate both date boundaries before building any filtered payment list.
    currency = request.args.get("currency", "UAH")
    if currency not in CURRENCIES:
        abort(400, description="Unsupported portfolio currency.")
    start_value = request.args.get("start", "")
    end_value = request.args.get("end", "")
    error = None
    try:
        start = parse_date(start_value) if start_value else None
        end = parse_date(end_value) if end_value else None
        if start and end and start > end:
            error = "Начало периода не может быть позже его окончания."
    except (TypeError, ValueError):
        error = "Укажите корректные даты периода."

    # Keep invalid input visible without evaluating the requested range.
    groups = None
    if not error:
        groups = list_payment_events(database, currency=currency, start=start, end=end, today=date.today())
    return render_template(
        "payments.html", currency=currency, currencies=CURRENCIES, start=start_value, end=end_value,
        error=error, groups=groups,
    ), 400 if error else 200


def render_portfolio(database: Database, *, closed: bool = False):
    """Render active or closed ISIN positions after validating the selected currency."""

    # Reject unsupported filters instead of silently showing a different currency.
    currency = request.args.get("currency", "UAH")
    if currency not in CURRENCIES:
        abort(400, description="Unsupported portfolio currency.")

    # Share currency selection and position rules between the portfolio and closed history.
    today = date.today()
    positions = list_positions(database, currency=currency, closed=closed, today=today)
    all_lots = [get_lot_details(database, lot["id"], today=today) for lot in database.list_lots()
                if lot["currency"] == currency]
    for position in positions:
        position["returns"] = get_position_returns(database, position["isin"], today=today)
    return render_template(
        "index.html", positions=positions, history=closed,
        currency=currency, currencies=CURRENCIES, saved=request.args.get("saved") == "1" and not closed,
        deleted=request.args.get("deleted") == "1", issue_saved=request.args.get("issue_saved") == "1",
        returns=get_portfolio_returns(database, currency=currency, today=today),
        summary=describe_analytics(database, all_lots, today),
    )


@purchase_pages.get("/issues/<isin>")
@with_database
def issue_details(database: Database, isin: str):
    """Render the ISIN position with all purchases, or return 404 when no position exists."""

    # Resolve the position from its shared ISIN rather than an arbitrary purchase.
    today = date.today()
    position = get_position_details(database, isin, today=today)
    if position is None:
        abort(404, description="Position not found.")

    # Show the complete calendar and both active and closed purchases in their canonical parent page.
    return render_template("issue_details.html", position=position,
                           returns=get_position_returns(database, isin, today=today),
                           summary=describe_analytics(database, position["lots"], today))


@purchase_pages.route("/issues/<isin>/edit", methods=["GET", "POST"])
@with_database
def edit_issue(database: Database, isin: str):
    """Render or save an explicit shared correction for isin, preserving rejected form values."""

    # Resolve a portfolio position and seed the editor from its complete saved calendar.
    position = get_position_details(database, isin)
    if position is None:
        abort(404, description="Position not found.")
    values = issue_form_values(position, position["schedule"])
    errors, status = {}, 200

    # Retain all raw inputs and the original dirty-form baseline when any validation or write fails.
    if request.method == "POST":
        values = request.form.to_dict()
        values.setdefault("isin", position["isin"])
        try:
            try:
                values["coupons"] = json.loads(values.get("coupons", "[]"))
            except (ValueError, RecursionError) as error:
                raise IssueValidationError({"coupons": "Enter a valid JSON coupon array."}) from error
            issue = save_issue_correction(database, position["id"], values)
        except IssueValidationError as error:
            errors = {field: user_message(message) for field, message in error.errors.items()}
            status = 400
        except DatabaseError:
            current_app.logger.exception("Could not update the shared issue.")
            errors["form"], status = user_message("Could not update the issue. Retry the operation."), 503
        else:
            return redirect(url_for("index", issue_saved="1", currency=issue["currency"]))

    # Share the calendar editor, navigation and unsaved-change protection with the other forms.
    return render_template("edit_issue.html", position=position, values=values, errors=errors,
                           currencies=CURRENCIES), status


@purchase_pages.get("/purchases/<int:lot_id>")
@with_database
def purchase_details(database: Database, lot_id: int):
    """Render the selected purchase with its shared calendar, or return 404 for an unknown lot_id."""

    # Resolve an existing purchase before rendering its details.
    today = date.today()
    lot = get_lot_details(database, lot_id, today=today)
    if lot is None:
        abort(404, description="Purchase not found.")

    # Present the same stored fields and current state as the portfolio list.
    purchase = get_purchase_cash_flows(database, lot_id, today=today)
    simple = simple_annual_return(purchase)
    annual = {"rate": None, "reason": "missing_redemption"} if simple["reason"] == "missing_redemption" else xirr(
        purchase["flows"]
    )
    return render_template("purchase_details.html", lot=lot, purchase=purchase,
                           returns={"xirr": annual, "simple_return": simple},
                           summary=describe_analytics(database, [lot], today))


@purchase_pages.route("/purchases/<int:lot_id>/payments/<int:event_id>/confirm", methods=["GET", "POST"])
@with_database
def new_payment(database: Database, lot_id: int, event_id: int):
    """Render or confirm the entitled event_id of lot_id while retaining rejected receipt inputs."""

    # Resolve the selected entitlement without exposing another issue's event or inventing a receipt.
    today = date.today()
    lot = get_lot_details(database, lot_id, today=today)
    if lot is None:
        abort(404, description="Purchase not found.")
    payment = next((item for item in lot["payments"] if item["id"] == event_id), None)
    if payment is None:
        abort(404, description="Payment is not due for this purchase.")
    if request.method == "GET" and payment["actual"] is not None:
        return redirect(url_for("purchases.purchase_details", lot_id=lot_id))
    if payment["actual"] is None and parse_money(payment["planned_amount"]) <= 0:
        abort(400, description="Payment does not require confirmation.")
    values = {"actual_date": payment["planned_date"], "amount": payment["planned_amount"]}
    errors, status = {}, 200

    # Recheck rights and history atomically on POST instead of trusting the displayed plan or hidden inputs.
    if request.method == "POST":
        values = request.form.to_dict()
        try:
            confirm_payment(database, lot_id, event_id, values, today)
        except PaymentValidationError as error:
            errors = {field: user_message(message) for field, message in error.errors.items()}
            status = 400
        except DatabaseError:
            current_app.logger.exception("Could not confirm the payment.")
            errors["form"], status = user_message("Could not confirm the payment. Retry the operation."), 503
        else:
            return redirect(url_for("purchases.purchase_details", lot_id=lot_id, payment_saved="1"))

    # Reuse the shared form protection, including the original baseline after validation or storage errors.
    return render_template("payment_form.html", lot=lot, payment=payment, values=values, errors=errors,
                           today=today.isoformat()), status


@purchase_pages.route("/purchases/<int:lot_id>/payments/<int:actual_id>/edit", methods=["GET", "POST"])
@with_database
def edit_payment(database: Database, lot_id: int, actual_id: int):
    """Render or correct actual_id within lot_id while preserving rejected receipt inputs."""

    # Locate this purchase's existing receipt without exposing confirmations of neighboring purchases.
    today = date.today()
    lot = get_lot_details(database, lot_id, today=today)
    if lot is None:
        abort(404, description="Purchase not found.")
    payment = next((item for item in lot["payments"] if item["actual"] and item["actual"]["id"] == actual_id), None)
    if payment is None:
        abort(404, description="Confirmed payment not found.")
    values, errors, status = payment["actual"], {}, 200

    # Recheck receipt ownership and the proposed history in the same transaction as the correction.
    if request.method == "POST":
        values = request.form.to_dict()
        try:
            save_payment_correction(database, lot_id, actual_id, values, today)
        except PaymentValidationError as error:
            errors = {field: user_message(message) for field, message in error.errors.items()}
            status = 400
        except DatabaseError:
            current_app.logger.exception("Could not update the payment.")
            errors["form"], status = user_message("Could not update the payment. Retry the operation."), 503
        else:
            return redirect(url_for("purchases.purchase_details", lot_id=lot_id, payment_updated="1"))

    # Reuse the receipt form, parent navigation and original dirty baseline after any rejected correction.
    return render_template("payment_form.html", lot=lot, payment=payment, values=values, errors=errors,
                           today=today.isoformat(), editing=True), status


@purchase_pages.post("/purchases/<int:lot_id>/payments/<int:actual_id>/delete")
@with_database
def delete_payment(database: Database, lot_id: int, actual_id: int):
    """Remove the explicitly confirmed receipt and preserve the submitted draft on failure."""

    # Resolve the saved receipt for the dialog and reject missing or foreign identifiers.
    today = date.today()
    lot = get_lot_details(database, lot_id, today=today)
    if lot is None:
        abort(404, description="Purchase not found.")
    payment = next((item for item in lot["payments"] if item["actual"] and item["actual"]["id"] == actual_id), None)
    if payment is None:
        abort(404, description="Confirmed payment not found.")

    # Delete only after explicit confirmation, rechecking ownership and history inside the domain operation.
    try:
        if request.form.get("confirm") != "delete":
            raise PaymentValidationError({"form": "Confirm deletion of the saved payment."})
        remove_payment(database, lot_id, actual_id)
    except PaymentValidationError as error:
        errors = {field: user_message(message) for field, message in error.errors.items()}
        status = 400
    except DatabaseError:
        current_app.logger.exception("Could not delete the payment.")
        errors = {"form": user_message("Could not delete the payment. Retry the operation.")}
        status = 503
    else:
        return redirect(url_for("purchases.purchase_details", lot_id=lot_id, payment_deleted="1"))

    # Retain the submitted editor values and original baseline independently of saved dialog details.
    values = {**payment["actual"], **request.form.to_dict()}
    return render_template("payment_form.html", lot=lot, payment=payment, values=values, errors=errors,
                           today=today.isoformat(), editing=True), status


@purchase_pages.route("/purchases/<int:lot_id>/delete", methods=["GET", "POST"])
@with_database
def delete_purchase(database: Database, lot_id: int):
    """Show the irreversible deletion summary for lot_id and delete its history only on explicit confirmation."""

    # Resolve the selected purchase and summarize the history that would be removed.
    lot = get_lot_details(database, lot_id)
    if lot is None:
        abort(404, description="Purchase not found.")
    error, status = "", 200

    # Require the confirmation action before atomically removing only this purchase's history.
    if request.method == "POST":
        if request.form.get("confirm") != "delete":
            error, status = user_message("Confirm deletion of the complete purchase history."), 400
        else:
            try:
                removed = database.delete_lot(lot_id)
            except DatabaseError:
                current_app.logger.exception("Could not delete the purchase.")
                error, status = user_message("Could not delete the purchase. Retry the operation."), 503
            else:
                if not removed:
                    abort(404, description="Purchase not found.")
                return redirect(url_for("index", deleted="1", currency=lot["currency"]))

    # Keep cancellation and retry available without changing stored data on a rejected request.
    return render_template(
        "delete_purchase.html", lot=lot, error=error, actual_count=len(database.list_actual_payments(lot_id)),
    ), status


@purchase_pages.route("/purchases/<int:lot_id>/sales/new", methods=["GET", "POST"])
@with_database
def new_sale(database: Database, lot_id: int):
    """Render or save a sale for the selected lot_id, preserving invalid inputs."""

    # Resolve the purchase before showing or processing its sale form.
    lot = get_lot_details(database, lot_id)
    if lot is None:
        abort(404, description="Purchase not found.")
    if lot["remaining_quantity"] <= 0:
        return redirect(url_for("purchases.purchase_details", lot_id=lot_id))
    today = date.today()
    values = {"sale_date": today.isoformat(), "quantity": "", "sale_price": ""}
    error = ""
    status = 200

    # Validate the operation and redirect after the transaction commits.
    if request.method == "POST":
        values = request.form.to_dict()
        try:
            create_sale(database, lot_id, values, today)
        except SaleValidationError as exception:
            error, status = user_message(str(exception)), 400
        except DatabaseError:
            current_app.logger.exception("Could not save the sale.")
            error, status = "Не удалось сохранить продажу. Изменения не сохранены; повторите попытку.", 503
        else:
            return redirect(url_for("purchases.purchase_details", lot_id=lot_id))

    # Retain entered values and the original form baseline on errors.
    return render_template("sale_form.html", lot=lot, values=values, error=error, today=today.isoformat()), status


@purchase_pages.route("/purchases/<int:lot_id>/edit", methods=["GET", "POST"])
@with_database
def edit_purchase(database: Database, lot_id: int):
    """Edit the independent fields of lot_id after atomically checking its history."""

    # Resolve the purchase and initialize its editable values.
    lot = get_lot_details(database, lot_id)
    if lot is None:
        abort(404, description="Purchase not found.")
    today = date.today()
    values = {**lot, "bank_name": lot["bank_name"]}
    error, status = "", 200

    # Validate using saved issue fields and reject attempts to change them through this form.
    if request.method == "POST":
        values = request.form.to_dict()
        try:
            with database.transaction():
                current = database.get_lot(lot_id)
                issue_values = issue_form_values(current, database.list_scheduled_payments(current["issue_id"]))
                if any(key in values for key in (*issue_values, "issue_id", "isin")):
                    raise SaleValidationError("Shared issue fields cannot be edited here.")
                parsed = validate_purchase({**issue_values, **values, "issue_mode": "existing"}, today)
                check_history(database, {**current, **parsed}, database.list_sales(lot_id))
                bank = database.add_bank(parsed["bank_name"])
                database.update_lot(lot_id, purchase_date=parsed["purchase_date"],
                                    purchase_price=parsed["purchase_price"], quantity=parsed["quantity"],
                                    bank_id=bank["id"])
        except PurchaseValidationError as exception:
            error, status = " ".join(user_message(message) for message in exception.errors.values()), 400
        except SaleValidationError as exception:
            error, status = user_message(str(exception)), 400
        except DatabaseError:
            current_app.logger.exception("Could not update the purchase.")
            error, status = "Не удалось сохранить покупку. Изменения не сохранены; повторите попытку.", 503
        else:
            return redirect(url_for("purchases.purchase_details", lot_id=lot_id))

    # Render the original values or the rejected submission with shared unsaved-change protection.
    return render_template("edit_purchase.html", lot=lot, values=values, error=error,
                           today=today.isoformat(), banks=database.list_banks()), status


@purchase_pages.route("/purchases/<int:lot_id>/sales/<int:sale_id>/edit", methods=["GET", "POST"])
@with_database
def edit_sale(database: Database, lot_id: int, sale_id: int):
    """Edit sale_id within lot_id after atomically validating the resulting purchase history."""

    # Require the sale to belong to the selected purchase, including closed purchases.
    lot = get_lot_details(database, lot_id)
    if lot is None:
        abort(404, description="Purchase not found.")
    sale = next((item for item in lot["sales"] if item["id"] == sale_id), None)
    if sale is None:
        abort(404, description="Sale not found.")
    today = date.today()
    values, error, status = sale, "", 200

    # Replace only this sale in the history and save after all checks pass.
    if request.method == "POST":
        values = request.form.to_dict()
        try:
            parsed = validate_sale(values, today)
            with database.transaction():
                current = database.get_lot(lot_id)
                sales = database.list_sales(lot_id)
                if current is None or not any(item["id"] == sale_id for item in sales):
                    abort(404, description="Sale not found.")
                check_history(database, current, [{**item, **parsed} if item["id"] == sale_id else item
                                                  for item in sales])
                database.update_sale(sale_id, **parsed)
        except SaleValidationError as exception:
            error, status = user_message(str(exception)), 400
        except DatabaseError:
            current_app.logger.exception("Could not update the sale.")
            error, status = "Не удалось сохранить продажу. Изменения не сохранены; повторите попытку.", 503
        else:
            return redirect(url_for("purchases.purchase_details", lot_id=lot_id))

    # Reuse the sale form and retain the initial baseline after a rejected submission.
    return render_template("sale_form.html", lot=lot, values=values, error=error, today=today.isoformat(),
                           editing=True, sale=sale), status


@purchase_pages.post("/purchases/<int:lot_id>/sales/<int:sale_id>/delete")
@with_database
def delete_sale(database: Database, lot_id: int, sale_id: int):
    """Delete the confirmed sale_id from lot_id after atomically validating ownership and the remaining history."""

    # Resolve saved details for confirmation errors without applying any submitted draft fields.
    lot = get_lot_details(database, lot_id)
    if lot is None:
        abort(404, description="Purchase not found.")
    sale = next((item for item in lot["sales"] if item["id"] == sale_id), None)
    if sale is None:
        abort(404, description="Sale not found.")
    error, status = "", 200

    # Recheck ownership and history under the write lock before deleting the one selected sale.
    try:
        if request.form.get("confirm") != "delete":
            raise SaleValidationError("Confirm deletion of the saved sale.")
        with database.transaction():
            current = database.get_lot(lot_id)
            sales = database.list_sales(lot_id)
            if current is None or not any(item["id"] == sale_id for item in sales):
                abort(404, description="Sale not found.")
            check_history(database, current, [item for item in sales if item["id"] != sale_id])
            if not database.delete_sale(sale_id, lot_id=lot_id):
                abort(404, description="Sale not found.")
    except SaleValidationError as exception:
        error, status = user_message(str(exception)), 400
    except DatabaseError:
        current_app.logger.exception("Could not delete the sale.")
        error, status = user_message("Could not delete the sale. Retry the operation."), 503
    else:
        return redirect(url_for("purchases.purchase_details", lot_id=lot_id, sale_deleted="1"))

    # Preserve the editor's draft and its original unsaved-change baseline on any rejected deletion.
    return render_template(
        "sale_form.html", lot=lot, sale=sale, values={**sale, **request.form.to_dict()}, error=error,
        today=date.today().isoformat(), editing=True,
    ), status
