"""High-level storage API with SQLite details kept inside this module."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3

from ovdp_tracker.money import parse_money

DEFAULT_DATABASE = Path(__file__).resolve().parent.parent / "instance" / "portfolio.sqlite3"
SCHEMA_VERSION = 3

# Execute individual SQL statements to create the schema in one transaction.
# In Python 3.11, executescript() commits any pending transaction first.
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE banks (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        normalized_name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE bond_issues (
        id INTEGER PRIMARY KEY,
        isin TEXT NOT NULL UNIQUE CHECK (length(trim(isin)) > 0),
        nominal TEXT NOT NULL,
        currency TEXT NOT NULL CHECK (currency IN ('UAH', 'USD', 'EUR')),
        maturity_date TEXT NOT NULL,
        issue_date TEXT NOT NULL DEFAULT '',
        nominal_rate TEXT NOT NULL DEFAULT '',
        coupon_period INTEGER,
        bond_type TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE bond_lots (
        id INTEGER PRIMARY KEY,
        issue_id INTEGER NOT NULL REFERENCES bond_issues(id) ON DELETE RESTRICT,
        purchase_date TEXT NOT NULL,
        purchase_price TEXT NOT NULL,
        quantity INTEGER NOT NULL
            CHECK (typeof(quantity) = 'integer' AND quantity > 0),
        bank_id INTEGER NOT NULL REFERENCES banks(id) ON DELETE RESTRICT,
        UNIQUE (id, issue_id)
    )
    """,
    """
    CREATE TABLE scheduled_payments (
        id INTEGER PRIMARY KEY,
        issue_id INTEGER NOT NULL REFERENCES bond_issues(id) ON DELETE RESTRICT,
        kind TEXT NOT NULL CHECK (kind IN ('coupon', 'redemption')),
        planned_date TEXT NOT NULL,
        amount TEXT NOT NULL,
        UNIQUE (issue_id, kind, planned_date),
        UNIQUE (id, issue_id)
    )
    """,
    """
    CREATE UNIQUE INDEX one_redemption_per_issue
        ON scheduled_payments(issue_id) WHERE kind = 'redemption'
    """,
    """
    CREATE TABLE actual_payments (
        id INTEGER PRIMARY KEY,
        lot_id INTEGER NOT NULL,
        issue_id INTEGER NOT NULL,
        scheduled_payment_id INTEGER NOT NULL,
        actual_date TEXT NOT NULL,
        amount TEXT NOT NULL,
        UNIQUE (lot_id, scheduled_payment_id),
        FOREIGN KEY (lot_id, issue_id) REFERENCES bond_lots(id, issue_id) ON DELETE CASCADE,
        FOREIGN KEY (scheduled_payment_id, issue_id) REFERENCES scheduled_payments(id, issue_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE sales (
        id INTEGER PRIMARY KEY,
        lot_id INTEGER NOT NULL REFERENCES bond_lots(id) ON DELETE CASCADE,
        sale_date TEXT NOT NULL,
        sale_price TEXT NOT NULL,
        quantity INTEGER NOT NULL
            CHECK (typeof(quantity) = 'integer' AND quantity > 0)
    )
    """,
)

# Check shared maturity boundaries when inserting or updating calendar events.
_CALENDAR_CONFLICT = """
    SELECT 1 FROM bond_issues
    WHERE id = NEW.issue_id AND (
        (NEW.kind = 'redemption' AND NEW.planned_date != maturity_date)
        OR (NEW.kind = 'coupon' AND NEW.planned_date > maturity_date)
    )
"""
_SCHEMA_STATEMENTS += tuple(
    f"""
    CREATE TRIGGER validate_event_{operation.lower()} BEFORE {operation} ON scheduled_payments
    WHEN EXISTS ({_CALENDAR_CONFLICT})
    BEGIN
        SELECT RAISE(ABORT, 'Event date conflicts with issue maturity.');
    END
    """
    for operation in ("INSERT", "UPDATE")
)

