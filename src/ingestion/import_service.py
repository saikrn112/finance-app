from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from src.config import settings
from src.data_paths import import_manifest_path, import_preview_dir, raw_source_dir
from src.ingestion.csv_importer import parse_csv
from src.models import SyncLog, Transaction, AccountSnapshot, InvestmentHoldingSnapshot
from src.services.account_values import record_account_value
from src.models import Payslip, PayslipLineItem, RetirementTransaction, RetirementStatement
from src.processing.categorizer import RuleMatcher
from src.processing.overlap_diagnostics import _merchant_matches
from src.processing.retirement_utils import summarize_retirement_transactions
from src.plugins.registry import (
    get_all_sources,
    get_credit_card_sources,
    get_import_source_defs,
    get_investment_parser,
    get_payslip_parser,
    get_retirement_parser,
    get_statement_parser,
)
CREDIT_CARD_SOURCES = None  # Populated lazily from registry


def _get_credit_card_sources() -> set[str]:
    global CREDIT_CARD_SOURCES
    if CREDIT_CARD_SOURCES is None:
        CREDIT_CARD_SOURCES = get_credit_card_sources()
    return CREDIT_CARD_SOURCES


IMPORT_WORKSPACE_DIR = Path(settings.app.data_dir) / "derived" / "preview_workspace"
IMPORT_MANIFESTS_DIR = Path(settings.app.data_dir) / "derived" / "manifests"




@dataclass
class NormalizedImportTransaction:
    source: str
    source_id: str
    date: date
    amount: float
    merchant_raw: str
    account_last4: str | None = None
    merchant_clean: str | None = None
    origin: str = "csv"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_id": self.source_id,
            "date": self.date.isoformat(),
            "amount": round(self.amount, 2),
            "merchant_raw": self.merchant_raw,
            "account_last4": self.account_last4,
            "merchant_clean": self.merchant_clean,
            "origin": self.origin,
        }


def preview_import(
    db: Session,
    *,
    file_bytes: bytes,
    filename: str,
    source: str,
    kind: str | None,
) -> dict[str, Any]:
    source_meta = _source_meta(source)
    workspace = IMPORT_WORKSPACE_DIR / uuid4().hex
    workspace.mkdir(parents=True, exist_ok=True)
    upload_path = workspace / filename
    upload_path.write_bytes(file_bytes)
    file_hash = _file_hash(upload_path)
    resolved_kind = _resolve_kind(upload_path, source, kind)

    existing_log = _find_successful_file_import(db, file_hash)
    parsed = _parse_upload(upload_path, source, resolved_kind)
    transactions = parsed["transactions"]
    duplicate_summary = _preview_duplicates(
        db,
        transactions,
        existing_log=existing_log,
        total_records=_record_count(parsed["record_type"], transactions, parsed["payload"], parsed.get("payloads")),
        record_type=parsed["record_type"],
        payload=parsed["payload"],
        payloads=parsed.get("payloads"),
        source=source_meta["label"],
        kind=resolved_kind,
    )

    all_sources = get_all_sources()
    plugin = all_sources.get(source)
    currency = plugin.currency if plugin else "USD"

    manifest = {
        "import_id": workspace.name,
        "filename": filename,
        "file_hash": file_hash,
        "source": source_meta["label"],
        "source_key": source,
        "kind": resolved_kind,
        "record_type": parsed["record_type"],
        "currency": currency,
        "path": str(upload_path),
        "created_at": datetime.utcnow().isoformat(),
        "already_imported": existing_log is not None,
        "transactions": [txn.as_dict() for txn in transactions],
        "payload": parsed["payload"],
        "payloads": parsed.get("payloads"),
        "duplicate_summary": duplicate_summary,
    }
    _write_manifest(workspace, manifest)
    return manifest


def load_preview(import_id: str) -> dict[str, Any]:
    manifest = _load_manifest(import_id)
    if not manifest:
        raise FileNotFoundError(import_id)
    return manifest


