"""High-level storage API with SQLite details kept inside this module."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3

DEFAULT_DATABASE = Path(__file__).resolve().parent.parent / "instance" / "portfolio.sqlite3"
SCHEMA_VERSION = 1

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
    CREATE TABLE bond_lots (
        id INTEGER PRIMARY KEY,
        isin TEXT NOT NULL,
        purchase_date TEXT NOT NULL,
        nominal TEXT NOT NULL,
        currency TEXT NOT NULL CHECK (currency IN ('UAH', 'USD', 'EUR')),
        purchase_price TEXT NOT NULL,
        quantity INTEGER NOT NULL
            CHECK (typeof(quantity) = 'integer' AND quantity > 0),
        bank_id INTEGER NOT NULL REFERENCES banks(id) ON DELETE RESTRICT,
        coupon_amount TEXT NOT NULL,
        maturity_date TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE scheduled_payments (
        id INTEGER PRIMARY KEY,
        lot_id INTEGER NOT NULL REFERENCES bond_lots(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('coupon', 'redemption')),
        planned_date TEXT NOT NULL,
        UNIQUE (lot_id, kind, planned_date)
    )
    """,
    """
    CREATE UNIQUE INDEX one_redemption_per_lot
        ON scheduled_payments(lot_id) WHERE kind = 'redemption'
    """,
    """
    CREATE TABLE actual_payments (
        id INTEGER PRIMARY KEY,
        scheduled_payment_id INTEGER NOT NULL UNIQUE
            REFERENCES scheduled_payments(id) ON DELETE CASCADE,
        actual_date TEXT NOT NULL,
        amount TEXT NOT NULL
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


class DatabaseError(RuntimeError):
    """A storage operation could not be completed."""


class DataIntegrityError(DatabaseError):
    """The supplied data conflicts with a database constraint."""


class SchemaVersionError(DatabaseError):
    """The database has an unsupported schema version and must not be changed."""


class Database:
    """Own a SQLite connection and expose storage operations without Flask."""

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
                    f"(supported version: {SCHEMA_VERSION}). The database has not been changed."
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


    def add_lot(
        self,
        *, isin: str, purchase_date: str, nominal: str, currency: str,
        purchase_price: str, quantity: int, bank_id: int, coupon_amount: str,
        maturity_date: str,
    ) -> dict:
        """Store purchase details and return the new lot dictionary, including its ID.

        Args:
            isin: The bond issue identifier.
            purchase_date: The purchase date as an ISO date string.
            nominal: The nominal value per bond as a decimal string.
            currency: The currency code: UAH, USD, or EUR.
            purchase_price: The purchase price per bond as a decimal string.
            quantity: The positive integer number of purchased bonds.
            bank_id: The identifier of the bank used for this purchase.
            coupon_amount: The coupon amount per bond as a decimal string.
            maturity_date: The maturity date as an ISO date string.

        Returns:
            A dictionary containing the supplied purchase fields and the generated ID.
        """
        return self._insert_record("bond_lots", {
            "isin": isin,
            "purchase_date": purchase_date,
            "nominal": nominal,
            "currency": currency,
            "purchase_price": purchase_price,
            "quantity": quantity,
            "bank_id": bank_id,
            "coupon_amount": coupon_amount,
            "maturity_date": maturity_date,
        })


    def get_lot(self, lot_id: int) -> dict | None:
        """Find a purchase by lot ID and return its dictionary, or None if no lot exists."""
        return self._fetch_one("SELECT * FROM bond_lots WHERE id = ?", (lot_id,))


    def list_lots(self) -> list[dict]:
        """List all purchases as dictionaries ordered by lot ID."""
        return self._fetch_all("SELECT * FROM bond_lots ORDER BY id")


    def delete_lot(self, lot_id: int) -> bool:
        """Delete a lot and its history by ID, preserving the bank; return True if removed or False if absent."""
        with self._write_scope():
            return self._execute("DELETE FROM bond_lots WHERE id = ?", (lot_id,)).rowcount > 0


    def add_scheduled_payment(self, *, lot_id: int, kind: str, planned_date: str) -> dict:
        """Schedule a coupon or redemption for a lot and ISO date; return the stored event dictionary."""
        return self._insert_record("scheduled_payments", {"lot_id": lot_id, "kind": kind, "planned_date": planned_date})


    def list_scheduled_payments(self, lot_id: int | None = None) -> list[dict]:
        """List planned event dictionaries by ID; filter by lot_id when supplied, or include all lots for None."""

        # Read the complete schedule when no lot filter is supplied.
        if lot_id is None:
            return self._fetch_all("SELECT * FROM scheduled_payments ORDER BY id")

        # Restrict the schedule to the requested lot.
        return self._fetch_all("SELECT * FROM scheduled_payments WHERE lot_id = ? ORDER BY id", (lot_id,))


    def add_actual_payment(self, *, scheduled_payment_id: int, actual_date: str, amount: str) -> dict:
        """Record the actual date and amount string for a scheduled event; return the confirmation dictionary."""
        return self._insert_record("actual_payments", {
            "scheduled_payment_id": scheduled_payment_id,
            "actual_date": actual_date,
            "amount": amount,
        })


    def list_actual_payments(self, lot_id: int | None = None) -> list[dict]:
        """List confirmations by ID, optionally filtered by lot_id; return dictionaries for all lots when None."""

        # Read all confirmations when no lot filter is supplied.
        if lot_id is None:
            return self._fetch_all("SELECT * FROM actual_payments ORDER BY id")

        # Resolve each confirmation's lot through its planned event.
        return self._fetch_all(
            """SELECT a.* FROM actual_payments a
               JOIN scheduled_payments p ON p.id = a.scheduled_payment_id
               WHERE p.lot_id = ? ORDER BY a.id""", (lot_id,)
        )


    def add_sale(self, *, lot_id: int, sale_date: str, sale_price: str, quantity: int) -> dict:
        """Record a lot's sale date, unit price string, and quantity; return the stored sale dictionary."""
        return self._insert_record("sales", {
            "lot_id": lot_id, "sale_date": sale_date,
            "sale_price": sale_price, "quantity": quantity,
        })


    def list_sales(self, lot_id: int | None = None) -> list[dict]:
        """List sale dictionaries by ID, optionally filtered by lot_id; None selects all lots."""

        # Read every sale when no lot filter is supplied.
        if lot_id is None:
            return self._fetch_all("SELECT * FROM sales ORDER BY id")

        # Restrict the result to sales from the requested lot.
        return self._fetch_all("SELECT * FROM sales WHERE lot_id = ? ORDER BY id", (lot_id,))
