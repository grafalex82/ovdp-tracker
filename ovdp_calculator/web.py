"""Adapt the independent database API to Flask request handlers."""

from datetime import date
from functools import wraps
import json

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for
from itsdangerous import BadSignature

from .db import Database, DatabaseError
from .money import operation_amount, parse_money
from .portfolio import get_lot_details, get_position_details, list_positions
from .nbu import NbuError, NbuNotFound
from .purchases import (
    CURRENCIES, IssueValidationError, PurchaseValidationError, create_purchase, issue_form_values, issue_signature,
    validate_imported_issue, validate_issue,
)

purchase_pages = Blueprint("purchases", __name__)
PREVIEW_FIELDS = (
    ("nominal", "Номинал одной облигации"), ("currency", "Валюта"),
    ("issue_date", "Дата выпуска / первичного размещения"), ("maturity_date", "Дата погашения"),
    ("nominal_rate", "Номинальная ставка, %"), ("coupon_period", "Купонный период, дней"),
    ("bond_type", "Вид облигации"),
)


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
    return str(operation_amount(parse_money(value))).replace(".", ",")


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
            errors = error.errors
            status = 400
        except DatabaseError:
            current_app.logger.exception("Could not save the purchase.")
            errors["form"] = "Could not save the purchase. No changes were saved; please try again."
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
        "purchase_form.html", values=values, errors=errors, currencies=CURRENCIES,
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
        message = str(error)
        if isinstance(error, IssueValidationError):
            message = f"Unsupported NBU bond data: {error.errors}"
        if issue is None:
            return jsonify(error=message), 404 if isinstance(error, NbuNotFound) else 502
        remote = None
        warning = f"{message} Saved issue data is being used."

    # Return saved values without allowing NBU to rewrite an existing issue or any actual receipts.
    if issue:
        values = issue_form_values(issue, events)
        if remote and issue_signature(remote["issue"], remote["payments"]) != issue_signature(issue, events):
            warning = "NBU data differs from the saved issue. Shared corrections must be made separately."
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


def render_portfolio(database: Database, *, closed: bool = False):
    """Render active or closed ISIN positions after validating the selected currency."""

    # Reject unsupported filters instead of silently showing a different currency.
    currency = request.args.get("currency", "UAH")
    if currency not in CURRENCIES:
        abort(400, description="Unsupported portfolio currency.")

    # Share currency selection and position rules between the portfolio and closed history.
    return render_template(
        "index.html", positions=list_positions(database, currency=currency, closed=closed), history=closed,
        currency=currency, currencies=CURRENCIES, saved=request.args.get("saved") == "1" and not closed,
    )


@purchase_pages.get("/issues/<isin>")
@with_database
def issue_details(database: Database, isin: str):
    """Render the ISIN position with all purchases, or return 404 when no position exists."""

    # Resolve the position from its shared ISIN rather than an arbitrary purchase.
    position = get_position_details(database, isin)
    if position is None:
        abort(404, description="Position not found.")

    # Show the complete calendar and both active and closed purchases in their canonical parent page.
    return render_template("issue_details.html", position=position)


@purchase_pages.get("/purchases/<int:lot_id>")
@with_database
def purchase_details(database: Database, lot_id: int):
    """Render the selected purchase with its shared calendar, or return 404 for an unknown lot_id."""

    # Resolve an existing purchase before rendering its details.
    lot = get_lot_details(database, lot_id)
    if lot is None:
        abort(404, description="Purchase not found.")

    # Present the same stored fields and current state as the portfolio list.
    return render_template("purchase_details.html", lot=lot)
