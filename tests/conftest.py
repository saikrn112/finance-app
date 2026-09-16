"""Shared test fixtures and configuration."""
import pytest
import tempfile
import os

# Must be set before anything imports the app: the auto-task worker starts a Plaid sync as
# soon as the app starts up and holds the shared job lock while it runs, which races the
# requests the tests make.
os.environ.setdefault("FINANCE_APP_DISABLE_AUTO_TASKS", "1")
from pathlib import Path
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base
from src.models.transaction import Transaction, Subscription, Rule, SyncLog
from src.plugins.base import ParserPlugin, CsvColumnConfig


def _example_retirement_csv_parser(file_path):
    """Parse the synthetic retirement CSV used by the import tests.

    Core has no generic retirement parser on purpose — provider parsing lives in the private
    plugin repo — so the test plugin brings its own. These tests are about import_service
    (preview, duplicate summary, commit, payload persistence), not about parsing.
    """
    import csv as _csv
    import hashlib as _hashlib
    from datetime import datetime as _dt

    from src.processing.retirement_utils import summarize_retirement_transactions

    transactions = []
    with open(file_path, newline="") as handle:
        for row in _csv.DictReader(handle):
            if not row.get("Date"):
                continue
            entry = {
                "date": _dt.strptime(row["Date"].strip(), "%m/%d/%y").date().isoformat(),
                "type": (row.get("Transaction Type") or "").strip(),
                "source": (row.get("Source") or "").strip(),
                "fund": (row.get("Fund Name") or "").strip(),
                "units": float(row["Unit Count"]),
                "unit_price": float(row["Unit Value"]),
                "amount": float(row["Transaction Amount"]),
            }
            # Retirement dedupe keys on source_id, so a parser has to supply a stable one.
            entry["source_id"] = _hashlib.sha1(
                "|".join(str(entry[k]) for k in
                         ("date", "type", "source", "fund", "units", "amount")).encode()
            ).hexdigest()[:24]
            transactions.append(entry)
    return {
        "transactions": transactions,
        "summary": summarize_retirement_transactions(transactions),
    }


def _ensure_test_plugins_registered():
    """Register minimal test plugins so import tests work without personal plugins."""
    # Go through the module, not `from ... import _registry`: load_plugins() rebinds the
    # module-level _registry to a fresh list, so a name imported beforehand points at an
    # orphaned list and every append below silently disappeared.
    from src.plugins import loader

    # Ensure plugins are loaded first (picks up any installed plugins)
    loader.load_plugins()
    registry = loader.get_registry()

    # Only add test plugins if their source_key isn't already registered
    existing_keys = {p.source_key for p in registry}

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
            retirement_parser=_example_retirement_csv_parser,
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
            # Device-card exports list purchases as positive amounts under a Merchant column.
            csv_config=CsvColumnConfig(date_col="Transaction Date", amount_col="Amount (USD)", merchant_col="Merchant", negate_amount=True),
        ),
    ]

    for plugin in test_plugins:
        if plugin.source_key not in existing_keys:
            registry.append(plugin)
            existing_keys.add(plugin.source_key)


_ensure_test_plugins_registered()


@pytest.fixture(autouse=True)
def _isolate_runtime_dir(tmp_path_factory):
    """Point runtime_dir at a temp directory for every test.

    Anything that writes beside the database -- the sync device id, vault metadata, the import
    workspace -- resolves through settings.app.runtime_dir. Without this, tests that exercise
    sync minted a real `device-id` into data/runtime/prod, stamped with the *host* platform, so
    the container app would have identified itself as a Mac. Tests must not write into
    production data at all.
    """
    from src.config import settings

    original = settings.app.runtime_dir
    settings.app.runtime_dir = str(tmp_path_factory.mktemp("runtime"))
    try:
        yield
    finally:
        settings.app.runtime_dir = original


@pytest.fixture(autouse=True)
def _isolate_finance_env():
    """Undo FINANCE_APP_* env changes a test makes.

    The CLI's serve/demo commands set FINANCE_APP_DB_PATH and friends in os.environ, and
    Settings.load() treats those as overrides. Without this, running the CLI tests before the
    config tests made the config tests fail — order-dependent failures that looked like
    config bugs.
    """
    before = {k: v for k, v in os.environ.items() if k.startswith("FINANCE_APP_")}
    yield
    for key in [k for k in os.environ if k.startswith("FINANCE_APP_")]:
        if key not in before:
            del os.environ[key]
    for key, value in before.items():
        os.environ[key] = value


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
    """Create sample transactions for testing.

    Dates are relative to today on purpose. These were hardcoded to Feb 2026, and once that
    drifted into the past the recurring detector stopped calling the subscription "active",
    so three tests failed for reasons that had nothing to do with the code under test.
    """
    today = date.today()
    # Three charges one month apart, the most recent within the active window.
    stream_dates = [today - timedelta(days=30 * n) for n in (1, 2, 3)]
    txns = [
        Transaction(
            source_id="txn1", source="example_card", date=today - timedelta(days=5),
            amount=Decimal("-50.00"), merchant_raw="EXAMPLE GROCER",
            merchant_clean="Example Grocer", category="Groceries", category_source="rule"
        ),
        Transaction(
            source_id="txn2", source="example_card", date=today - timedelta(days=4),
            amount=Decimal("-25.00"), merchant_raw="EXAMPLE DELIVERY",
            merchant_clean="Example Delivery", category="Dining", category_source="rule"
        ),
        Transaction(
            source_id="txn3", source="example_charge_card", date=stream_dates[0],
            amount=Decimal("-15.99"), merchant_raw="EXAMPLE STREAM",
            merchant_clean="Example Stream", category="Subscriptions", category_source="rule"
        ),
        Transaction(
            source_id="txn4", source="example_bank", date=today - timedelta(days=5),
            amount=Decimal("5000.00"), merchant_raw="PAYROLL DIRECT DEP",
            merchant_clean="Payroll", category="Salary/Paycheck", category_source="rule"
        ),
        Transaction(
            source_id="txn5", source="example_card", date=stream_dates[1],
            amount=Decimal("-15.99"), merchant_raw="EXAMPLE STREAM",
            merchant_clean="Example Stream", category="Subscriptions", category_source="rule"
        ),
        Transaction(
            source_id="txn6", source="example_card", date=stream_dates[2],
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
  - pattern: "EXAMPLE DELIVERY"
    category: "Dining"
  - pattern: "EXAMPLE STREAM"
    category: "Subscriptions"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(content)
        path = f.name
    yield Path(path)
    os.unlink(path)
