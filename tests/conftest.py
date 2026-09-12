"""Shared test fixtures and configuration."""
import pytest
import tempfile
import os

# Must be set before anything imports the app: the auto-task worker starts a Plaid sync as
# soon as the app starts up and holds the shared job lock while it runs, which races the
# requests the tests make.
os.environ.setdefault("FINANCE_APP_DISABLE_AUTO_TASKS", "1")
from pathlib import Path
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base
from src.models.transaction import Transaction, Subscription, Rule, SyncLog
from src.plugins.base import ParserPlugin, CsvColumnConfig


def _ensure_test_plugins_registered():
    """Register minimal test plugins so import tests work without personal plugins."""
    from src.plugins.loader import get_registry, _registry, load_plugins

    # Ensure plugins are loaded first (picks up any installed plugins)
    load_plugins()

    # Only add test plugins if their source_key isn't already registered
    existing_keys = {p.source_key for p in _registry}

    test_plugins = [
        ParserPlugin(
            source_key="acme_payroll",
            label="Acme Corp Payroll",
            record_type="payslip",
            allowed_kinds={"payslip_pdf"},
            domain="payroll",
            directory_name="acme_payroll",
            group="Payroll",
            hint="Upload payslip PDF",
        ),
        ParserPlugin(
            source_key="example_retirement",
            label="National Retirement Plan",
            record_type="retirement",
            allowed_kinds={"retirement_csv", "retirement_statement_pdf"},
            domain="retirement",
            directory_name="example_retirement",
            group="Retirement",
            hint="Upload retirement CSV or statement",
        ),
        ParserPlugin(
            source_key="example_bank",
            label="Example Bank",
            record_type="transactions",
            allowed_kinds={"csv", "statement_pdf"},
            domain="financial_accounts",
            directory_name="example_bank",
            group="Bank Accounts",
            is_credit_card=False,
            csv_config=CsvColumnConfig(date_col="Date", amount_col="Amount", merchant_col="Description"),
        ),
        ParserPlugin(
            source_key="example_card",
            label="Example Card",
            record_type="transactions",
            allowed_kinds={"csv", "statement_pdf"},
            domain="financial_accounts",
            directory_name="example_card",
            group="Credit Cards",
            is_credit_card=True,
            csv_config=CsvColumnConfig(date_col="Transaction Date", amount_col="Amount", merchant_col="Description"),
        ),
        ParserPlugin(
            source_key="example_charge_card",
            label="Example Charge Card",
            record_type="transactions",
            allowed_kinds={"csv", "statement_pdf"},
            domain="financial_accounts",
            directory_name="example_charge_card",
            group="Credit Cards",
            is_credit_card=True,
            csv_config=CsvColumnConfig(date_col="Date", amount_col="Amount", merchant_col="Description", negate_amount=True),
        ),
        ParserPlugin(
            source_key="example_credit_card",
            label="Example Credit Card",
            record_type="transactions",
            allowed_kinds={"csv", "statement_pdf"},
            domain="financial_accounts",
            directory_name="example_credit_card",
            group="Credit Cards",
            is_credit_card=True,
            csv_config=CsvColumnConfig(date_col="Trans. Date", amount_col="Amount", merchant_col="Description"),
        ),
        ParserPlugin(
            source_key="example_device_card",
            label="Example Device Card",
            record_type="transactions",
            allowed_kinds={"csv", "statement_pdf"},
            domain="financial_accounts",
            directory_name="example_device_card",
            group="Credit Cards",
            is_credit_card=True,
            csv_config=CsvColumnConfig(date_col="Transaction Date", amount_col="Amount (USD)", merchant_col="Description"),
        ),
    ]

    for plugin in test_plugins:
        if plugin.source_key not in existing_keys:
            _registry.append(plugin)
            existing_keys.add(plugin.source_key)


_ensure_test_plugins_registered()


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    # Seed identity rates so SQL JOINs with exchange_rates work
    from src.models.transaction import ExchangeRate, generate_uuid
    session = Session()
    for currency in ["USD", "INR", "EUR", "GBP", "CAD", "AUD", "JPY"]:
        session.add(ExchangeRate(
            id=generate_uuid(),
            date=date.today(),
            from_currency=currency,
            to_currency=currency,
            rate=1.0,
        ))
    session.commit()
    session.close()

    yield Session, db_path

    os.unlink(db_path)


@pytest.fixture
def db_session(temp_db):
    """Provide a database session for tests."""
    Session, _ = temp_db
    session = Session()
    yield session
    session.close()


@pytest.fixture
def sample_transactions(db_session):
    """Create sample transactions for testing."""
    txns = [
        Transaction(
            source_id="txn1", source="example_card", date=date(2026, 2, 1),
            amount=Decimal("-50.00"), merchant_raw="EXAMPLE GROCER",
            merchant_clean="Example Grocer", category="Groceries", category_source="rule"
        ),
        Transaction(
            source_id="txn2", source="example_card", date=date(2026, 2, 2),
            amount=Decimal("-25.00"), merchant_raw="EXAMPLE DELIVERY",
            merchant_clean="Example Delivery", category="Dining", category_source="rule"
        ),
        Transaction(
            source_id="txn3", source="example_charge_card", date=date(2026, 2, 3),
            amount=Decimal("-15.99"), merchant_raw="EXAMPLE STREAM",
            merchant_clean="Example Stream", category="Subscriptions", category_source="rule"
        ),
        Transaction(
            source_id="txn4", source="example_bank", date=date(2026, 2, 1),
            amount=Decimal("5000.00"), merchant_raw="PAYROLL DIRECT DEP",
            merchant_clean="Payroll", category="Salary/Paycheck", category_source="rule"
        ),
        Transaction(
            source_id="txn5", source="example_card", date=date(2026, 1, 1),
            amount=Decimal("-15.99"), merchant_raw="EXAMPLE STREAM",
            merchant_clean="Example Stream", category="Subscriptions", category_source="rule"
        ),
        Transaction(
            source_id="txn6", source="example_card", date=date(2025, 12, 1),
            amount=Decimal("-15.99"), merchant_raw="EXAMPLE STREAM",
            merchant_clean="Example Stream", category="Subscriptions", category_source="rule"
        ),
    ]
    for t in txns:
        db_session.add(t)
    db_session.commit()
    return txns


@pytest.fixture
def temp_csv_file():
    """Create a temporary CSV file for import testing."""
    content = """Transaction Date,Amount,Description
02/01/2026,-45.00,EXAMPLE GROCER #123
02/02/2026,-32.50,EXAMPLE DELIVERY
02/03/2026,100.00,EXAMPLE REFUND
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write(content)
        path = f.name
    yield Path(path)
    os.unlink(path)


@pytest.fixture
def temp_rules_file():
    """Create a temporary rules YAML file."""
    content = """rules:
  - pattern: "WHOLEFDS|EXAMPLE GROCER"
    category: "Groceries"
    merchant_clean: "Example Grocer"
  - pattern: "UBER.*EATS"
    category: "Dining"
  - pattern: "EXAMPLE STREAM"
    category: "Subscriptions"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(content)
        path = f.name
    yield Path(path)
    os.unlink(path)
