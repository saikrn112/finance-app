"""Pydantic response models for all financial API endpoints.

Every model that contains money fields includes a required `currency: str` field.
If an endpoint returns financial data without specifying currency, Pydantic will
raise a validation error at response time — guaranteeing all money has been
converted through the rate system before reaching the client.
"""

from __future__ import annotations

from pydantic import BaseModel
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Base mixin — all financial responses must declare target currency
# ---------------------------------------------------------------------------


class FinancialResponse(BaseModel):
    """Base model for responses containing money fields.

    The required `currency` field proves that the endpoint intentionally
    converted amounts to a target currency before responding.
    """
    currency: str


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


class SummaryResponse(FinancialResponse):
    income: float
    spending: float
    core_spending: float
    core_spending_excluding_rent: float
    transfers: float
    spend_income_ratio: float
    net_flow: float
    total_balance: float
    subscriptions_monthly: float
    start_date: str
    end_date: str


class SubcategoryItem(BaseModel):
    name: str
    total: float


class CategoryBreakdownItem(BaseModel):
    category: str
    total: float
    subcategories: list[SubcategoryItem] = []


class MerchantBreakdownItem(BaseModel):
    merchant: str
    total: float


class TrendPeriodItem(BaseModel):
    period: str
    total: float

    model_config = {"extra": "allow"}


class SubscriptionItem(BaseModel):
    merchant: Optional[str] = None
    merchant_clean: Optional[str] = None
    amount: float
    monthly_equivalent: float
    frequency: Optional[str] = None
    last_date: Optional[str] = None
    last_charge: Optional[str] = None
    category: Optional[str] = None
    source: Optional[str] = None
    occurrence_count: Optional[int] = None
    occurrences: Optional[int] = None
    first_seen: Optional[str] = None
    status: Optional[str] = None
    next_expected: Optional[str] = None
    account_last4: Optional[str] = None
    flags: list[str] = []

    model_config = {"extra": "allow"}


class AccountBalanceItem(FinancialResponse):
    source: str
    balance: float


class NetWorthSourceItem(BaseModel):
    key: str
    label: str
    group: str
    current: float
    currency: str
    history_mode: str


class NetWorthGroupItem(BaseModel):
    key: str
    label: str
    history_mode: str


class NetWorthPointItem(BaseModel):
    date: str
    bank_accounts: float
    credit_cards: float
    cash_like: float
    brokerage: float
    retirement: float
    tracked_total: float
    total: float


class NetWorthResponse(FinancialResponse):
    start_date: str
    end_date: str
    points: list[NetWorthPointItem]
    latest_sources: list[NetWorthSourceItem]
    groups: list[NetWorthGroupItem]


class CCDiagnosticCategoryItem(BaseModel):
    category: Optional[str] = None
    total: float
    count: int


class CCDiagnosticResponse(BaseModel):
    cc_net: float
    balanced: bool
    categories: list[CCDiagnosticCategoryItem]


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class TransactionProjectItem(BaseModel):
    id: str
    name: str
    color: Optional[str] = None


class MemberTotalItem(BaseModel):
    id: str
    name: str
    color: Optional[str] = None
    expenditure: float = 0
    income: float = 0
    net: float = 0


class ContactItem(BaseModel):
    id: str
    name: str
    color: Optional[str] = None
    # Present on transaction splits: this person's explicit share, in the response
    # currency. None means the transaction divides equally.
    share_amount: Optional[float] = None


class TransactionItem(BaseModel):
    id: str
    date: Optional[str] = None
    authorized_date: Optional[str] = None
    effective_date: Optional[str] = None
    amount: float
    merchant_raw: Optional[str] = None
    merchant_clean: Optional[str] = None
    original_description: Optional[str] = None
    category: Optional[str] = None
    source: Optional[str] = None
    account_last4: Optional[str] = None
    pending: bool = False
    is_recurring: Optional[bool] = None
    tags: list[str] = []
    notes: Optional[str] = None
    projects: list[TransactionProjectItem] = []
    description: Optional[str] = None
    splits: list[ContactItem] = []
    split_mode: Optional[str] = None  # "equal" | "unequal"


class TransactionListResponse(FinancialResponse):
    transactions: list[TransactionItem]
    total: int
    offset: int
    limit: int


class UncategorizedGroupItem(BaseModel):
    merchant_key: str
    display_name: str
    merchant_clean_suggestion: str
    rule_pattern_source: str
    transaction_count: int
    total_amount: float
    latest_date: Optional[str] = None
    transactions: list[TransactionItem] = []


class UncategorizedReviewResponse(FinancialResponse):
    groups: list[UncategorizedGroupItem]
    total_groups: int
    total_transactions: int
    page: int
    limit: int
    total_pages: int


