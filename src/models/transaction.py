from sqlalchemy import Column, String, Date, DateTime, Numeric, Boolean, Index, ForeignKey
from sqlalchemy.dialects.sqlite import JSON
from datetime import datetime
import uuid

from src.models.database import Base


def generate_uuid():
    return str(uuid.uuid4())


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"

    id = Column(String, primary_key=True, default=generate_uuid)
    date = Column(Date, nullable=False)
    from_currency = Column(String(3), nullable=False)
    to_currency = Column(String(3), nullable=False)
    rate = Column(Numeric(12, 6), nullable=False)

    __table_args__ = (
        Index("ix_rate_lookup", "date", "from_currency", "to_currency", unique=True),
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String, primary_key=True, default=generate_uuid)
    source_id = Column(String, nullable=False)
    source = Column(String, nullable=False)  # institution name from Plaid or import source
    account_last4 = Column(String(4))
    plaid_account_id = Column(String)
    plaid_mask = Column(String)
    date = Column(Date, nullable=False)
    authorized_date = Column(Date)
    _amount = Column("amount", Numeric(10, 2), nullable=False)
    merchant_raw = Column(String, nullable=False)
    merchant_clean = Column(String)
    original_description = Column(String)
    payment_channel = Column(String)
    category = Column(String)
    category_source = Column(String)  # rule, llm, user
    is_recurring = Column(Boolean, default=False)
    tags = Column(JSON, default=list)
    notes = Column(String)
    origin = Column(String, default="statements")
    currency = Column(String(3), default="USD", nullable=False)
    pending = Column(Boolean, default=False, nullable=False)
    pending_transaction_id = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def amount(self):
        """Block direct access to amount. Use Transaction._amount in rate-joined queries."""
        raise AttributeError(
            "Direct access to Transaction.amount is forbidden. "
            "Use Transaction._amount with a rate JOIN (amount * rate) for all financial queries. "
            "See src/services/exchange_rates.py:latest_rate_subquery()"
        )

    @amount.setter
    def amount(self, value):
        """Allow setting amount during creation/import."""
        self._amount = value

    __table_args__ = (
        Index("ix_source_source_id", "source", "source_id", unique=True),
        Index("ix_date", "date"),
        Index("ix_category", "category"),
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(String, primary_key=True, default=generate_uuid)
    merchant = Column(String, nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    frequency = Column(String)  # weekly, monthly, quarterly, annual
    source = Column(String)
    account_last4 = Column(String(4))
    last_charged = Column(Date)
    next_expected = Column(Date)
    status = Column(String, default="active")  # active, cancelled, paused
    price_history = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)


class Rule(Base):
    __tablename__ = "rules"

    id = Column(String, primary_key=True, default=generate_uuid)
    pattern = Column(String, nullable=False)
    match_field = Column(String, default="merchant_raw")
    category = Column(String, nullable=False)
    merchant_clean = Column(String)
    source = Column(String, default="user")  # builtin, learned, user
    priority = Column(Numeric, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class SyncLog(Base):
    __tablename__ = "sync_log"

    id = Column(String, primary_key=True, default=generate_uuid)
    source = Column(String, nullable=False)
    sync_type = Column(String)  # plaid, csv
    file_hash = Column(String)
    record_count = Column(Numeric)
    status = Column(String)  # success, error, connected
    error_message = Column(String)
    # Plaid-specific
    plaid_item_id = Column(String)
    plaid_cursor = Column(String)
    extra_data = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)


class Balance(Base):
    __tablename__ = "balances"

    id = Column(String, primary_key=True, default=generate_uuid)
    source = Column(String, nullable=False)  # "Bank of America", "Chase", etc.
    date = Column(Date, nullable=False)
    balance = Column(Numeric(10, 2), nullable=False)
    balance_type = Column(String)  # "opening", "closing", "statement"
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_balance_source_date", "source", "date"),
    )


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id = Column(String, primary_key=True, default=generate_uuid)
    source = Column(String, nullable=False)
    account_group = Column(String, nullable=False)  # bank_account, credit_card, investment, retirement
    connection_state = Column(String, nullable=False)  # plaid, manual
    _current_value = Column("current_value", Numeric(12, 2), nullable=False)
    currency = Column(String(3), default="USD", nullable=False)
    synced_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def current_value(self):
        """Block direct access. Use AccountSnapshot._current_value with rate JOIN."""
        raise AttributeError(
            "Direct access to AccountSnapshot.current_value is forbidden. "
            "Use AccountSnapshot._current_value with a rate JOIN."
        )

    @current_value.setter
    def current_value(self, value):
        self._current_value = value

    __table_args__ = (
        Index("ix_account_snapshot_source_synced_at", "source", "synced_at"),
        Index("ix_account_snapshot_group_synced_at", "account_group", "synced_at"),
    )


