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
    # No natural key here -- `merchant` is not unique -- so identity is a minted uid.
    uid = Column(String)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    uid = Column(String)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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


class ConnectedAccount(Base):
    """One financial account exposed by a connection such as a Plaid Item."""

    __tablename__ = "connected_accounts"

    id = Column(String, primary_key=True, default=generate_uuid)
    sync_log_id = Column(String, ForeignKey("sync_log.id"), nullable=False)
    source_key = Column(String, nullable=False)
    source = Column(String, nullable=False)
    external_account_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    mask = Column(String(4))
    account_type = Column(String)
    subtype = Column(String)
    account_group = Column(String, nullable=False)
    detail_view = Column(String)
    currency = Column(String(3), default="USD", nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_connected_account_external", "sync_log_id", "external_account_id", unique=True),
        Index("ix_connected_account_source", "source_key"),
    )


class Balance(Base):
    __tablename__ = "balances"

    id = Column(String, primary_key=True, default=generate_uuid)
    source = Column(String, nullable=False)  # Institution or imported-source label.
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
    account_key = Column(String)
    account_name = Column(String)
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
    account_key = Column(String)
    account_name = Column(String)
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


class InvestmentPeriodFact(Base):
    """Persisted investment movement between two observed balance snapshots."""

    __tablename__ = "investment_period_facts"

    id = Column(String, primary_key=True, default=generate_uuid)
    source_key = Column(String)
    source = Column(String, nullable=False)
    period_start = Column(Date)
    period_end = Column(Date, nullable=False)
    _beginning_value = Column("beginning_value", Numeric(12, 2))
    _ending_value = Column("ending_value", Numeric(12, 2), nullable=False)
    _inflow = Column("inflow", Numeric(12, 2), nullable=False, default=0)
    _market_gain = Column("market_gain", Numeric(12, 2), nullable=False, default=0)
    currency = Column(String(3), nullable=False, default="USD")
    provenance = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_investment_period_source_end", "source", "period_end", unique=True),
        Index("ix_investment_period_source_key_end", "source_key", "period_end"),
    )


class AccountActivity(Base):
    """Provider activity that belongs to an account detail view, not the household ledger."""

    __tablename__ = "account_activity"

    id = Column(String, primary_key=True, default=generate_uuid)
    source_id = Column(String, nullable=False)
    source_key = Column(String)
    source = Column(String, nullable=False)
    account_id = Column(String)
    account_last4 = Column(String(4))
    date = Column(Date, nullable=False)
    authorized_date = Column(Date)
    _amount = Column("amount", Numeric(12, 2), nullable=False)
    description = Column(String, nullable=False)
    merchant = Column(String)
    activity_type = Column(String, nullable=False, default="other")
    currency = Column(String(3), default="USD", nullable=False)
    pending = Column(Boolean, default=False, nullable=False)
    pending_activity_id = Column(String)
    raw_data = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    @property
    def amount(self):
        raise AttributeError("Direct access to AccountActivity.amount is forbidden. Use _amount with a rate JOIN.")

    @amount.setter
    def amount(self, value):
        self._amount = value

    __table_args__ = (
        Index("ix_account_activity_source_id", "source", "source_id", unique=True),
        Index("ix_account_activity_source_date", "source", "date"),
        Index("ix_account_activity_source_key_date", "source_key", "date"),
    )


class PlaidApiUsage(Base):
    __tablename__ = "plaid_api_usage"

    id = Column(String, primary_key=True, default=generate_uuid)
    endpoint = Column(String, nullable=False)
    institution = Column(String)
    plaid_item_id = Column(String)
    units = Column(Numeric(10, 2), nullable=False, default=1)
    estimated_cost = Column(Numeric(10, 4), nullable=False, default=0)
    status = Column(String, nullable=False, default="attempted")
    metadata_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_plaid_api_usage_created_at", "created_at"),
        Index("ix_plaid_api_usage_endpoint_created_at", "endpoint", "created_at"),
        Index("ix_plaid_api_usage_item_created_at", "plaid_item_id", "created_at"),
    )


class PlaidProductEnrollment(Base):
    __tablename__ = "plaid_product_enrollments"

    id = Column(String, primary_key=True, default=generate_uuid)
    plaid_item_id = Column(String, nullable=False)
    product = Column(String, nullable=False)
    active_from = Column(DateTime, nullable=False, default=datetime.utcnow)
    active_until = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_plaid_product_item_product", "plaid_item_id", "product", unique=True),
        Index("ix_plaid_product_active", "product", "active_from", "active_until"),
    )


class AppMetadata(Base):
    __tablename__ = "app_metadata"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Tombstone(Base):
    """A record that something was deleted, so the deletion can travel between devices.

    Without these a delete does not survive a merge: the other device still has the row, sends it
    back, and it reappears -- which reads as "delete does not work". Applied *before* everything
    else in a merge, so an incoming edit cannot resurrect a row this device has deleted. That makes
    delete win over a concurrent edit regardless of timestamps, which is the safer direction: a
    resurrected transaction is a wrong balance, while a lost edit is a re-typed note.

    `ref` is the *natural* identity of what was deleted, not a local primary key -- a project name,
    or `source|source_id` for a transaction -- because local ids differ per device. See
    docs/multi_device_sync.md §4.
    """

    __tablename__ = "tombstones"

    kind = Column(String, primary_key=True)
    ref = Column(String, primary_key=True)
    deleted_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Which device deleted it. Diagnostic only: merge behaviour must never depend on this, or two
    # devices would resolve the same tombstone differently.
    device_id = Column(String)

    __table_args__ = (Index("ix_tombstone_deleted_at", "deleted_at"),)