def commit_import(db: Session, import_id: str) -> dict[str, Any]:
    manifest = _load_manifest(import_id)
    if not manifest:
        raise FileNotFoundError(import_id)

    file_hash = manifest["file_hash"]
    existing_log = _find_successful_file_import(db, file_hash)
    total_records = _record_count(manifest["record_type"], manifest["transactions"], manifest.get("payload"), manifest.get("payloads"))
    duplicate_summary = manifest["duplicate_summary"]
    if existing_log:
        return {
            "status": "already_imported",
            "import_id": import_id,
            "imported": 0,
            "skipped": total_records,
            "duplicate_count": duplicate_summary["duplicate_count"],
        }

    imported = 0
    skipped = 0
    if manifest["record_type"] == "transactions":
        matcher = RuleMatcher()
        txns_to_import: list[NormalizedImportTransaction] = []
        for payload in manifest["transactions"]:
            txn = _txn_from_manifest(payload)
            if _duplicate_reason(db, txn):
                skipped += 1
            else:
                txns_to_import.append(txn)

        source_key = manifest.get("source_key", "")
        plugin = get_all_sources().get(source_key)
        currency = plugin.currency if plugin else "USD"

        for txn in txns_to_import:
            category = matcher.match(txn.merchant_raw)
            db.add(
                Transaction(
                    source=txn.source,
                    source_id=txn.source_id,
                    origin=txn.origin,
                    date=txn.date,
                    amount=txn.amount,
                    merchant_raw=txn.merchant_raw,
                    merchant_clean=(category.merchant_clean if category and category.merchant_clean else txn.merchant_raw[:50]),
                    category=category.category if category else "Uncategorized",
                    category_source="rule" if category else "default",
                    account_last4=txn.account_last4,
                    currency=currency,
                )
            )
        imported = len(txns_to_import)
        ending_balance = (manifest.get("payload") or {}).get("ending_balance")
        if ending_balance is not None and imported > 0:
            end_date_str = (manifest.get("payload") or {}).get("end_date")
            snapshot_date = datetime.fromisoformat(end_date_str) if end_date_str and "-" in str(end_date_str) else datetime.utcnow()
            account_group = "bank_account" if not (plugin and plugin.is_credit_card) else "credit_card"
            db.add(
                AccountSnapshot(
                    source=manifest["source"],
                    account_group=account_group,
                    connection_state="manual",
                    current_value=round(float(ending_balance), 2),
                    currency=currency,
                    synced_at=snapshot_date,
                )
            )
            record_account_value(
                db,
                source=manifest["source"],
                account_group=account_group,
                value=float(ending_balance),
                observed_at=snapshot_date,
                currency=currency,
                provenance="statement",
            )
    elif manifest["record_type"] == "payslip":
        if duplicate_summary["importable_count"] == 0 and duplicate_summary["duplicate_count"] > 0:
            return {
                "status": "already_imported",
                "import_id": import_id,
                "imported": 0,
                "skipped": total_records,
                "duplicate_count": duplicate_summary["duplicate_count"],
            }
        payloads = list(manifest.get("payloads") or ([] if not manifest.get("payload") else [manifest["payload"]]))
        importable_ids = {
            item["source_id"]
            for item in duplicate_summary["items"]
            if not item.get("duplicate_reason")
        }
        filtered_payloads = [payload for payload in payloads if _payslip_signature(payload) in importable_ids]
        manifest["payloads"] = filtered_payloads
        manifest["payload"] = filtered_payloads[0] if len(filtered_payloads) == 1 else None
        imported = len(filtered_payloads)
        skipped = total_records - imported

        # Write to normalized Payslip + PayslipLineItem tables
        payslip_currency = manifest.get("currency") or "USD"
        for p_payload in filtered_payloads:
            pay_date_str = _normalize_date_string(p_payload.get("pay_date"))
            if not pay_date_str:
                continue
            pay_date_val = date.fromisoformat(pay_date_str)
            payslip_row = Payslip(
                source=manifest["source"],
                employer=str(p_payload.get("employer") or "").strip(),
                pay_date=pay_date_val,
                gross=round(float(p_payload.get("gross") or 0), 2),
                net=round(float(p_payload.get("net") or 0), 2),
                total_taxes=round(float(p_payload.get("total_taxes") or 0), 2),
                total_deductions=round(float(p_payload.get("total_deductions") or 0), 2),
                currency=payslip_currency,
            )
            db.add(payslip_row)
            db.flush()  # Get payslip_row.id

            # Write line items for each section
            for section in ("taxes", "deductions", "earnings"):
                items = p_payload.get(section)
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            label = str(item.get("label") or item.get("name") or "")
                            amount_val = float(item.get("amount") or item.get("value") or 0)
                            ytd_val = item.get("ytd")
                            line_item = PayslipLineItem(
                                payslip_id=payslip_row.id,
                                section=section,
                                label=label,
                                amount=amount_val,
                            )
                            if ytd_val is not None:
                                line_item._ytd = round(float(ytd_val), 2)
                            db.add(line_item)
                elif isinstance(items, dict):
                    for label, amount_val in items.items():
                        if isinstance(amount_val, (int, float)):
                            db.add(PayslipLineItem(
                                payslip_id=payslip_row.id,
                                section=section,
                                label=str(label),
                                amount=float(amount_val),
                            ))
    elif manifest["record_type"] == "retirement":
        importable_ids = {
            item["source_id"]
            for item in duplicate_summary["items"]
            if not item.get("duplicate_reason")
        }
        payload = dict(manifest.get("payload") or {})
        payload["transactions"] = [
            txn for txn in payload.get("transactions", [])
            if txn.get("source_id") in importable_ids
        ]
        payload["summary"] = summarize_retirement_transactions(payload["transactions"])
        manifest["payload"] = payload
        imported = len(payload["transactions"])
        skipped = total_records - imported

        # Determine currency from plugin
        retirement_currency = manifest.get("currency") or "USD"

        # Write to normalized RetirementTransaction table
        for txn in payload["transactions"]:
            txn_date_str = str(txn.get("date") or "")
            if not txn_date_str or "-" not in txn_date_str:
                continue
            txn_date_val = date.fromisoformat(txn_date_str)
            db.add(RetirementTransaction(
                source=manifest["source"],
                source_id=txn.get("source_id") or "",
                date=txn_date_val,
                type=txn.get("type") or "",
                contribution_source=txn.get("source") or txn.get("contribution_source"),
                fund=txn.get("fund") or "",
                amount=round(float(txn.get("amount") or 0), 2),
                units=float(txn["units"]) if txn.get("units") else None,
                unit_price=float(txn["unit_price"]) if txn.get("unit_price") else None,
                currency=retirement_currency,
            ))

        # Write to normalized RetirementStatement table
        statements = list(payload.get("statements") or [])
        if statements:
            for stmt in statements:
                period_start_str = str(stmt.get("period_start") or "")
                period_end_str = str(stmt.get("period_end") or "")
                if not period_start_str or "-" not in period_start_str or not period_end_str or "-" not in period_end_str:
                    continue
                db.add(RetirementStatement(
                    source=manifest["source"],
                    period_start=date.fromisoformat(period_start_str),
                    period_end=date.fromisoformat(period_end_str),
                    beginning_balance=round(float(stmt.get("beginning_balance") or 0), 2),
                    ending_balance=round(float(stmt.get("ending_balance") or 0), 2),
                    employee_contributions=round(float(stmt.get("employee_contributions") or 0), 2),
                    employer_contributions=round(float(stmt.get("employer_contributions") or 0), 2),
                    market_change=round(float(stmt.get("market_change") or 0), 2),
                    vested_balance=round(float(stmt["vested_balance"]), 2) if stmt.get("vested_balance") is not None else None,
                    rate_of_return=stmt.get("rate_of_return"),
                    currency=retirement_currency,
                ))

            latest = max(statements, key=lambda row: str(row.get("period_end") or ""))
            statement_date = str(latest.get("period_end") or "")
            synced_at = datetime.fromisoformat(f"{statement_date}T12:00:00") if statement_date else datetime.utcnow()
            db.add(
                AccountSnapshot(
                    source=manifest["source"],
                    account_group="retirement",
                    connection_state="manual",
                    current_value=round(float(latest.get("ending_balance") or 0), 2),
                    synced_at=synced_at,
                    created_at=synced_at,
                )
            )
            record_account_value(
                db,
                source=manifest["source"],
                account_group="retirement",
                value=float(latest.get("ending_balance") or 0),
                observed_at=synced_at,
                currency=retirement_currency,
                provenance="statement",
            )
        if imported == 0 and duplicate_summary["duplicate_count"] > 0:
            return {
                "status": "already_imported",
                "import_id": import_id,
                "imported": 0,
                "skipped": total_records,
                "duplicate_count": duplicate_summary["duplicate_count"],
            }
    elif manifest["record_type"] == "investment":
        if duplicate_summary["importable_count"] == 0 and duplicate_summary["duplicate_count"] > 0:
            return {
                "status": "already_imported",
                "import_id": import_id,
                "imported": 0,
                "skipped": total_records,
                "duplicate_count": duplicate_summary["duplicate_count"],
            }
        payload = dict(manifest.get("payload") or {})
        account = payload.get("account") or {}
        statement_date = str(account.get("statement_date") or "")
        synced_at = datetime.fromisoformat(f"{statement_date}T12:00:00") if statement_date else datetime.utcnow()
        summary = payload.get("summary") or {}
        holdings = list(payload.get("holdings") or [])
        source_name = manifest["source"]
        investment_plugin = get_all_sources().get(manifest.get("source_key", ""))
        db.add(
            AccountSnapshot(
                source=source_name,
                account_group="investment",
                connection_state="manual",
                current_value=round(float(summary.get("ending_value") or 0), 2),
                synced_at=synced_at,
                created_at=synced_at,
            )
        )
        record_account_value(
            db,
            source=source_name,
            account_group="investment",
            value=float(summary.get("ending_value") or 0),
            observed_at=synced_at,
            currency=investment_plugin.currency if investment_plugin else "USD",
            provenance="statement",
        )
        for holding in holdings:
            db.add(
                InvestmentHoldingSnapshot(
                    source=source_name,
                    plaid_account_id=holding.get("account_id"),
                    account_name=holding.get("account_name"),
                    security_id=holding.get("security_id"),
                    ticker=holding.get("ticker"),
                    name=holding.get("name"),
                    quantity=holding.get("quantity"),
                    price=holding.get("price"),
                    value=round(float(holding.get("value") or 0), 2),
                    cost_basis=holding.get("cost_basis"),
                    type=holding.get("type"),
                    synced_at=synced_at,
                    created_at=synced_at,
                )
            )
        imported = 1
        skipped = total_records - imported
    else:
        imported = total_records

    archive_path = _archive_import_file(Path(manifest["path"]), manifest["source_key"])
    log = SyncLog(
        source=manifest["source"],
        sync_type=f"import_{manifest['kind']}",
        file_hash=file_hash,
        record_count=imported,
        status="success",
        extra_data={
            "filename": manifest["filename"],
            "import_id": import_id,
            "source_key": manifest["source_key"],
            "kind": manifest["kind"],
            "record_type": manifest["record_type"],
            "archived_path": str(archive_path),
            "duplicate_summary": duplicate_summary,
            "payload": manifest.get("payload"),
            "payloads": manifest.get("payloads"),
        },
    )
    db.add(log)
    db.commit()

    return {
        "status": "success",
        "import_id": import_id,
        "imported": imported,
        "skipped": skipped,
        "duplicate_count": manifest["duplicate_summary"]["duplicate_count"],
        "archived_path": str(archive_path),
    }