_LOT_SELECT = """
    SELECT l.*, i.isin, i.nominal, i.currency, i.maturity_date,
           i.issue_date, i.nominal_rate, i.coupon_period, i.bond_type
    FROM bond_lots l JOIN bond_issues i ON i.id = l.issue_id
"""


class DatabaseError(RuntimeError):
    """A storage operation could not be completed."""


class DataIntegrityError(DatabaseError):
    """The supplied data conflicts with a database constraint."""


class SchemaVersionError(DatabaseError):
    """The database has an unsupported schema version and must not be changed."""


class Database:
    """Own a SQLite connection and expose storage operations without Flask.

    Monetary writes raise ValueError for invalid strings or more than two decimal places.
    """

    def __init__(self, path: str | Path | None = None):
        """Open storage at path or the default location for None and initialize its schema."""

        # Resolve the storage path and prepare an unopened instance.
        self._connection = None
        database_path = Path(path) if path is not None else DEFAULT_DATABASE

        # Open the file and enable connection-level integrity checks.
        try:
            database_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(database_path)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
        except (OSError, sqlite3.Error) as error:
            self.close()
            raise DatabaseError("Could not open the database.") from error

        # Initialize the schema and release the connection if initialization fails.
        try:
            self._initialize_schema()
        except BaseException:
            self.close()
            raise


    def _get_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise DatabaseError("The database is closed.")
        return self._connection


    def close(self) -> None:
        """Close storage and roll back unfinished writes. Repeated calls are safe."""
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()


    def __enter__(self):
        """Enter the database context and return this open Database instance."""
        self._get_connection()
        return self


    def __exit__(self, exception_type, exception, traceback):
        """Close storage when the context exits and let any supplied exception propagate."""
        self.close()


    @contextmanager
    def transaction(self):
        """Group storage operations atomically, committing on success and rolling back on error."""

        # Reject nested transactions before starting another unit of work.
        connection = self._get_connection()
        if connection.in_transaction:
            raise DatabaseError("Nested transactions are not supported.")

        # Acquire the write lock before the caller performs checks or changes.
        self._execute("BEGIN IMMEDIATE")

        # Commit completed work and roll back any failure from the caller or commit.
        try:
            yield
            self._execute("COMMIT")
        except BaseException:
            connection.rollback()
            raise


    def _execute(self, sql, parameters=()):
        try:
            return self._get_connection().execute(sql, parameters)
        except sqlite3.IntegrityError as error:
            raise DataIntegrityError("The data violates a database constraint.") from error
        except sqlite3.Error as error:
            raise DatabaseError("The database operation failed.") from error


    def _fetch_one(self, sql, parameters=()):
        row = self._execute(sql, parameters).fetchone()
        return dict(row) if row is not None else None


    def _fetch_all(self, sql, parameters=()):
        return [dict(row) for row in self._execute(sql, parameters).fetchall()]


    @contextmanager
    def _write_scope(self):
        # Writes join an explicit transaction, or commit independently otherwise.
        if self._get_connection().in_transaction:
            yield
        else:
            with self.transaction():
                yield


    def _insert_record(self, table, values):
        # Prepare identifiers supplied by the API and placeholders for bound values.
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)

        # Insert and read back the new record within the same write scope.
        with self._write_scope():
            cursor = self._execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values()))
            return self._fetch_one(f"SELECT * FROM {table} WHERE id = ?", (cursor.lastrowid,))


    def get_schema_version(self) -> int:
        """Read the storage schema version and return it as an integer."""
        return self._execute("PRAGMA user_version").fetchone()[0]


    def _initialize_schema(self) -> None:
        """Initialize an empty database without recreating an existing schema."""

        # Keep version checks and schema creation in a single atomic transaction.
        with self.transaction():
            # Reuse the current schema without recreating any database objects.
            version = self.get_schema_version()
            if version == SCHEMA_VERSION:
                return

            # Reject versions that this application cannot safely interpret.
            if version != 0:
                raise SchemaVersionError(
                    f"Unsupported SQLite schema version: {version} "
                    f"(supported version: {SCHEMA_VERSION}). The database has not been changed. "
                    "To discard development data, stop the application and delete the database file before restarting."
                )

            # Refuse to adopt an existing schema that has no version marker.
            if self._execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                raise SchemaVersionError(
                    "The existing database has an unversioned schema. "
                    "The database has not been changed."
                )

            # Create the empty schema and assign its version only after all statements succeed.
            for statement in _SCHEMA_STATEMENTS:
                self._execute(statement)
            self._execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


    def add_bank(self, name: str) -> dict:
        """Create or reuse a bank by its trimmed, case-insensitive name; return an id/name dictionary."""

        # Normalize the supplied name and reject empty input.
        name = name.strip()
        if not name:
            raise ValueError("Bank name must not be empty.")
        normalized_name = name.casefold()

        # Reuse a matching bank within the same write scope as any new insertion.
        with self._write_scope():
            existing = self._fetch_one("SELECT id, name FROM banks WHERE normalized_name = ?", (normalized_name,))
            if existing is not None:
                return existing

            # Persist the new bank and return its public representation.
            bank = self._insert_record("banks", {"name": name, "normalized_name": normalized_name})
            return self.get_bank(bank["id"])


    def get_bank(self, bank_id: int) -> dict | None:
        """Find a bank by ID and return its id/name dictionary, or None if no bank exists."""
        return self._fetch_one("SELECT id, name FROM banks WHERE id = ?", (bank_id,))


    def list_banks(self) -> list[dict]:
        """List all banks as id/name dictionaries ordered by ID."""
        return self._fetch_all("SELECT id, name FROM banks ORDER BY id")


    def delete_bank(self, bank_id: int) -> bool:
        """Delete an unreferenced bank by ID; return True if removed or False if absent."""
        with self._write_scope():
            return self._execute("DELETE FROM banks WHERE id = ?", (bank_id,)).rowcount > 0


    def add_issue(
        self, *, isin: str, nominal: str, currency: str, maturity_date: str, issue_date: str = "",
        nominal_rate: str = "", coupon_period: int | None = None, bond_type: str = "",
    ) -> dict:
        """Create an issue with exact nominal, ISO dates and optional source metadata; return its dictionary."""
        return self._insert_record("bond_issues", {
            "isin": isin.strip().upper(), "nominal": str(parse_money(nominal)), "currency": currency,
            "maturity_date": maturity_date, "issue_date": issue_date, "nominal_rate": nominal_rate,
            "coupon_period": coupon_period, "bond_type": bond_type,
        })


    def get_issue(self, issue_id: int) -> dict | None:
        """Find a shared issue by ID and return its dictionary, or None if absent."""
        return self._fetch_one("SELECT * FROM bond_issues WHERE id = ?", (issue_id,))


    def get_issue_by_isin(self, isin: str) -> dict | None:
        """Find a shared issue by trimmed ISIN and return its dictionary, or None if absent."""
        return self._fetch_one("SELECT * FROM bond_issues WHERE isin = ?", (isin.strip().upper(),))


    def list_issues(self) -> list[dict]:
        """List shared issue dictionaries ordered by ID, including issues without purchases."""
        return self._fetch_all("SELECT * FROM bond_issues ORDER BY id")


    def update_issue(
        self, issue_id: int, *, nominal: str, currency: str, maturity_date: str, issue_date: str,
        nominal_rate: str, coupon_period: int | None, bond_type: str, payments: list[dict],
    ) -> None:
        """Save metadata and the complete calendar for issue_id atomically, preserving its ISIN.

        Receive ISO dates and exact unit amounts in payments. Retain event IDs for matching
        kind/date pairs, update their amounts and remove omitted events. Referenced events
        cannot be removed. Purchase history validation belongs to the caller's transaction.
        """

        # Normalize exact amounts before changing any persisted fields.
        nominal = str(parse_money(nominal))
        payments = [{**event, "amount": str(parse_money(event["amount"]))} for event in payments]
        keys = {(event["kind"], event["planned_date"]) for event in payments}

        # Reconcile the calendar in place so existing confirmations keep their event identities.
        with self._write_scope():
            existing = {(event["kind"], event["planned_date"]): event
                        for event in self.list_scheduled_payments(issue_id)}
            self._execute(
                "UPDATE bond_issues SET nominal = ?, currency = ?, maturity_date = ?, issue_date = ?, "
                "nominal_rate = ?, coupon_period = ?, bond_type = ? WHERE id = ?",
                (nominal, currency, maturity_date, issue_date, nominal_rate, coupon_period, bond_type, issue_id),
            )
            for key, event in existing.items():
                if key not in keys:
                    self._execute("DELETE FROM scheduled_payments WHERE id = ?", (event["id"],))
            for event in payments:
                saved = existing.get((event["kind"], event["planned_date"]))
                if saved:
                    self._execute("UPDATE scheduled_payments SET amount = ? WHERE id = ?",
                                  (event["amount"], saved["id"]))
                else:
                    self.add_scheduled_payment(issue_id=issue_id, **event)


    def add_lot(self, *, issue_id: int, purchase_date: str, purchase_price: str, quantity: int, bank_id: int) -> dict:
        """Store issue ID, ISO purchase date, exact unit price, quantity and bank; return the lot with issue fields."""
        with self._write_scope():
            lot = self._insert_record("bond_lots", {
                "issue_id": issue_id, "purchase_date": purchase_date,
                "purchase_price": str(parse_money(purchase_price)), "quantity": quantity, "bank_id": bank_id,
            })
            return self.get_lot(lot["id"])


    def get_lot(self, lot_id: int) -> dict | None:
        """Find a purchase by lot ID and return its dictionary, or None if no lot exists."""
        return self._fetch_one(_LOT_SELECT + " WHERE l.id = ?", (lot_id,))


    def list_lots(self) -> list[dict]:
        """List all purchases as dictionaries ordered by lot ID."""
        return self._fetch_all(_LOT_SELECT + " ORDER BY l.id")


    def delete_lot(self, lot_id: int) -> bool:
        """Delete a lot's own history by ID, preserving its issue, calendar and bank; return whether it was removed."""
        with self._write_scope():
            return self._execute("DELETE FROM bond_lots WHERE id = ?", (lot_id,)).rowcount > 0


    def add_scheduled_payment(self, *, issue_id: int, kind: str, planned_date: str, amount: str) -> dict:
        """Schedule an ISO-dated event with its exact amount per bond; return the shared event dictionary."""

        # Validate the event's own amount before opening a write scope.
        parsed = parse_money(amount)
        if parsed < 0 or (kind == "redemption" and parsed == 0):
            raise ValueError("Event amount must be nonnegative and redemption must be positive.")

        # Keep unit amounts on calendar events rather than on the shared issue.
        return self._insert_record("scheduled_payments", {
            "issue_id": issue_id, "kind": kind, "planned_date": planned_date, "amount": str(parsed),
        })


    def list_scheduled_payments(self, issue_id: int | None = None) -> list[dict]:
        """List shared event dictionaries by ID, optionally restricted to issue_id; None selects all issues."""

        # Read the complete schedule when no issue filter is supplied.
        if issue_id is None:
            return self._fetch_all("SELECT * FROM scheduled_payments ORDER BY id")

        # Restrict the schedule to the requested shared issue.
        return self._fetch_all("SELECT * FROM scheduled_payments WHERE issue_id = ? ORDER BY id", (issue_id,))


    def add_actual_payment(self, *, lot_id: int, scheduled_payment_id: int, actual_date: str, amount: str) -> dict:
        """Confirm a lot/event pair with its ISO actual date and exact total amount; return the stored dictionary."""

        # Parse money before writing and resolve the purchase's shared issue.
        amount = str(parse_money(amount))
        with self._write_scope():
            lot = self.get_lot(lot_id)
            if lot is None:
                raise DataIntegrityError("The purchase does not exist.")

            # Composite foreign keys reject events belonging to another issue.
            return self._insert_record("actual_payments", {
                "lot_id": lot_id, "issue_id": lot["issue_id"], "scheduled_payment_id": scheduled_payment_id,
                "actual_date": actual_date, "amount": amount,
            })


    def list_actual_payments(self, lot_id: int | None = None) -> list[dict]:
        """List confirmations by ID, optionally filtered by lot_id; return dictionaries for all lots when None."""

        # Read all confirmations when no lot filter is supplied.
        if lot_id is None:
            return self._fetch_all("SELECT * FROM actual_payments ORDER BY id")

        # Restrict confirmations to the requested purchase without duplicating its shared calendar.
        return self._fetch_all("SELECT * FROM actual_payments WHERE lot_id = ? ORDER BY id", (lot_id,))


    def get_actual_payment(self, actual_id: int, *, lot_id: int) -> dict | None:
        """Return actual_id's receipt owned by lot_id, or None when absent or owned by another purchase."""
        return self._fetch_one("SELECT * FROM actual_payments WHERE id = ? AND lot_id = ?", (actual_id, lot_id))


    def update_actual_payment(self, actual_id: int, *, lot_id: int, actual_date: str, amount: str) -> dict | None:
        """Correct actual_id's ISO date and exact total within lot_id; return the receipt or None if not owned."""
        with self._write_scope():
            self._execute("UPDATE actual_payments SET actual_date = ?, amount = ? WHERE id = ? AND lot_id = ?",
                          (actual_date, str(parse_money(amount)), actual_id, lot_id))
            return self.get_actual_payment(actual_id, lot_id=lot_id)


    def delete_actual_payment(self, actual_id: int, *, lot_id: int) -> bool:
        """Delete actual_id only within lot_id; return False if the receipt is absent or belongs to another purchase."""
        with self._write_scope():
            cursor = self._execute("DELETE FROM actual_payments WHERE id = ? AND lot_id = ?", (actual_id, lot_id))
            return cursor.rowcount > 0


    def add_sale(self, *, lot_id: int, sale_date: str, sale_price: str, quantity: int) -> dict:
        """Record a lot's sale date, exact decimal unit price string, and quantity; return the sale dictionary."""
        return self._insert_record("sales", {
            "lot_id": lot_id, "sale_date": sale_date,
            "sale_price": str(parse_money(sale_price)), "quantity": quantity,
        })


    def list_sales(self, lot_id: int | None = None) -> list[dict]:
        """List sale dictionaries by ID, optionally filtered by lot_id; None selects all lots."""

        # Read every sale when no lot filter is supplied.
        if lot_id is None:
            return self._fetch_all("SELECT * FROM sales ORDER BY id")

        # Restrict the result to sales from the requested lot.
        return self._fetch_all("SELECT * FROM sales WHERE lot_id = ? ORDER BY id", (lot_id,))


    def delete_sale(self, sale_id: int, *, lot_id: int) -> bool:
        """Delete sale_id only when it belongs to lot_id; return True if removed or False if absent or foreign."""
        with self._write_scope():
            return self._execute("DELETE FROM sales WHERE id = ? AND lot_id = ?", (sale_id, lot_id)).rowcount > 0


    def update_lot(self, lot_id: int, *, purchase_date: str, purchase_price: str, quantity: int, bank_id: int) -> None:
        """Update the independent purchase fields for lot_id using an ISO date and exact unit price."""
        with self._write_scope():
            self._execute(
                "UPDATE bond_lots SET purchase_date = ?, purchase_price = ?, quantity = ?, bank_id = ? WHERE id = ?",
                (purchase_date, str(parse_money(purchase_price)), quantity, bank_id, lot_id),
            )


    def update_sale(self, sale_id: int, *, sale_date: str, sale_price: str, quantity: int) -> None:
        """Update sale_id with an ISO date, exact unit price and quantity."""
        with self._write_scope():
            self._execute("UPDATE sales SET sale_date = ?, sale_price = ?, quantity = ? WHERE id = ?",
                          (sale_date, str(parse_money(sale_price)), quantity, sale_id))
