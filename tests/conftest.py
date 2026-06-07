"""Shared test fixtures and configuration."""
import pytest
import tempfile
import os
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
            source_key="transamerica",
            label="National Retirement Plan",
            record_type="retirement",
            allowed_kinds={"retirement_csv", "retirement_statement_pdf"},
            domain="retirement",
            directory_name="transamerica_401k",
            group="Retirement",
            hint="Upload retirement CSV or statement",
        ),
        ParserPlugin(
            source_key="bofa",
            label="Bank of America",
            record_type="transactions",
            allowed_kinds={"csv", "statement_pdf"},
            domain="financial_accounts",
            directory_name="bofa",
            group="Bank Accounts",
            is_credit_card=False,
            csv_config=CsvColumnConfig(date_col="Date", amount_col="Amount", merchant_col="Description"),
        ),
        ParserPlugin(
            source_key="chase",
            label="Chase",
            record_type="transactions",
            allowed_kinds={"csv", "statement_pdf"},
            domain="financial_accounts",
            directory_name="chase",
            group="Credit Cards",
            is_credit_card=True,
            csv_config=CsvColumnConfig(date_col="Transaction Date", amount_col="Amount", merchant_col="Description"),
        ),
        ParserPlugin(
            source_key="amex",
            label="American Express",
            record_type="transactions",
            allowed_kinds={"csv", "statement_pdf"},
            domain="financial_accounts",
            directory_name="amex",
            group="Credit Cards",
            is_credit_card=True,
            csv_config=CsvColumnConfig(date_col="Date", amount_col="Amount", merchant_col="Description", negate_amount=True),
        ),
        ParserPlugin(
            source_key="discover",
            label="Discover",
            record_type="transactions",
            allowed_kinds={"csv", "statement_pdf"},
            domain="financial_accounts",
            directory_name="discover",
            group="Credit Cards",
            is_credit_card=True,
            csv_config=CsvColumnConfig(date_col="Trans. Date", amount_col="Amount", merchant_col="Description"),
        ),
        ParserPlugin(
            source_key="apple",
            label="Apple Card",
            record_type="transactions",
            allowed_kinds={"csv", "statement_pdf"},
            domain="financial_accounts",
            directory_name="apple",
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
            source_id="txn1", source="chase", date=date(2026, 2, 1),
            amount=Decimal("-50.00"), merchant_raw="WHOLE FOODS",
            merchant_clean="Whole Foods", category="Groceries", category_source="rule"
        ),
        Transaction(
            source_id="txn2", source="chase", date=date(2026, 2, 2),
            amount=Decimal("-25.00"), merchant_raw="UBER EATS",
            merchant_clean="Uber Eats", category="Dining", category_source="rule"
        ),
        Transaction(
            source_id="txn3", source="amex", date=date(2026, 2, 3),
            amount=Decimal("-15.99"), merchant_raw="NETFLIX",
            merchant_clean="Netflix", category="Subscriptions", category_source="rule"
        ),
        Transaction(
            source_id="txn4", source="bofa", date=date(2026, 2, 1),
            amount=Decimal("5000.00"), merchant_raw="PAYROLL DIRECT DEP",
            merchant_clean="Payroll", category="Salary/Paycheck", category_source="rule"
        ),
        Transaction(
            source_id="txn5", source="chase", date=date(2026, 1, 1),
            amount=Decimal("-15.99"), merchant_raw="NETFLIX",
            merchant_clean="Netflix", category="Subscriptions", category_source="rule"
        ),
        Transaction(
            source_id="txn6", source="chase", date=date(2025, 12, 1),
            amount=Decimal("-15.99"), merchant_raw="NETFLIX",
            merchant_clean="Netflix", category="Subscriptions", category_source="rule"
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
02/01/2026,-45.00,WHOLEFDS MKT #123
02/02/2026,-32.50,UBER *EATS
02/03/2026,100.00,REFUND AMAZON
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
  - pattern: "WHOLEFDS|WHOLE FOODS"
    category: "Groceries"
    merchant_clean: "Whole Foods"
  - pattern: "UBER.*EATS"
    category: "Dining"
  - pattern: "NETFLIX"
    category: "Subscriptions"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(content)
        path = f.name
    yield Path(path)
    os.unlink(path)