def _parse_upload(file_path: Path, source: str, kind: str) -> dict[str, Any]:
    source_meta = _source_meta(source)
    if kind == "csv":
        parsed = list(parse_csv(file_path, source))
        normalized = [
            NormalizedImportTransaction(
                source=source_meta["label"],
                source_id="",
                date=txn.date,
                amount=float(txn.amount),
                merchant_raw=txn.merchant_raw,
                account_last4=txn.account_last4 or None,
                origin="csv",
            )
            for txn in parsed
        ]
        return {
            "record_type": "transactions",
            "transactions": _assign_stable_ids(normalized),
            "payload": None,
        }

    if kind == "statement_pdf":
        source_defs = get_import_source_defs()
        source_def = source_defs.get(source, {})
        if source_def.get("record_type") == "retirement":
            stmt_parser = get_statement_parser(source)
            if not stmt_parser:
                raise ValueError(f"No statement parser for {source}")
            payload = stmt_parser(file_path)
            payload = {**payload, "filename": file_path.name}
            return {
                "record_type": "retirement",
                "transactions": [],
                "payload": payload,
            }
        parser = _statement_parser(source)
        if not parser:
            raise ValueError(f"Unsupported statement source: {source}")
        try:
            parsed = parser(str(file_path))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Could not parse this statement PDF: {exc}") from exc
        normalized = []
        for txn in parsed["transactions"]:
            amount = float(txn["amount"])
            if parsed["source"] in _get_credit_card_sources():
                amount = -amount
            normalized.append(
                NormalizedImportTransaction(
                    source=parsed["source"],
                    source_id="",
                    date=date.fromisoformat(txn["date"]),
                    amount=amount,
                    merchant_raw=txn["description"],
                    origin="statements",
                )
            )
        return {
            "record_type": "transactions",
            "transactions": _assign_stable_ids(normalized),
            "payload": {
                "beginning_balance": parsed.get("beginning_balance"),
                "ending_balance": parsed.get("ending_balance"),
                "start_date": parsed.get("start_date"),
                "end_date": parsed.get("end_date"),
            },
        }

    if kind == "payslip_pdf":
        parser = _payslip_parser(source)
        if not parser:
            raise ValueError(f"Unsupported payroll source: {source}")
        try:
            payload = parser(str(file_path))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Could not parse this payslip PDF: {exc}") from exc
        if not payload:
            raise ValueError("No payroll data could be parsed from this PDF")
        if isinstance(payload, list):
            payloads = [{**item, "filename": file_path.name} for item in payload]
            payload = payloads[0] if len(payloads) == 1 else None
        else:
            payload = {**payload, "filename": file_path.name}
            payloads = [payload]
        return {
            "record_type": "payslip",
            "transactions": [],
            "payload": payload,
            "payloads": payloads,
        }

    if kind == "retirement_csv":
        retirement_parser = get_retirement_parser(source)
        if not retirement_parser:
            raise ValueError(f"No retirement parser for source: {source}")
        payload = retirement_parser(file_path)
        if not payload.get("transactions"):
            raise ValueError("No retirement transactions were parsed from this CSV")
        payload = {**payload, "filename": file_path.name}
        return {
            "record_type": "retirement",
            "transactions": [],
            "payload": payload,
        }

    if kind == "retirement_statement_pdf":
        stmt_parser = get_statement_parser(source)
        if not stmt_parser:
            raise ValueError(f"Unsupported retirement statement source: {source}")
        payload = stmt_parser(file_path)
        payload = {**payload, "filename": file_path.name}
        return {
            "record_type": "retirement",
            "transactions": [],
            "payload": payload,
        }

    if kind == "investment_csv":
        inv_parser = get_investment_parser(source)
        if not inv_parser:
            raise ValueError(f"No investment parser for source: {source}")
        payload = inv_parser(file_path)
        if not payload.get("holdings"):
            raise ValueError("No investment holdings were parsed from this CSV")
        payload = {**payload, "filename": file_path.name}
        return {
            "record_type": "investment",
            "transactions": [],
            "payload": payload,
        }

    raise ValueError(f"Unsupported import kind: {kind}")


