"""Validate purchases and persist shared issue calendars with per-event amounts."""

from datetime import date
from decimal import Decimal, InvalidOperation
import re

from .db import Database
from .money import parse_money
from .quantity import parse_quantity

CURRENCIES = ("UAH", "USD", "EUR")
ISSUE_FIELDS = (
    "isin", "nominal", "currency", "maturity_date", "issue_date", "nominal_rate", "coupon_period", "bond_type"
)


class IssueValidationError(ValueError):
    """Carry issue and calendar errors independently of any purchase."""

    def __init__(self, errors: dict):
        """Keep field errors for unsupported or inconsistent bond data.

        Store errors as a field-name-to-message dictionary available through self.errors,
        allowing callers to display issue errors independently of purchase validation.
        """
        super().__init__("The bond data contains invalid fields.")
        self.errors = errors


class PurchaseValidationError(ValueError):
    """Carry field errors without discarding submitted values."""

    def __init__(self, errors: dict):
        """Keep field errors for rendering beside the purchase inputs.

        Store the combined issue and purchase errors supplied in errors through self.errors
        so the form can display messages without discarding the submitted values.
        """
        super().__init__("The purchase contains invalid fields.")
        self.errors = errors


def parse_date(value: str) -> date:
    """Parse an ISO calendar date or raise a readable validation error.

    Require value to be a zero-padded YYYY-MM-DD string, then check that the calendar date
    exists. Return a datetime.date object. Raise ValueError for either an invalid format
    or an impossible date, using the same message for both cases.
    """
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Enter a valid date in YYYY-MM-DD format.") from error


def issue_form_values(issue: dict, events: list[dict]) -> dict:
    """Convert shared issue metadata and payment events into purchase-form values.

    Read the metadata listed in ISSUE_FIELDS from issue, convert values to strings and
    replace None with an empty string. Return coupons as date/amount dictionaries sorted
    by planned_date, including historical and zero coupons. Keep monetary amounts as strings.
    Copy the redemption amount separately and set infer_maturity_from_coupons to "0"
    because the issue already supplies its own maturity date.

    Return a new dictionary suitable for form display or validate_issue(). Expect
    complete metadata and a calendar with one redemption; this function does not validate
    them. The supplied issue and events are not modified.
    """
    return {
        **{key: "" if issue[key] is None else str(issue[key]) for key in ISSUE_FIELDS},
        "coupons": [
            {"date": event["planned_date"], "amount": event["amount"]}
            for event in sorted(events, key=lambda event: event["planned_date"]) if event["kind"] == "coupon"
        ],
        "redemption_amount": next(event["amount"] for event in events if event["kind"] == "redemption"),
        "infer_maturity_from_coupons": "0",
    }


def issue_signature(issue: dict, events: list[dict]) -> tuple:
    """Build a comparison key for shared issue metadata and its complete payment calendar.

    Read metadata from issue and payments from events. Compare nominal, a known nominal
    rate and each event amount as Decimal values, making values such as 1000 and 1000.00
    equivalent. Keep an unknown nominal rate distinct from a known zero rate.

    Return a pair containing metadata in ISSUE_FIELDS order and sorted payment tuples
    of kind, date and amount. Event order, database identifiers and purchase-specific
    fields do not affect the key. This is a comparison tuple, not a cryptographic signature;
    it is used to detect differences between saved and imported issues without changing either.
    """

    # Compare numeric metadata by value while keeping unknown manual rates distinct from zero.
    fields = dict(issue)
    fields["nominal"] = parse_money(fields["nominal"])
    if fields["nominal_rate"]:
        fields["nominal_rate"] = Decimal(fields["nominal_rate"])

    # Include each event's own unit amount without storage identifiers or purchase-specific facts.
    calendar = tuple(sorted((event["kind"], event["planned_date"], parse_money(event["amount"])) for event in events))
    return tuple(fields[field] for field in ISSUE_FIELDS), calendar