class InvestmentHoldingSnapshot(Base):
    __tablename__ = "investment_holding_snapshots"

    id = Column(String, primary_key=True, default=generate_uuid)
    source = Column(String, nullable=False)
    plaid_account_id = Column(String)
    account_name = Column(String)
    security_id = Column(String)
    ticker = Column(String)
    name = Column(String)
    quantity = Column(Numeric(18, 6))
    price = Column(Numeric(18, 6))
    _value = Column("value", Numeric(18, 2), nullable=False)
    _cost_basis = Column("cost_basis", Numeric(18, 2))
    currency = Column(String(3), default="USD", nullable=False)
    type = Column(String)
    synced_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def value(self):
        raise AttributeError("Direct access to InvestmentHoldingSnapshot.value is forbidden. Use _value with rate JOIN.")

    @value.setter
    def value(self, val):
        self._value = val

    @property
    def cost_basis(self):
        raise AttributeError("Direct access to InvestmentHoldingSnapshot.cost_basis is forbidden. Use _cost_basis with rate JOIN.")

    @cost_basis.setter
    def cost_basis(self, val):
        self._cost_basis = val

    __table_args__ = (
        Index("ix_investment_holding_source_synced_at", "source", "synced_at"),
        Index("ix_investment_holding_security_synced_at", "security_id", "synced_at"),
        Index("ix_investment_holding_account_synced_at", "plaid_account_id", "synced_at"),
    )



