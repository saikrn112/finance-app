from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Session
from pathlib import Path
from src.config import settings


class Base(DeclarativeBase):
    pass


# Ensure data directory exists
Path(settings.database.path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(f"sqlite:///{settings.database.path}", echo=False)
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

    # Investment holding snapshots currency column
    if inspector.has_table("investment_holding_snapshots"):
        inv_columns = {column["name"] for column in inspector.get_columns("investment_holding_snapshots")}
        if "currency" not in inv_columns:
            statements.append("ALTER TABLE investment_holding_snapshots ADD COLUMN currency VARCHAR(3) NOT NULL DEFAULT 'USD'")

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
