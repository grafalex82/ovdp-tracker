"""Apply explicit shared issue corrections without rewriting independent purchase history."""

from .db import Database
from .purchases import ISSUE_FIELDS, IssueValidationError, validate_issue
from .sales import SaleValidationError, check_history


def edit_issue(database: Database, issue_id: int, values: dict) -> dict:
    """Validate shared values and atomically save issue_id after checking every purchase and confirmation."""

    # Keep the maturity date explicit when editing an existing shared calendar.
    parsed = validate_issue({**values, "infer_maturity_from_coupons": "0"})

    # Lock the issue's complete history before checking or changing any shared record.
    with database.transaction():
        issue = database.get_issue(issue_id)
        if issue is None:
            raise IssueValidationError({"form": "Position not found."})
        if parsed["isin"] != issue["isin"]:
            raise IssueValidationError({"isin": "The saved ISIN cannot be changed."})
        events = database.list_scheduled_payments(issue_id)
        keys = {(event["kind"], event["planned_date"]) for event in parsed["payments"]}
        removed = {event["id"] for event in events if (event["kind"], event["planned_date"]) not in keys}
        if any(actual["scheduled_payment_id"] in removed for actual in database.list_actual_payments()):
            raise IssueValidationError({"coupons": "Cannot remove an event with a confirmed payment."})

        # Reuse stable events and let any incompatible purchase roll back the entire correction.
        database.update_issue(issue_id, **{key: parsed[key] for key in ISSUE_FIELDS if key != "isin"},
                              payments=parsed["payments"])
        for lot in database.list_lots():
            if lot["issue_id"] != issue_id:
                continue
            if lot["purchase_date"] > parsed["maturity_date"]:
                raise IssueValidationError({"maturity_date": "Maturity date cannot be before the purchase date."})
            try:
                check_history(database, lot, database.list_sales(lot["id"]))
            except SaleValidationError as error:
                raise IssueValidationError({"form": str(error)}) from error

    # Return the saved shared metadata after the complete transaction commits.
    return database.get_issue(issue_id)