def _assign_stable_ids(txns: list[NormalizedImportTransaction]) -> list[NormalizedImportTransaction]:
    ordered = sorted(txns, key=lambda txn: (txn.date, txn.merchant_raw, txn.amount, txn.account_last4 or ""))
    counts: dict[tuple[str, str, float, str | None], int] = defaultdict(int)
    for txn in ordered:
        dedup_key = (
            txn.date.isoformat(),
            _normalize_merchant(txn.merchant_raw),
            round(txn.amount, 2),
            txn.account_last4,
        )
        occurrence = counts[dedup_key]
        counts[dedup_key] += 1
        txn.source_id = hashlib.sha1(
            f"{txn.source}|{dedup_key[0]}|{dedup_key[1]}|{dedup_key[2]:.2f}|{txn.account_last4 or ''}|{occurrence}".encode("utf-8")
        ).hexdigest()[:32]
    return ordered


def _preview_duplicates(
    db: Session,
    txns: list[NormalizedImportTransaction],
    *,
    existing_log: SyncLog | None,
    total_records: int,
    record_type: str,
    payload: dict[str, Any] | None,
    payloads: list[dict[str, Any]] | None,
    source: str,
    kind: str,
) -> dict[str, Any]:
    if existing_log:
        items = [
            {
                **txn.as_dict(),
                "duplicate_reason": {
                    "type": "file_hash",
                    "existing_id": existing_log.id,
                    "existing_origin": existing_log.sync_type,
                },
            }
            for txn in txns
        ]
        return {
            "total_transactions": total_records,
            "duplicate_count": total_records,
            "importable_count": 0,
            "items": items,
        }

    if record_type == "payslip":
        return _preview_payslip_duplicates(db, payloads or ([] if not payload else [payload]), source)

    if record_type == "retirement":
        return _preview_retirement_duplicates(db, payload, source, kind)

    if record_type == "investment":
        return _preview_investment_duplicates(db, payload, source, kind)

    items = []
    duplicate_count = 0
    for txn in txns:
        reason = _duplicate_reason(db, txn)
        if reason:
            duplicate_count += 1
        items.append({**txn.as_dict(), "duplicate_reason": reason})
    return {
        "total_transactions": total_records,
        "duplicate_count": duplicate_count,
        "importable_count": total_records - duplicate_count,
        "items": items,
    }