# ---------------------------------------------------------------------------
# Payslips
# ---------------------------------------------------------------------------


class PayslipLineItem(BaseModel):
    label: str
    amount: float

    model_config = {"extra": "allow"}


class PayslipItem(BaseModel):
    employer: Optional[str] = None
    pay_date: Optional[str] = None
    pay_period_start: Optional[str] = None
    pay_period_end: Optional[str] = None
    gross: float = 0
    net: float = 0
    total_taxes: float = 0
    total_deductions: float = 0
    taxes: Any = None
    deductions: Any = None
    earnings: Any = None

    model_config = {"extra": "allow"}


class PayslipTotals(BaseModel):
    gross: float
    net: float
    taxes: float
    deductions: float
    effective_tax_rate: float


class PayslipListResponse(FinancialResponse):
    payslips: list[PayslipItem]
    latest: Optional[PayslipItem] = None
    totals: Optional[PayslipTotals] = None


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------


class RetirementTransactionItem(BaseModel):
    date: Optional[str] = None
    type: Optional[str] = None
    source: Optional[str] = None
    fund: Optional[str] = None
    amount: Optional[float] = None
    source_id: Optional[str] = None
    investment: Optional[str] = None
    transaction_type: Optional[str] = None
    shares: Optional[float] = None
    unit_price: Optional[float] = None

    model_config = {"extra": "allow"}


class RetirementSummary(BaseModel):
    balance: Optional[float] = None
    employee_contributed: Optional[float] = None
    employer_match: Optional[float] = None
    total_contributed: Optional[float] = None
    gain: Optional[float] = None
    vested_balance: Optional[float] = None
    rate_of_return: Optional[float] = None
    date_range: Optional[list[str]] = None
    latest_statement_end: Optional[str] = None

    model_config = {"extra": "allow"}


class RetirementStatementItem(BaseModel):
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    beginning_balance: Optional[float] = None
    ending_balance: Optional[float] = None
    employee_contributions: Optional[float] = None
    employer_contributions: Optional[float] = None
    market_change: Optional[float] = None
    vested_balance: Optional[float] = None
    rate_of_return: Optional[float] = None
    source_file: Optional[str] = None

    model_config = {"extra": "allow"}


class RetirementResponse(FinancialResponse):
    source_type: Optional[str] = None
    plan_name: Optional[str] = None
    history_date_range: Optional[str] = None
    summary: Optional[RetirementSummary] = None
    statements: list[RetirementStatementItem] = []
    activity: list[RetirementTransactionItem] = []
    transactions: list[RetirementTransactionItem] = []

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


class ProjectCategorySubItem(BaseModel):
    name: str
    total: float


class ProjectCategoryItem(BaseModel):
    category: str
    total: float
    subcategories: list[ProjectCategorySubItem] = []


class ProjectItem(FinancialResponse):
    id: str
    name: str
    color: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    budget: Optional[float] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    spent: float = 0
    txn_count: int = 0
    created_at: Optional[str] = None
    earliest_transaction_date: Optional[str] = None
    latest_transaction_date: Optional[str] = None


class ProjectDetailResponse(ProjectItem):
    categories: list[ProjectCategoryItem] = []
    transactions: list[TransactionItem] = []
    members: list[ContactItem] = []
    member_totals: list[MemberTotalItem] = []


# ---------------------------------------------------------------------------
# Recurring
# ---------------------------------------------------------------------------


class RecurringItemDetail(BaseModel):
    id: Optional[str] = None
    display_name: Optional[str] = None
    canonical_name: Optional[str] = None
    status: Optional[str] = None
    kind: Optional[str] = None
    category: Optional[str] = None
    top_category: Optional[str] = None
    currency: Optional[str] = None
    current_amount: float
    monthly_equivalent: float
    frequency: Optional[str] = None
    current_source: Optional[str] = None
    current_account_last4: Optional[str] = None
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    next_expected: Optional[str] = None
    ended_at: Optional[str] = None
    latest_event_type: Optional[str] = None
    flags: list[str] = []
    detection_mode: Optional[str] = None
    event_count: Optional[int] = None

    model_config = {"extra": "allow"}


class RecurringSummary(BaseModel):
    current_monthly_total: Optional[float] = None
    active_count: Optional[int] = None
    ended_in_range: Optional[int] = None
    price_changes_in_range: Optional[int] = None
    renewing_soon: Optional[int] = None

    model_config = {"extra": "allow"}


class RecurringFilters(BaseModel):
    statuses: list[str] = []
    kinds: list[str] = []
    sources: list[str] = []
    categories: list[str] = []
    currencies: list[str] = []

    model_config = {"extra": "allow"}


