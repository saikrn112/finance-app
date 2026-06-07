from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class StatementParser(Protocol):
    def __call__(self, file_path: str) -> dict[str, Any]: ...


class PayslipParser(Protocol):
    def __call__(self, file_path: str) -> dict[str, Any] | list[dict[str, Any]] | None: ...


class RetirementParser(Protocol):
    def __call__(self, file_path: str) -> dict[str, Any]: ...


class InvestmentParser(Protocol):
    def __call__(self, file_path: str) -> dict[str, Any]: ...


@dataclass
class CsvColumnConfig:
    date_col: str
    amount_col: str
    merchant_col: str
    date_fmt: str = "%m/%d/%Y"
    negate_amount: bool = False


@dataclass
class ParserPlugin:
    source_key: str
    label: str
    record_type: str  # "transactions" | "payslip" | "retirement" | "investment"
    allowed_kinds: set[str]
    domain: str  # "financial_accounts" | "investments" | "retirement" | "payroll"
    directory_name: str
    group: str = ""
    hint: str = ""
    statement_parser: Callable | None = None
    payslip_parser: Callable | None = None
    retirement_parser: Callable | None = None
    investment_parser: Callable | None = None
    csv_config: CsvColumnConfig | None = None
    currency: str = "USD"
    is_credit_card: bool = False
    icon_url: str = ""
    source_aliases: list[str] = field(default_factory=list)
    filename_patterns: list[str] = field(default_factory=list)