def _parse_all_legacy_payslips() -> list[dict]:
    """Return previously-imported legacy payslips for dedup detection.

    Override via plugins if you need legacy payslip scanning.
    """
    return []


# Alias kept for test monkeypatching compatibility
parse_all_payslips = _parse_all_legacy_payslips


def _preview_payslip_duplicates(
    db: Session,
    payloads: list[dict[str, Any]],
    source: str,
) -> dict[str, Any]:
    if not payloads:
        return {
            "total_transactions": 0,
            "duplicate_count": 0,
            "importable_count": 0,
            "items": [],
        }
    existing_logs = (
        db.query(SyncLog)
        .filter(
            SyncLog.source == source,
            SyncLog.sync_type == "import_payslip_pdf",
            SyncLog.status == "success",
        )
        .order_by(SyncLog.created_at.asc())
        .all()
    )
    existing_signatures: dict[str, dict[str, str]] = {}
    for log in existing_logs:
        extra = log.extra_data or {}
        stored_payloads = list(extra.get("payloads") or ([] if not extra.get("payload") else [extra["payload"]]))
        for stored in stored_payloads:
            signature = _payslip_signature(stored)
            if signature and signature not in existing_signatures:
                existing_signatures[signature] = {
                    "existing_id": log.id,
                    "existing_origin": log.sync_type,
                }
    for legacy in parse_all_payslips():
        signature = _payslip_signature(legacy)
        if signature and signature not in existing_signatures:
            existing_signatures[signature] = {
                "existing_id": f"legacy-payslip:{signature}",
                "existing_origin": "legacy_payslips",
            }

    items = []
    duplicate_count = 0
    for payload in payloads:
        signature = _payslip_signature(payload)
        if not signature:
            continue
        existing = existing_signatures.get(signature)
        duplicate_reason = None
        if existing:
            duplicate_reason = {
                "type": "payslip_signature",
                "existing_id": existing["existing_id"],
                "existing_origin": existing["existing_origin"],
            }
            duplicate_count += 1
        employer = (payload or {}).get("employer") or source
        pay_date = _normalize_date_string((payload or {}).get("pay_date"))
        items.append({
            "source": source,
            "source_id": signature,
            "date": pay_date,
            "amount": round(float((payload or {}).get("net") or 0), 2),
            "merchant_raw": f"{employer} payslip",
            "account_last4": None,
            "merchant_clean": employer,
            "origin": "payslip",
            "duplicate_reason": duplicate_reason,
        })
    return {
        "total_transactions": len(items),
        "duplicate_count": duplicate_count,
        "importable_count": len(items) - duplicate_count,
        "items": items,
    }