def validate_issue(values: dict) -> dict:
    """Validate form-shaped bond values and return canonical issue fields with a supported calendar.

    Read shared metadata and coupons from values without requiring purchase fields.
    Normalize ISIN casing and surrounding whitespace, require a nonempty identifier and
    a supported currency, and parse positive nominal and redemption amounts without rounding.
    Default an omitted redemption amount to nominal. Monetary values allow at most two
    decimal places; coupons must be a list of dictionaries with ISO date and nonnegative
    amount strings. Empty or malformed rows are rejected instead of silently discarded.

    Validate issue and maturity dates, unique coupon dates and the maturity boundary.
    An empty maturity date can be inferred from the latest coupon only in manual mode
    when infer_maturity_from_coupons is "1" (the default if omitted). Without coupons,
    an explicit maturity date is required. Optional issue metadata may remain unknown;
    a supplied nominal rate must be finite and nonnegative, and a supplied coupon period
    must be a nonnegative whole number with at most six digits.

    Return a new flat dictionary of issue fields plus payments: date-sorted coupon events
    followed by one separate redemption on maturity. Each event has kind, planned_date
    and its own amount; dates and monetary values are strings, and coupon_period is an
    integer or None. Raise IssueValidationError with collected field messages on failure.
    This function performs no database writes or current-date/purchase checks.
    """

    # Normalize the shared identifier and require a supported currency.
    errors = {}
    issue = {"isin": values.get("isin", "").strip().upper(), "currency": values.get("currency", "")}
    if not issue["isin"]:
        errors["isin"] = "This field is required."
    if issue["currency"] not in CURRENCIES:
        errors["currency"] = "Currency must be UAH, USD, or EUR."

    # Parse issue amounts without float conversion or implicit rounding.
    for field in ("nominal", "redemption_amount"):
        try:
            raw = values.get(field, "")
            if field == "redemption_amount" and not raw.strip():
                raw = values.get("nominal", "")
            amount = parse_money(raw)
            if amount <= 0:
                raise ValueError("Amount must be positive.")
            issue[field] = str(amount)
        except ValueError as error:
            errors[field] = str(error)

    # Validate structured coupons without parsing a separate date-and-amount text format.
    coupons = []
    rows = values.get("coupons", [])
    if not isinstance(rows, list):
        errors["coupons"] = "Coupons must be a list of date and amount objects."
        rows = []
    for row in rows:
        try:
            if not isinstance(row, dict) or set(row) != {"date", "amount"}:
                raise ValueError("Each coupon must contain date and amount strings.")
            if not isinstance(row["date"], str) or not isinstance(row["amount"], str):
                raise ValueError("Each coupon must contain date and amount strings.")
            day, amount = parse_date(row["date"]), parse_money(row["amount"])
            if amount < 0:
                raise ValueError("Amount must be nonnegative.")
            coupons.append({"kind": "coupon", "planned_date": day.isoformat(), "amount": str(amount)})
        except ValueError as error:
            errors["coupons"] = str(error)

    # Validate issue dates and allow maturity inference only for a manual draft.
    for field in ("maturity_date", "issue_date"):
        raw = values.get(field, "").strip()
        if field == "issue_date" and not raw:
            issue[field] = ""
            continue
        if (field == "maturity_date" and not raw and values.get("infer_maturity_from_coupons", "1") == "1"
                and values.get("issue_mode") == "manual"):
            raw = max((event["planned_date"] for event in coupons), default="")
        try:
            issue[field] = parse_date(raw).isoformat()
        except ValueError as error:
            errors[field] = str(error)
    if issue.get("issue_date", "") > issue.get("maturity_date", "9999"):
        errors["issue_date"] = "Issue date cannot be after maturity."
    if len(coupons) != len({event["planned_date"] for event in coupons}):
        errors["coupons"] = "Coupon dates must be unique."
    if any(event["planned_date"] > issue.get("maturity_date", "9999") for event in coupons):
        errors["coupons"] = "Coupon dates must not be after maturity."

    # Preserve optional manual metadata and validate available source values.
    issue["bond_type"] = values.get("bond_type", "").strip()
    issue["nominal_rate"] = values.get("nominal_rate", "").strip().replace(",", ".")
    try:
        rate = issue["nominal_rate"]
        if rate and (not Decimal(rate).is_finite() or Decimal(rate) < 0):
            raise ValueError
    except (ValueError, InvalidOperation):
        errors["nominal_rate"] = "Nominal rate must be a finite nonnegative percentage."
    period = values.get("coupon_period", "")
    if period and (not re.fullmatch(r"[0-9]{1,6}", str(period))):
        errors["coupon_period"] = "Coupon period must be a nonnegative whole number of days."
    issue["coupon_period"] = int(period) if period and "coupon_period" not in errors else None
    if errors:
        raise IssueValidationError(errors)

    # Return the complete schedule, keeping redemption separate from the final coupon.
    issue["payments"] = sorted(coupons, key=lambda event: event["planned_date"]) + [{
        "kind": "redemption", "planned_date": issue["maturity_date"], "amount": issue.pop("redemption_amount"),
    }]
    return issue


