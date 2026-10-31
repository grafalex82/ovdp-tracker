"""Adapt the independent database API to Flask request handlers."""

from datetime import date
from functools import wraps

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for

from .db import Database, DatabaseError
from .money import operation_amount, parse_money
from .portfolio import get_lot_details, list_lot_groups
from .purchases import CURRENCIES, PurchaseValidationError, create_purchase

purchase_pages = Blueprint("purchases", __name__)


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
        "coupon_amount": "0", "maturity_manual": "0",
    }
    errors = {}
    status = 200

    # Preserve every submitted value on validation or storage failure.
    if request.method == "POST":
        values = request.form.to_dict()
        try:
            create_purchase(database, values, today)
        except PurchaseValidationError as error:
            errors = error.errors
            status = 400
        except DatabaseError:
            current_app.logger.exception("Could not save the purchase.")
            errors["form"] = "Could not save the purchase. No changes were saved; please try again."
            status = 503
        else:
            return redirect(url_for("index", saved="1"))

    # Render the same form with field errors and the original input strings.
    return render_template(
        "purchase_form.html", values=values, errors=errors, currencies=CURRENCIES,
        today=today.isoformat(),
    ), status


@purchase_pages.get("/history")
@with_database
def purchase_history(database: Database):
    """Render closed purchases grouped by their shared ISIN."""
    return render_template("index.html", groups=list_lot_groups(database, closed=True), history=True, saved=False)


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