def _preview_retirement_duplicates(
    db: Session,
    payload: dict[str, Any] | None,
    source: str,
    kind: str,
) -> dict[str, Any]:
    rows = list((payload or {}).get("transactions") or [])
    existing_ids = _existing_payload_source_ids(
        db,
        source=source,
        sync_type=f"import_{kind}",
    )
    items = []
    duplicate_count = 0
    for row in rows:
        duplicate_reason = None
        source_id = row.get("source_id", "")
        if source_id in existing_ids:
            existing = existing_ids[source_id]
            duplicate_reason = {
                "type": "retirement_source_id",
                "existing_id": existing["existing_id"],
                "existing_origin": existing["existing_origin"],
            }
            duplicate_count += 1
        items.append(
            {
                "source": source,
                "source_id": source_id,
                "date": row.get("date", ""),
                "amount": round(float(row.get("amount") or 0), 2),
                "merchant_raw": f"{row.get('type', 'Activity')} · {row.get('fund', '')}".strip(" ·"),
                "account_last4": None,
                "merchant_clean": row.get("fund"),
                "origin": "retirement",
                "duplicate_reason": duplicate_reason,
            }
        )
    total_records = len(rows)
    return {
        "total_transactions": total_records,
        "duplicate_count": duplicate_count,
        "importable_count": total_records - duplicate_count,
        "items": items,
    }


def _preview_investment_duplicates(
    db: Session,
    payload: dict[str, Any] | None,
    source: str,
    kind: str,
) -> dict[str, Any]:
    account = (payload or {}).get("account") or {}
    summary = (payload or {}).get("summary") or {}
    source_id = str(account.get("source_id") or "")
    existing_ids = _existing_payload_source_ids(
        db,
        source=source,
        sync_type=f"import_{kind}",
    )
    duplicate_reason = None
    if source_id and source_id in existing_ids:
        existing = existing_ids[source_id]
        duplicate_reason = {
            "type": "retirement_source_id",
            "existing_id": existing["existing_id"],
            "existing_origin": existing["existing_origin"],
        }
    items = []
    if source_id:
        items.append(
            {
                "source": source,
                "source_id": source_id,
                "date": account.get("statement_date", ""),
                "amount": round(float(summary.get("ending_value") or 0), 2),
                "merchant_raw": f"{account.get('account_type', 'HSA')} monthly statement",
                "account_last4": str(account.get("account_id") or "")[-4:] or None,
                "merchant_clean": source,
                "origin": "investment",
                "duplicate_reason": duplicate_reason,
            }
        )
    total_records = len(items)
    duplicate_count = 1 if duplicate_reason else 0
    return {
        "total_transactions": total_records,
        "duplicate_count": duplicate_count,
        "importable_count": total_records - duplicate_count,
        "items": items,
    }