class SyncDevice(Base):
    """Devices participating in this vault, and when each was last heard from.

    Persisted rather than derived from whoever published recently, because a row written months ago
    by a device that is currently offline still needs a name to display.
    """

    __tablename__ = "sync_devices"

    device_id = Column(String, primary_key=True)
    label = Column(String)
    platform = Column(String)  # macos, container, ios
    last_seen_at = Column(DateTime)
    # High-water mark: the newest `updated_at` already merged from this peer. Timeslice omits this
    # and consequently re-downloads every peer's entire history on every poll, forever.
    last_merged_watermark = Column(DateTime)


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
    # One Splitwise group per project, cached after the first commit.
    splitwise_group_id = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Multi-device identity. `name` is the merge key across devices because it is already unique,
    # but a rename changes it -- `uid` is what follows this project *through* a rename. Nullable:
    # existing rows are populated by the `backfill-sync-identity` CLI, never on startup.
    # See docs/multi_device_sync.md.
    uid = Column(String)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TransactionProject(Base):
    __tablename__ = "transaction_projects"

    transaction_id = Column(String, ForeignKey("transactions.id"), primary_key=True)
    project_id = Column(String, ForeignKey("projects.id"), primary_key=True)
    description = Column(String)
    # No uid: across devices this row is identified by (transaction natural key, project name),
    # because its own primary key is a composite of per-device UUIDs. `description` is user input,
    # so it needs a timestamp to order competing edits.
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False, unique=True)
    color = Column(String, default="#6366f1")
    # The account owner. Splitwise needs a payer, and transactions here are implicitly
    # paid by whoever owns the cards. Exactly one contact should carry this flag; it also
    # avoids hardcoding a personal name anywhere in the app.
    is_self = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    # As for Project: merged on the unique `name`, tracked through renames by `uid`.
    uid = Column(String)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProjectMember(Base):
    __tablename__ = "project_members"

    project_id = Column(String, ForeignKey("projects.id"), primary_key=True)
    contact_id = Column(String, ForeignKey("contacts.id"), primary_key=True)
    # Identified across devices by (project name, contact name).
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TransactionSplit(Base):
    """Who owes what on a transaction. Not scoped to a project.

    A split is a fact about the expense: the same $40 dinner cannot be 50/50 in one grouping
    and 70/30 in another, because there is only one debt. Projects are a grouping, so keying
    splits by project let the same transaction carry contradictory splits, and made a project
    delete destroy split data that never belonged to it.

    Superseded `transaction_project_splits`, which is kept for now as the migration source.
    """

    __tablename__ = "transaction_splits"

    transaction_id = Column(String, primary_key=True)
    contact_id = Column(String, ForeignKey("contacts.id"), primary_key=True)
    # NULL means "split equally". Stored in the transaction's own currency, like
    # Transaction._amount, so it must be rate-converted on the way out.
    _share_amount = Column("share_amount", Numeric(10, 2))

    @property
    def share_amount(self):
        """Block direct access. Use _share_amount with a rate JOIN / rate map."""
        raise AttributeError(
            "Direct access to TransactionSplit.share_amount is forbidden. "
            "Use _share_amount with currency conversion."
        )

    @share_amount.setter
    def share_amount(self, value):
        self._share_amount = value

    # Identified across devices by (transaction natural key, contact name). The share is user
    # input, so competing edits are ordered by this.
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TransactionProjectSplit(Base):
    __tablename__ = "transaction_project_splits"

    transaction_id = Column(String, primary_key=True)
    project_id = Column(String, primary_key=True)
    contact_id = Column(String, ForeignKey("contacts.id"), primary_key=True)
    # NULL means "split this transaction equally", which is what every row meant before
    # unequal splits existed. Stored in the transaction's own currency, like
    # Transaction._amount, so it must be rate-converted on the way out.
    _share_amount = Column("share_amount", Numeric(10, 2))

    @property
    def share_amount(self):
        """Block direct access. Use _share_amount with a rate JOIN / rate map."""
        raise AttributeError(
            "Direct access to TransactionProjectSplit.share_amount is forbidden. "
            "Use _share_amount with currency conversion."
        )

    @share_amount.setter
    def share_amount(self, value):
        self._share_amount = value

    # Identified by (transaction natural key, project name, contact name).
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ContactSplitwiseLink(Base):
    """Optional mapping from a local contact to a Splitwise user."""
    __tablename__ = "contact_splitwise_links"

    contact_id = Column(String, ForeignKey("contacts.id"), primary_key=True)
    splitwise_user_id = Column(String, nullable=False)
    display_name = Column(String)
    linked_at = Column(DateTime, default=datetime.utcnow)
    # Identified by (contact name); the Splitwise user id is the value, not the identity.
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SplitwiseCommit(Base):
    """One row per (transaction, project) pushed to Splitwise.

    Splitwise creates one expense per API call, so a project is drained in batches. This
    table is the queue: `state` tracks progress so pressing Sync repeatedly picks up where
    the last batch stopped, and `expense_id` lets an amend update the existing expense
    instead of creating a duplicate.
    """
    __tablename__ = "splitwise_commits"

    transaction_id = Column(String, primary_key=True)
    project_id = Column(String, primary_key=True)
    state = Column(String, nullable=False, default="pending")  # pending | committed | failed
    expense_id = Column(String)
    error = Column(String)
    # Detects a share edit after a commit, so an amend is only offered when it is needed.
    committed_fingerprint = Column(String)
    attempted_at = Column(DateTime)
    committed_at = Column(DateTime)

    __table_args__ = (
        Index("ix_splitwise_commit_project_state", "project_id", "state"),
    )


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
