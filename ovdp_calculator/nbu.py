"""Fetch the NBU securities directory and parse complete domestic bond data."""

from decimal import Decimal
from http.client import HTTPException
import json
import re
from urllib.error import URLError
from urllib.request import urlopen

DIRECTORY_URL = "https://bank.gov.ua/depo_securities?json"


class NbuError(ValueError):
    """The requested bond could not be fetched or its source record could not be parsed."""


class NbuNotFound(NbuError):
    """The directory has no domestic issue matching the requested ISIN."""


def parse_bond_data(record: dict) -> dict:
    """Parse an NBU record into bond metadata and payment data for subsequent processing.

    The input is one already-decoded directory record, not the entire API response.
    Illustrative input with two different coupon amounts, expressed per bond:

        {
            "cpcode": "UA0000000000",
            "cptype": "DCP",
            "val_code": "UAH",
            "nominal": 1000,
            "razm_date": "2025-01-01",
            "pgs_date": "2026-01-01",
            "auk_proc": Decimal("16.5"),
            "pay_period": 182,
            "cpdescr": "Short-term",
            "payments": [
                {"pay_type": "1", "pay_date": "2025-07-01", "pay_val": Decimal("80.00")},
                {"pay_type": "1", "pay_date": "2026-01-01", "pay_val": Decimal("85.00")},
                {"pay_type": "2", "pay_date": "2026-01-01", "pay_val": 1000},
            ],
        }

    Parsing proceeds as follows:
    1. Map required source fields to application fields, such as cpcode to isin,
       val_code to currency, razm_date to issue_date and pgs_date to maturity_date.
       Reject missing or empty metadata. ISIN lookup and DCP filtering happen in lookup().
    2. Map each payment to kind, planned_date and amount. Type "1" means coupon; "2" and
       "3" mean regular or early redemption. Keep unknown type codes as kind values so
       subsequent processing can decide whether to support them.
    3. Return {"issue": metadata, "payments": events} in source payment order. Convert
       monetary values and the rate to strings without rounding; keep pay_period as supplied.
       The example produces two coupons ("80.00" and "85.00") and a redemption ("1000").

    Raise NbuError for missing fields or malformed record structure. Currency support,
    monetary precision, date consistency and redemption-count rules belong to subsequent
    processing. No purchase fields, purchase validation, network requests or database writes
    are involved here.
    """

    # Require all documented source fields rather than inventing defaults for a partial response.
    mapping = {
        "isin": "cpcode", "currency": "val_code", "nominal": "nominal", "issue_date": "razm_date",
        "maturity_date": "pgs_date", "nominal_rate": "auk_proc", "coupon_period": "pay_period",
        "bond_type": "cpdescr",
    }
    try:
        issue = {field: str(record[source]) for field, source in mapping.items()}
        if any(record[source] is None or isinstance(record[source], bool) for source in mapping.values()):
            raise ValueError("Missing or invalid issue metadata.")
        if not all(value.strip() for value in issue.values()) or not isinstance(record["cpdescr"], str):
            raise ValueError("Missing issue metadata.")
        issue["coupon_period"] = record["pay_period"]
        events = record["payments"]
        if not isinstance(events, list):
            raise ValueError("Missing payment calendar.")

        # Preserve every source payment without applying purchase or supported-calendar rules.
        payments = []
        for event in events:
            kind, day, amount = event["pay_type"], event["pay_date"], event["pay_val"]
            if not isinstance(kind, str) or not isinstance(day, str) or isinstance(amount, bool) or amount is None:
                raise ValueError("Invalid payment date or amount.")
            payments.append({
                "kind": {"1": "coupon", "2": "redemption", "3": "redemption"}.get(kind, kind),
                "planned_date": day, "amount": str(amount),
            })
        return {"issue": issue, "payments": payments}
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise NbuError(f"NBU returned an incomplete or malformed bond record: {error}") from error


class NbuClient:
    """Fetch the public directory for each bond lookup."""

    def __init__(self, *, timeout: float = 10):
        """Configure the NBU request timeout in seconds."""
        self._timeout = timeout


    def _download(self) -> list[dict]:
        # Read a bounded public response and parse decimal literals without binary floats.
        try:
            with urlopen(DIRECTORY_URL, timeout=self._timeout) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise NbuError("The NBU directory response is too large.")
            records = json.loads(payload, parse_float=Decimal)
            if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
                raise NbuError("NBU returned an invalid directory.")
            return records
        except (URLError, OSError, ValueError, HTTPException) as error:
            raise NbuError("Could not load the NBU directory. Retry or choose manual entry.") from error


    def lookup(self, isin: str) -> dict:
        """Fetch metadata and payments for a domestic ISIN, raising NbuError on unavailable data."""

        # Validate and normalize the public identifier before making any network request.
        isin = isin.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin):
            raise NbuError("Enter a complete 12-character ISIN.")

        # Fetch a fresh directory and match only the exact domestic issue.
        records = self._download()
        matches = [row for row in records if row.get("cpcode") == isin and row.get("cptype") == "DCP"]

        # Reject missing or ambiguous records without importing a partial calendar.
        if not matches:
            raise NbuNotFound("ISIN was not found in the NBU domestic bond directory.")
        if len(matches) != 1:
            raise NbuError("NBU returned multiple records for this ISIN.")
        return parse_bond_data(matches[0])