def _duplicate_reason(db: Session, txn: NormalizedImportTransaction) -> dict[str, Any] | None:
    exact = db.query(Transaction).filter(
        Transaction.source == txn.source,
        Transaction.source_id == txn.source_id,
    ).first()
    if exact:
        return {
            "type": "source_id",
            "existing_id": exact.id,
            "existing_origin": exact.origin,
        }

    legacy_statement = db.query(Transaction).filter(
        Transaction.source == txn.source,
        Transaction.date == txn.date,
        Transaction.origin == "statements",
    ).all()
    for existing in legacy_statement:
        if round(float(existing._amount), 2) != round(txn.amount, 2):
            continue
        if not _merchant_matches(existing.merchant_raw, txn.merchant_raw):
            continue
        return {
            "type": "legacy_statement",
            "existing_id": existing.id,
            "existing_origin": existing.origin,
            "existing_date": existing.date.isoformat(),
            "existing_amount": round(float(existing._amount), 2),
            "existing_merchant_raw": existing.merchant_raw,
        }

    lower_bound = txn.date - timedelta(days=3)
    upper_bound = txn.date + timedelta(days=3)
    nearby = db.query(Transaction).filter(
        Transaction.source == txn.source,
        Transaction.date >= lower_bound,
        Transaction.date <= upper_bound,
        Transaction.origin == "plaid",
    ).all()
    for existing in nearby:
        if round(abs(float(existing._amount)), 2) != round(abs(txn.amount), 2):
            continue
        if not _merchant_matches(existing.merchant_raw, txn.merchant_raw):
            continue
        return {
            "type": "heuristic",
            "existing_id": existing.id,
            "existing_origin": existing.origin,
            "existing_date": existing.date.isoformat(),
            "existing_amount": round(float(existing._amount), 2),
            "existing_merchant_raw": existing.merchant_raw,
        }
    return None


def _txn_from_manifest(payload: dict[str, Any]) -> NormalizedImportTransaction:
    return NormalizedImportTransaction(
        source=payload["source"],
        source_id=payload["source_id"],
        date=date.fromisoformat(payload["date"]),
        amount=float(payload["amount"]),
        merchant_raw=payload["merchant_raw"],
        account_last4=payload.get("account_last4"),
        merchant_clean=payload.get("merchant_clean"),
        origin=payload["origin"],
    )


def _record_count(record_type: str, transactions: list[Any], payload: dict[str, Any] | None, payloads: list[dict[str, Any]] | None = None) -> int:
    if record_type == "transactions":
        return len(transactions)
    if record_type == "payslip":
        if payloads is not None:
            return len(payloads)
        return 1 if payload else 0
    if record_type == "retirement":
        return len((payload or {}).get("transactions", []))
    if record_type == "investment":
        return 1 if payload else 0
    return 0


def _find_successful_file_import(db: Session, file_hash: str) -> SyncLog | None:
    return db.query(SyncLog).filter(SyncLog.file_hash == file_hash, SyncLog.status == "success").first()


def _find_matching_payload_import(
    db: Session,
    *,
    source: str,
    sync_type: str,
    matcher,
) -> SyncLog | None:
    logs = (
        db.query(SyncLog)
        .filter(
            SyncLog.source == source,
            SyncLog.sync_type == sync_type,
            SyncLog.status == "success",
        )
        .order_by(SyncLog.created_at.asc())
        .all()
    )
    for log in logs:
        payload = (log.extra_data or {}).get("payload") or {}
        if matcher(payload):
            return log
    return None


