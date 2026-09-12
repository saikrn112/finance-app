import os

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Session
from pathlib import Path
from src.config import settings


class Base(DeclarativeBase):
    pass


# Ensure data directory exists
Path(settings.database.path).parent.mkdir(parents=True, exist_ok=True)

# Long enough to ride out the background sync's write transaction, short enough that a
# blocked request fails fast rather than pinning a pooled connection.
SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get("FINANCE_APP_SQLITE_BUSY_TIMEOUT_MS", "15000"))

engine = create_engine(f"sqlite:///{settings.database.path}", echo=False)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record):
    """Wait briefly for the write lock instead of failing instantly.

    The background auto-sync thread holds a write transaction while it talks to Plaid, and
    SQLite's default busy timeout of 0 turns any overlapping request into an immediate
    "database is locked" — which is how editing a category during a sync returned a 500.
    A few seconds covers those windows.

    Note: deliberately *not* WAL. The database lives on a Finch/Lima bind mount, where
    WAL's shared-memory file is unreliable and produces "disk I/O error".
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


SessionLocal = sessionmaker(bind=engine)

_vault_dirty_paused = False


def pause_dirty_tracking():
    global _vault_dirty_paused
    _vault_dirty_paused = True


def resume_dirty_tracking():
    global _vault_dirty_paused
    _vault_dirty_paused = False


@event.listens_for(Session, "after_flush")
def _mark_dirty_on_flush(session, flush_context):
    if _vault_dirty_paused:
        return
    if session.new or session.dirty or session.deleted:
        from src.api.routes.settings import mark_vault_dirty
        mark_vault_dirty()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    _ensure_transaction_columns()


def _ensure_transaction_columns() -> None:
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("transactions")}
    statements: list[str] = []
    if "pending" not in columns:
        statements.append("ALTER TABLE transactions ADD COLUMN pending BOOLEAN NOT NULL DEFAULT 0")
    if "pending_transaction_id" not in columns:
        statements.append("ALTER TABLE transactions ADD COLUMN pending_transaction_id VARCHAR")
    if "plaid_account_id" not in columns:
        statements.append("ALTER TABLE transactions ADD COLUMN plaid_account_id VARCHAR")
    if "plaid_mask" not in columns:
        statements.append("ALTER TABLE transactions ADD COLUMN plaid_mask VARCHAR")
    if "authorized_date" not in columns:
        statements.append("ALTER TABLE transactions ADD COLUMN authorized_date DATE")
    if "original_description" not in columns:
        statements.append("ALTER TABLE transactions ADD COLUMN original_description VARCHAR")
    if "payment_channel" not in columns:
        statements.append("ALTER TABLE transactions ADD COLUMN payment_channel VARCHAR")
    if "currency" not in columns:
        statements.append("ALTER TABLE transactions ADD COLUMN currency VARCHAR(3) NOT NULL DEFAULT 'USD'")

    # Account snapshots currency column
    if inspector.has_table("account_snapshots"):
        snapshot_columns = {column["name"] for column in inspector.get_columns("account_snapshots")}
        if "currency" not in snapshot_columns:
            statements.append("ALTER TABLE account_snapshots ADD COLUMN currency VARCHAR(3) NOT NULL DEFAULT 'USD'")
        if "account_key" not in snapshot_columns:
            statements.append("ALTER TABLE account_snapshots ADD COLUMN account_key VARCHAR")
        if "account_name" not in snapshot_columns:
            statements.append("ALTER TABLE account_snapshots ADD COLUMN account_name VARCHAR")

    if inspector.has_table("source_balance_history"):
        history_columns = {column["name"] for column in inspector.get_columns("source_balance_history")}
        if "account_key" not in history_columns:
            statements.append("ALTER TABLE source_balance_history ADD COLUMN account_key VARCHAR")
        if "account_name" not in history_columns:
            statements.append("ALTER TABLE source_balance_history ADD COLUMN account_name VARCHAR")
        for index in inspector.get_indexes("source_balance_history"):
            if index["name"] == "ix_source_balance_history_source_date" and index.get("unique"):
                statements.append("DROP INDEX ix_source_balance_history_source_date")
                statements.append("CREATE INDEX ix_source_balance_history_source_date ON source_balance_history (source, date)")
        statements.append(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_source_balance_history_account_day "
            "ON source_balance_history (account_key, date) WHERE account_key IS NOT NULL"
        )

    # Investment holding snapshots currency column
    if inspector.has_table("investment_holding_snapshots"):
        inv_columns = {column["name"] for column in inspector.get_columns("investment_holding_snapshots")}
        if "currency" not in inv_columns:
            statements.append("ALTER TABLE investment_holding_snapshots ADD COLUMN currency VARCHAR(3) NOT NULL DEFAULT 'USD'")

    # Unequal split shares. init_db() only creates missing *tables*, so an added column on a
    # live table needs an explicit ALTER here.
    if inspector.has_table("transaction_project_splits"):
        split_columns = {column["name"] for column in inspector.get_columns("transaction_project_splits")}
        if "share_amount" not in split_columns:
            statements.append("ALTER TABLE transaction_project_splits ADD COLUMN share_amount NUMERIC(10, 2)")

    if inspector.has_table("contacts"):
        contact_columns = {column["name"] for column in inspector.get_columns("contacts")}
        if "is_self" not in contact_columns:
            statements.append("ALTER TABLE contacts ADD COLUMN is_self BOOLEAN NOT NULL DEFAULT 0")

    if inspector.has_table("projects"):
        project_columns = {column["name"] for column in inspector.get_columns("projects")}
        if "splitwise_group_id" not in project_columns:
            statements.append("ALTER TABLE projects ADD COLUMN splitwise_group_id VARCHAR")

    if inspector.has_table("plaid_api_usage"):
        usage_columns = {column["name"] for column in inspector.get_columns("plaid_api_usage")}
        if "status" not in usage_columns:
            statements.append("ALTER TABLE plaid_api_usage ADD COLUMN status VARCHAR NOT NULL DEFAULT 'attempted'")

    # Create exchange_rates table if it doesn't exist
    if not inspector.has_table("exchange_rates"):
        statements.append(
            "CREATE TABLE exchange_rates ("
            "id VARCHAR PRIMARY KEY, "
            "date DATE NOT NULL, "
            "from_currency VARCHAR(3) NOT NULL, "
            "to_currency VARCHAR(3) NOT NULL, "
            "rate NUMERIC(12, 6) NOT NULL"
            ")"
        )
        statements.append(
            "CREATE UNIQUE INDEX ix_rate_lookup ON exchange_rates (date, from_currency, to_currency)"
        )

    if not statements:
        return
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
