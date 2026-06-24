"""Backfill normalized schema tables from SyncLog payloads.

Idempotent: skips rows that already exist (unique constraint enforcement).
Intended for explicit migration commands, not automatic startup repair.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models.transaction import (
    Payslip,
    PayslipLineItem,
    RetirementStatement,
    RetirementTransaction,
    SyncLog,
)
from src.plugins.registry import get_all_sources

logger = logging.getLogger(__name__)


def run_backfill(db: Session) -> None:
    """Run all backfill passes."""
    backfill_payslips(db)
    backfill_retirement(db)


# ---------------------------------------------------------------------------
# Payslips
# ---------------------------------------------------------------------------


def backfill_payslips(db: Session) -> int:
    """Scan SyncLog for import_payslip_pdf entries and insert Payslip + line items."""
    logs = (
        db.query(SyncLog)
        .filter(
            SyncLog.sync_type == "import_payslip_pdf",
            SyncLog.status == "success",
        )
        .order_by(SyncLog.created_at.asc())
        .all()
    )
    inserted = 0
    for log in logs:
        extra = log.extra_data or {}
        source_key = extra.get("source_key", "")
        currency = _currency_for_source(source_key)
        payloads = list(extra.get("payloads") or ([] if not extra.get("payload") else [extra["payload"]]))
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            if _insert_payslip(db, payload, source_key, currency, log.id):
                inserted += 1
    if inserted:
        db.commit()
        logger.info("Backfilled %d payslip(s)", inserted)
    return inserted


def _insert_payslip(db: Session, payload: dict[str, Any], source_key: str, currency: str, sync_log_id: str) -> bool:
    """Insert a single Payslip + its line items. Returns True if inserted."""
    signature = _payslip_signature(payload)
    if not signature:
        return False

    source_label = _source_label(source_key)
    employer = str(payload.get("employer") or "").strip()
    pay_date_val = _safe_date(payload.get("pay_date"))
    if not employer or not pay_date_val:
        return False

    payslip = Payslip(
        source=source_label,
        sync_log_id=sync_log_id,
        employer=employer,
        pay_date=pay_date_val,
        pay_period_start=_safe_date(payload.get("pay_period_start")),
        pay_period_end=_safe_date(payload.get("pay_period_end")),
        currency=currency,
        gross=round(float(payload.get("gross") or 0), 2),
        net=round(float(payload.get("net") or 0), 2),
        total_taxes=round(float(payload.get("total_taxes") or 0), 2),
        total_deductions=round(float(payload.get("total_deductions") or 0), 2),
        signature=signature,
        filename=payload.get("filename"),
    )
    try:
        with db.begin_nested():
            db.add(payslip)
            db.flush()
    except IntegrityError:
        return False

    # Insert line items
    for section in ("taxes", "deductions", "earnings"):
        items = payload.get(section)
        if isinstance(items, dict):
            for label, amount in items.items():
                if not label or not isinstance(amount, (int, float)):
                    continue
                db.add(PayslipLineItem(
                    payslip_id=payslip.id,
                    section=section,
                    label=str(label),
                    amount=round(float(amount), 2),
                ))
        elif isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or item.get("name") or "").strip()
                amount = item.get("amount") or item.get("value")
                if not label or amount is None:
                    continue
                line_item = PayslipLineItem(
                    payslip_id=payslip.id,
                    section=section,
                    label=label,
                    amount=round(float(amount), 2),
                )
                ytd = item.get("ytd")
                if ytd is not None:
                    line_item._ytd = round(float(ytd), 2)
                db.add(line_item)

    return True


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------


def backfill_retirement(db: Session) -> int:
    """Scan SyncLog for retirement entries and insert transactions + statements."""
    logs = (
        db.query(SyncLog)
        .filter(
            SyncLog.sync_type.in_(["import_retirement_csv", "import_retirement_statement_pdf"]),
            SyncLog.status == "success",
        )
        .order_by(SyncLog.created_at.asc())
        .all()
    )
    txn_inserted = 0
    stmt_inserted = 0
    for log in logs:
        extra = log.extra_data or {}
        source_key = extra.get("source_key", "")
        currency = _currency_for_source(source_key)
        payload = extra.get("payload") or {}
        source_label = _source_label(source_key)

        # Transactions
        for row in payload.get("transactions", []):
            if _insert_retirement_transaction(db, row, source_label, currency, log.id):
                txn_inserted += 1

        # Statements
        for stmt in payload.get("statements", []):
            if _insert_retirement_statement(db, stmt, source_label, currency, log.id):
                stmt_inserted += 1

    if txn_inserted or stmt_inserted:
        db.commit()
        logger.info("Backfilled %d retirement transaction(s), %d statement(s)", txn_inserted, stmt_inserted)
    return txn_inserted + stmt_inserted


def _insert_retirement_transaction(
    db: Session, row: dict[str, Any], source_label: str, currency: str, sync_log_id: str
) -> bool:
    source_id = row.get("source_id", "")
    if not source_id:
        return False
    date_val = _safe_date(row.get("date"))
    if not date_val:
        return False

    txn = RetirementTransaction(
        source=source_label,
        sync_log_id=sync_log_id,
        source_id=source_id,
        date=date_val,
        type=row.get("type", "Unknown"),
        contribution_source=row.get("contribution_source"),
        fund=row.get("fund"),
        currency=currency,
        amount=round(float(row.get("amount") or 0), 2),
        units=row.get("units"),
        unit_price=row.get("unit_price"),
    )
    try:
        with db.begin_nested():
            db.add(txn)
            db.flush()
    except IntegrityError:
        return False
    return True


def _insert_retirement_statement(
    db: Session, stmt: dict[str, Any], source_label: str, currency: str, sync_log_id: str
) -> bool:
    period_end_str = _normalize_date_string(stmt.get("period_end"))
    period_start_str = _normalize_date_string(stmt.get("period_start"))
    if not period_end_str or not period_start_str:
        return False

    row = RetirementStatement(
        source=source_label,
        sync_log_id=sync_log_id,
        plan_name=stmt.get("plan_name"),
        period_start=date.fromisoformat(period_start_str),
        period_end=date.fromisoformat(period_end_str),
        currency=currency,
        beginning_balance=round(float(stmt.get("beginning_balance") or 0), 2),
        ending_balance=round(float(stmt.get("ending_balance") or 0), 2),
        employee_contributions=round(float(stmt.get("employee_contributions") or 0), 2),
        employer_contributions=round(float(stmt.get("employer_contributions") or 0), 2),
        market_change=round(float(stmt.get("market_change") or 0), 2),
        vested_balance=round(float(stmt.get("vested_balance") or 0), 2) if stmt.get("vested_balance") is not None else None,
        rate_of_return=stmt.get("rate_of_return"),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return False
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payslip_signature(payload: dict[str, Any] | None) -> str | None:
    """Compute signature matching import_service._payslip_signature."""
    if not payload:
        return None
    employer = str(payload.get("employer") or "").strip()
    pay_date = _normalize_date_string(payload.get("pay_date"))
    if not employer or not pay_date:
        return None
    gross = round(float(payload.get("gross") or 0), 2)
    net = round(float(payload.get("net") or 0), 2)
    taxes = round(float(payload.get("total_taxes") or 0), 2)
    deductions = round(float(payload.get("total_deductions") or 0), 2)
    return hashlib.sha1(
        f"{employer}|{pay_date}|{gross:.2f}|{net:.2f}|{taxes:.2f}|{deductions:.2f}".encode("utf-8")
    ).hexdigest()[:32]


def _normalize_date_string(value: Any) -> str:
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _safe_date(value: Any) -> date | None:
    normalized = _normalize_date_string(value)
    if not normalized:
        return None
    try:
        return date.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None


def _currency_for_source(source_key: str) -> str:
    all_sources = get_all_sources()
    plugin = all_sources.get(source_key)
    return plugin.currency if plugin else "USD"


def _source_label(source_key: str) -> str:
    all_sources = get_all_sources()
    plugin = all_sources.get(source_key)
    return plugin.label if plugin else source_key