def validate_imported_issue(data: dict) -> dict:
    """Check whether parsed source bond data fits the application's supported issue model.

    Expect data in the structure returned by parse_bond_data(): an issue dictionary and
    a payments list. Require supported coupon/redemption event kinds and exactly one
    redemption whose date matches the issue's maturity date. These restrictions belong
    to the application model rather than the source parser.

    Convert the data with issue_form_values() and reuse validate_issue() for currency,
    monetary precision, dates, metadata and coupon-calendar rules. Return a new dictionary
    with the same issue/payments structure and canonical values. Raise IssueValidationError
    if the calendar is unsupported or any issue values fail validation. No purchase is
    created or simulated, and no purchase price, date, quantity or bank is required.
    """

    # Reject source calendars that cannot be represented by the current application model.
    issue, events = data["issue"], data["payments"]
    if any(event["kind"] not in ("coupon", "redemption") for event in events):
        raise IssueValidationError({"coupons": "Unsupported payment type."})
    redemptions = [event for event in events if event["kind"] == "redemption"]
    if len(redemptions) != 1 or redemptions[0]["planned_date"] != issue["maturity_date"]:
        raise IssueValidationError({"maturity_date": "Exactly one redemption matching maturity is required."})

    # Reuse issue-only validation and return the same metadata/calendar structure as the source parser.
    parsed = validate_issue(issue_form_values(issue, events))
    return {"issue": {field: parsed[field] for field in ISSUE_FIELDS}, "payments": parsed["payments"]}


def validate_purchase(values: dict, today: date) -> dict:
    """Validate submitted issue and purchase values and return a canonical purchase dictionary.

    Pass shared fields from values to validate_issue() and collect its errors alongside
    purchase errors. Require a nonempty bank name, a supported positive whole quantity,
    and a positive purchase price with cent precision.
    Require a completed manual, automatic or existing-issue mode; automatic drafts must
    have loaded_isin matching the normalized current identifier.

    Temporarily allow future purchase dates while still rejecting a purchase after maturity.
    Keep today available for restoring the date cutoff without changing callers.
    Return canonical issue fields, payments and independent purchase fields in one new
    dictionary. Raise PurchaseValidationError with field messages when either the issue
    or purchase is invalid. This function validates values without persisting them.
    """

    # Validate the shared issue independently and collect its field errors alongside purchase errors.
    errors = {}
    try:
        purchase = validate_issue(values)
    except IssueValidationError as error:
        errors.update(error.errors)
        purchase = {}

    # Require independent purchase values and a supported positive whole quantity.
    purchase["bank_name"] = values.get("bank_name", "").strip()
    if not purchase["bank_name"]:
        errors["bank_name"] = "This field is required."
    try:
        purchase["quantity"] = parse_quantity(values.get("quantity", ""))
    except ValueError as error:
        errors["quantity"] = str(error)

    # Reject unfinished or stale automatic drafts while retaining an explicit manual mode.
    mode = values.get("issue_mode", "pending")
    if mode not in ("manual", "auto", "existing"):
        errors["isin"] = "Load issue data or explicitly choose manual entry before saving."
    if mode == "auto" and values.get("loaded_isin") != values.get("isin", "").strip().upper():
        errors["isin"] = "Loaded data belongs to a different ISIN. Load the current issue again."

    # Validate the purchase price and date without introducing purchase fields into issue parsing.
    try:
        price = parse_money(values.get("purchase_price", ""))
        if price <= 0:
            raise ValueError("Amount must be positive.")
        purchase["purchase_price"] = str(price)
    except ValueError as error:
        errors["purchase_price"] = str(error)
    try:
        purchase["purchase_date"] = parse_date(values.get("purchase_date", "").strip()).isoformat()
    except ValueError as error:
        errors["purchase_date"] = str(error)
    maturity_date = purchase.get("maturity_date", values.get("maturity_date", "").strip())
    if "maturity_date" not in errors and maturity_date and maturity_date < purchase.get("purchase_date", ""):
        errors["maturity_date"] = "Maturity date cannot be before the purchase date."
    if errors:
        raise PurchaseValidationError(errors)
    return purchase