def _existing_payload_source_ids(
    db: Session,
    *,
    source: str,
    sync_type: str,
) -> dict[str, dict[str, str]]:
    logs = (
        db.query(SyncLog)
        .filter(
            SyncLog.source == source,
            SyncLog.sync_type == sync_type,
            SyncLog.status == "success",
        )
        .order_by(SyncLog.created_at.asc())
        .all()
    )
    existing: dict[str, dict[str, str]] = {}
    for log in logs:
        payload = (log.extra_data or {}).get("payload") or {}
        for txn in payload.get("transactions", []):
            source_id = txn.get("source_id")
            if source_id and source_id not in existing:
                existing[source_id] = {
                    "existing_id": log.id,
                    "existing_origin": log.sync_type,
                }
    return existing




def migrate_import_artifact_paths(db: Session) -> bool:
    logs = (
        db.query(SyncLog)
        .filter(SyncLog.status == "success")
        .all()
    )
    known_sources = get_import_source_defs()
    changed = False
    for log in logs:
        extra = dict(log.extra_data or {})
        log_changed = False
        source_key = str(extra.get("source_key") or "").strip()
        archived_path = str(extra.get("archived_path") or "").strip()
        if source_key and archived_path and source_key in known_sources:
            new_archived_path = str(_canonical_import_artifact_path(Path(archived_path).name, source_key))
            if archived_path != new_archived_path:
                extra["archived_path"] = new_archived_path
                log_changed = True
        migrated_path = str(extra.get("migrated_from_legacy_path") or "").strip()
        if source_key and migrated_path and source_key in known_sources:
            new_migrated_path = str(_canonical_import_artifact_path(Path(migrated_path).name, source_key))
            if migrated_path != new_migrated_path:
                extra["migrated_from_legacy_path"] = new_migrated_path
                log_changed = True
        if log_changed:
            log.extra_data = extra
            changed = True
    if changed:
        db.commit()
    return changed


def _payslip_signature(payload: dict[str, Any] | None) -> str | None:
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


def _source_meta(source: str) -> dict[str, Any]:
    defs = get_import_source_defs()
    if source not in defs:
        raise ValueError(f"Unsupported source: {source}")
    return defs[source]


def _resolve_kind(file_path: Path, source: str, kind: str | None) -> str:
    source_meta = _source_meta(source)
    allowed_kinds = source_meta["allowed_kinds"]
    suffix = file_path.suffix.lower()

    if kind is None:
        if suffix == ".pdf":
            if "payslip_pdf" in allowed_kinds:
                return "payslip_pdf"
            if "retirement_statement_pdf" in allowed_kinds:
                return "retirement_statement_pdf"
            if "statement_pdf" in allowed_kinds:
                return "statement_pdf"
        if suffix == ".csv":
            if "retirement_csv" in allowed_kinds:
                return "retirement_csv"
            if "investment_csv" in allowed_kinds:
                return "investment_csv"
            if "csv" in allowed_kinds:
                return "csv"
        raise ValueError(f"Could not infer import kind for {file_path.name}")

    if kind not in allowed_kinds:
        raise ValueError(f"{source_meta['label']} does not support {kind}")
    if kind.endswith("_pdf") and suffix != ".pdf":
        raise ValueError("Choose a PDF file for this source")
    if kind.endswith("csv") and suffix != ".csv":
        raise ValueError("Choose a CSV file for this source")
    return kind


def _file_hash(file_path: Path) -> str:
    sha256 = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _normalize_merchant(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())


def _statement_parser(source: str):
    return get_statement_parser(source)


def _payslip_parser(source: str):
    return get_payslip_parser(source)


def _archive_import_file(file_path: Path, source: str) -> Path:
    archive_dir = raw_source_dir(source)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_name = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{source}_{file_path.name}"
    archive_path = archive_dir / archive_name
    shutil.copy(file_path, archive_path)
    return archive_path


def _write_manifest(workspace: Path, manifest: dict[str, Any]) -> None:
    IMPORT_MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    import_manifest_path(manifest["import_id"]).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _load_manifest(import_id: str) -> dict[str, Any] | None:
    manifest_path = import_manifest_path(import_id)
    if not manifest_path.exists():
        legacy_path = IMPORT_WORKSPACE_DIR / import_id / "manifest.json"
        if not legacy_path.exists():
            return None
        manifest = json.loads(legacy_path.read_text(encoding="utf-8"))
        _write_manifest(import_preview_dir(import_id), manifest)
        return manifest
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    file_path = Path(str(manifest.get("path") or ""))
    if not file_path.exists():
        candidate = import_preview_dir(import_id) / str(manifest.get("filename") or "")
        if candidate.exists():
            manifest["path"] = str(candidate)
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _canonical_import_artifact_path(filename: str, source_key: str) -> Path:
    return raw_source_dir(source_key) / filename