class SourceBalanceHistory(Base):
    __tablename__ = "source_balance_history"

    id = Column(String, primary_key=True, default=generate_uuid)
    source_key = Column(String)
    source = Column(String, nullable=False)
    account_group = Column(String, nullable=False)
    date = Column(Date, nullable=False)
    _value = Column("value", Numeric(12, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    provenance = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def value(self):
        raise AttributeError("Direct access to SourceBalanceHistory.value is forbidden. Use _value with rate JOIN.")

    @value.setter
    def value(self, val):
        self._value = val

    __table_args__ = (
        Index("ix_source_balance_history_source_date", "source", "date"),
        Index("ix_source_balance_history_group_date", "account_group", "date"),
    )


class PlaidApiUsage(Base):
    __tablename__ = "plaid_api_usage"

    id = Column(String, primary_key=True, default=generate_uuid)
    endpoint = Column(String, nullable=False)
    institution = Column(String)
    plaid_item_id = Column(String)
    units = Column(Numeric(10, 2), nullable=False, default=1)
    estimated_cost = Column(Numeric(10, 4), nullable=False, default=0)
    metadata_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_plaid_api_usage_created_at", "created_at"),
        Index("ix_plaid_api_usage_endpoint_created_at", "endpoint", "created_at"),
        Index("ix_plaid_api_usage_item_created_at", "plaid_item_id", "created_at"),
    )


class Project(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False, unique=True)
    color = Column(String, default="#6366f1")
    start_date = Column(Date)
    end_date = Column(Date)
    budget = Column(Numeric(10, 2))
    status = Column(String, default="active")  # active, completed, archived
    notes = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class TransactionProject(Base):
    __tablename__ = "transaction_projects"

    transaction_id = Column(String, ForeignKey("transactions.id"), primary_key=True)
    project_id = Column(String, ForeignKey("projects.id"), primary_key=True)


class Payslip(Base):
    __tablename__ = "payslips"

    id = Column(String, primary_key=True, default=generate_uuid)
    source = Column(String, nullable=False)
    sync_log_id = Column(String)
    employer = Column(String, nullable=False)
    pay_date = Column(Date, nullable=False)
    pay_period_start = Column(Date)
    pay_period_end = Column(Date)
    currency = Column(String(3), default="USD", nullable=False)
    _gross = Column("gross", Numeric(10, 2), nullable=False)
    _net = Column("net", Numeric(10, 2), nullable=False)
    _total_taxes = Column("total_taxes", Numeric(10, 2), nullable=False, default=0)
    _total_deductions = Column("total_deductions", Numeric(10, 2), nullable=False, default=0)
    signature = Column(String(32), nullable=False)
    filename = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    @property
    def gross(self):
        raise AttributeError("Direct access to Payslip.gross is forbidden. Use Payslip._gross with a rate JOIN.")

    @gross.setter
    def gross(self, value):
        self._gross = value

    @property
    def net(self):
        raise AttributeError("Direct access to Payslip.net is forbidden. Use Payslip._net with a rate JOIN.")

    @net.setter
    def net(self, value):
        self._net = value

    @property
    def total_taxes(self):
        raise AttributeError("Direct access to Payslip.total_taxes is forbidden. Use Payslip._total_taxes with a rate JOIN.")

    @total_taxes.setter
    def total_taxes(self, value):
        self._total_taxes = value

    @property
    def total_deductions(self):
        raise AttributeError("Direct access to Payslip.total_deductions is forbidden. Use Payslip._total_deductions with a rate JOIN.")

    @total_deductions.setter
    def total_deductions(self, value):
        self._total_deductions = value

    __table_args__ = (
        Index("ix_payslip_signature", "source", "signature", unique=True),
        Index("ix_payslip_pay_date", "pay_date"),
    )


class PayslipLineItem(Base):
    __tablename__ = "payslip_line_items"

    id = Column(String, primary_key=True, default=generate_uuid)
    payslip_id = Column(String, nullable=False)
    section = Column(String, nullable=False)  # 'taxes', 'deductions', 'earnings'
    label = Column(String, nullable=False)
    _amount = Column("amount", Numeric(10, 2), nullable=False)
    _ytd = Column("ytd", Numeric(10, 2))
    created_at = Column(DateTime, default=datetime.utcnow)

    @property
    def amount(self):
        raise AttributeError("Direct access forbidden. Use _amount with rate JOIN.")

    @amount.setter
    def amount(self, value):
        self._amount = value

    __table_args__ = (Index("ix_payslip_line_item_payslip", "payslip_id"),)


class RetirementTransaction(Base):
    __tablename__ = "retirement_transactions"

    id = Column(String, primary_key=True, default=generate_uuid)
    source = Column(String, nullable=False)
    sync_log_id = Column(String)
    source_id = Column(String, nullable=False)
    date = Column(Date, nullable=False)
    type = Column(String, nullable=False)
    contribution_source = Column(String)
    fund = Column(String)
    currency = Column(String(3), default="USD", nullable=False)
    _amount = Column("amount", Numeric(10, 2), nullable=False)
    units = Column(Numeric(18, 6))
    unit_price = Column(Numeric(18, 6))
    created_at = Column(DateTime, default=datetime.utcnow)

    @property
    def amount(self):
        raise AttributeError("Direct access forbidden. Use _amount with rate JOIN.")

    @amount.setter
    def amount(self, value):
        self._amount = value

    __table_args__ = (
        Index("ix_retirement_txn_source_id", "source", "source_id", unique=True),
        Index("ix_retirement_txn_date", "date"),
    )


class RetirementStatement(Base):
    __tablename__ = "retirement_statements"

    id = Column(String, primary_key=True, default=generate_uuid)
    source = Column(String, nullable=False)
    sync_log_id = Column(String)
    plan_name = Column(String)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    currency = Column(String(3), default="USD", nullable=False)
    _beginning_balance = Column("beginning_balance", Numeric(12, 2), nullable=False)
    _ending_balance = Column("ending_balance", Numeric(12, 2), nullable=False)
    _employee_contributions = Column("employee_contributions", Numeric(10, 2), nullable=False, default=0)
    _employer_contributions = Column("employer_contributions", Numeric(10, 2), nullable=False, default=0)
    _market_change = Column("market_change", Numeric(10, 2), nullable=False, default=0)
    _vested_balance = Column("vested_balance", Numeric(12, 2))
    rate_of_return = Column(Numeric(8, 4))  # percentage, not money
    created_at = Column(DateTime, default=datetime.utcnow)

    @property
    def ending_balance(self):
        raise AttributeError("Direct access forbidden. Use _ending_balance with rate JOIN.")

    @ending_balance.setter
    def ending_balance(self, value):
        self._ending_balance = value

    @property
    def beginning_balance(self):
        raise AttributeError("Direct access forbidden. Use _beginning_balance with rate JOIN.")

    @beginning_balance.setter
    def beginning_balance(self, value):
        self._beginning_balance = value

    @property
    def employee_contributions(self):
        raise AttributeError("Direct access forbidden. Use _employee_contributions with rate JOIN.")

    @employee_contributions.setter
    def employee_contributions(self, value):
        self._employee_contributions = value

    @property
    def employer_contributions(self):
        raise AttributeError("Direct access forbidden. Use _employer_contributions with rate JOIN.")

    @employer_contributions.setter
    def employer_contributions(self, value):
        self._employer_contributions = value

    @property
    def market_change(self):
        raise AttributeError("Direct access forbidden. Use _market_change with rate JOIN.")

    @market_change.setter
    def market_change(self, value):
        self._market_change = value

    @property
    def vested_balance(self):
        raise AttributeError("Direct access forbidden. Use _vested_balance with rate JOIN.")

    @vested_balance.setter
    def vested_balance(self, value):
        self._vested_balance = value

    __table_args__ = (
        Index("ix_retirement_stmt_source_period", "source", "period_end", unique=True),
    )