def create_purchase(database: Database, values: dict, today: date | None = None) -> dict:
    """Validate and atomically save a purchase, creating its shared issue only when new.

    Use database to resolve the ISIN supplied in values and perform all checks and writes
    in one transaction. For an existing issue, supply its saved metadata and calendar as
    defaults before validating submitted values. Pass today, or the current local date
    when omitted, to validation; the future-date cutoff is temporarily disabled.

    Call validate_purchase(), then split its result into issue metadata, payment events
    and independent purchase fields. Reject conflicts with an existing issue's metadata
    or complete calendar, comparing nominal and event amounts numerically. For a new ISIN,
    create the issue and each scheduled payment. Reuse or create the named bank and save
    the purchase linked to that bank and issue.

    Return the purchase dictionary provided by Database.add_lot(). Validation failures
    raise PurchaseValidationError; storage exceptions propagate. Any failure rolls back
    the transaction, leaving no partial issue, calendar, bank or purchase records.
    Existing shared schedules and actual payment receipts are never overwritten here.
    """

    # Resolve the saved issue and validate against a stable snapshot within the write transaction.
    with database.transaction():
        issue = database.get_issue_by_isin(values.get("isin", ""))
        if issue is None and values.get("issue_mode") == "existing":
            raise PurchaseValidationError({"isin": "Saved issue not found. Load NBU data or choose manual entry."})
        original = database.list_scheduled_payments(issue["id"]) if issue else []
        submitted = {**issue_form_values(issue, original), **values, "issue_mode": "existing"} if issue else values
        purchase = validate_purchase(submitted, today if today is not None else date.today())
        bank_name = purchase.pop("bank_name")
        events = purchase.pop("payments")
        issue_values = {field: purchase.pop(field) for field in ISSUE_FIELDS}

        # Reject conflicting shared fields without rewriting a saved calendar or actual receipts.
        if issue:
            errors = {}
            for field, value in issue_values.items():
                stored = issue[field]
                if field == "nominal":
                    stored, value = parse_money(stored), parse_money(value)
                if field == "nominal_rate" and stored and value:
                    stored, value = Decimal(stored), Decimal(value)
                if stored != value:
                    errors[field] = "This field conflicts with the existing ISIN."
            expected = [(event["kind"], event["planned_date"], parse_money(event["amount"])) for event in original]
            received = [(event["kind"], event["planned_date"], parse_money(event["amount"])) for event in events]
            if sorted(expected) != sorted(received):
                errors["coupons"] = "Calendar dates or amounts conflict with the existing ISIN."
            if errors:
                raise PurchaseValidationError(errors)
        else:
            issue = database.add_issue(**issue_values)
            for event in events:
                database.add_scheduled_payment(issue_id=issue["id"], **event)

        # Store only independent purchase parameters after the complete issue has been validated.
        bank = database.add_bank(bank_name)
        return database.add_lot(**purchase, issue_id=issue["id"], bank_id=bank["id"])