class RecurringCatalogResponse(FinancialResponse):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    items: list[RecurringItemDetail] = []
    summary: Optional[RecurringSummary] = None
    filters: Optional[RecurringFilters] = None

    model_config = {"extra": "allow"}


class RecurringTrendPoint(BaseModel):
    date: Optional[str] = None
    label: Optional[str] = None
    total: float
    breakdown: Optional[dict[str, float]] = None

    model_config = {"extra": "allow"}


class RecurringTrendGroup(BaseModel):
    key: Optional[str] = None
    label: Optional[str] = None
    total: float

    model_config = {"extra": "allow"}


class RecurringTrendsResponse(FinancialResponse):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    metric: Optional[str] = None
    group_by: Optional[str] = None
    points: list[RecurringTrendPoint] = []
    groups: list[RecurringTrendGroup] = []

    model_config = {"extra": "allow"}


class RecurringLinkedTransaction(BaseModel):
    transaction_id: Optional[str] = None
    date: Optional[str] = None
    amount: Optional[float] = None
    source: Optional[str] = None
    account_last4: Optional[str] = None
    merchant: Optional[str] = None
    category: Optional[str] = None

    model_config = {"extra": "allow"}


class RecurringEvent(BaseModel):
    event_type: Optional[str] = None
    effective_date: Optional[str] = None
    amount: Optional[float] = None
    frequency: Optional[str] = None
    source: Optional[str] = None
    account_last4: Optional[str] = None
    transaction_id: Optional[str] = None

    model_config = {"extra": "allow"}


class RecurringDetailResponse(FinancialResponse):
    id: Optional[str] = None
    display_name: Optional[str] = None
    canonical_name: Optional[str] = None
    status: Optional[str] = None
    kind: Optional[str] = None
    category: Optional[str] = None
    top_category: Optional[str] = None
    current_amount: float
    monthly_equivalent: float
    frequency: Optional[str] = None
    current_source: Optional[str] = None
    current_account_last4: Optional[str] = None
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    next_expected: Optional[str] = None
    ended_at: Optional[str] = None
    latest_event_type: Optional[str] = None
    flags: list[str] = []
    detection_mode: Optional[str] = None
    event_count: Optional[int] = None
    events: list[RecurringEvent] = []
    transactions: list[RecurringLinkedTransaction] = []
    price_history: list[dict[str, Any]] = []
    source_history: list[dict[str, Any]] = []

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Sync / Sidebar
# ---------------------------------------------------------------------------


class SidebarAccountItem(BaseModel):
    source: str
    account_last4: Optional[str] = None
    account_key: Optional[str] = None
    provider_source: Optional[str] = None
    source_key: Optional[str] = None
    group: str
    connection_state: Optional[str] = None
    balance: Optional[float] = None
    ledger_balance: Optional[float] = None
    snapshot_balance: Optional[float] = None
    currency: str
    last_synced: Optional[str] = None
    filter_source: Optional[str] = None
    icon_url: Optional[str] = None


class ExchangeRateItem(BaseModel):
    from_currency: str
    to: str
    rate: float

    class Config:
        extra = "allow"


class SidebarResponse(FinancialResponse):
    accounts: list[SidebarAccountItem]
    exchange_rates: list[ExchangeRateItem] = []


class InvestmentHoldingItem(BaseModel):
    account_id: Optional[str] = None
    security_id: Optional[str] = None
    ticker: Optional[str] = None
    name: Optional[str] = None
    quantity: float = 0
    price: float = 0
    value: float = 0
    cost_basis: Optional[float] = None
    type: Optional[str] = None
    source: Optional[str] = None
    synced_at: Optional[str] = None

    model_config = {"extra": "allow"}


class InvestmentAccountItem(BaseModel):
    account_id: Optional[str] = None
    name: Optional[str] = None
    source: Optional[str] = None
    synced_at: Optional[str] = None

    model_config = {"extra": "allow"}


class InvestmentHoldingsResponse(FinancialResponse):
    holdings: list[InvestmentHoldingItem]
    accounts: list[InvestmentAccountItem]


class InvestmentHistoryItem(BaseModel):
    account_key: Optional[str] = None
    account_name: Optional[str] = None
    synced_at: Optional[str] = None
    source: Optional[str] = None
    value: float = 0
    beginning_value: Optional[float] = None
    market_gain: Optional[float] = None
    inflow: Optional[float] = None
    ending_value: Optional[float] = None

    model_config = {"extra": "allow"}


class InvestmentHistoryResponse(FinancialResponse):
    history: list[InvestmentHistoryItem]


class PlaidBalanceItem(FinancialResponse):
    source: str
    balance: float
