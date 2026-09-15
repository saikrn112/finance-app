from fastapi import APIRouter, Depends, Query, UploadFile, File, Form, HTTPException
from sqlalchemy.orm import Session
from datetime import date, datetime, timedelta
import tempfile
import os
import json
import logging

from sqlalchemy import and_, func, literal

from src.models import (
    get_db, SyncLog, Transaction, AccountSnapshot, InvestmentHoldingSnapshot,
    ExchangeRate, SourceBalanceHistory, InvestmentPeriodFact, AccountActivity, ConnectedAccount,
)
from src.config import settings
from src.demo import get_demo_investments, get_demo_plaid_balances, get_demo_sidebar_accounts
from src.ingestion.plaid_client import create_link_token, exchange_public_token, remove_item, sync_transactions, get_investment_holdings, get_investment_transactions, get_account_balances
from src.ingestion.csv_importer import import_csv_file
from src.ingestion.import_service import commit_import, preview_import
from src.ingestion.plaid_sync import apply_plaid_sync_batch
from src.ingestion.plaid_activity import apply_account_activity_batch, plaid_transactions_destination, relink_orphaned_account_rows
from src.ingestion.plaid_usage import (
    plaid_usage_summary, record_plaid_usage, finish_plaid_usage, upsert_product_enrollments,
)
from src.processing.categorizer import RuleMatcher
from src.processing.parse_retirement import summarize_retirement_transactions
from src.plugins.registry import get_all_sources, get_credit_card_sources, classify_source, get_plaid_account_profile
from src.services.exchange_rates import latest_rate_subquery, ensure_rates_fresh
from src.services import job_lock
from src.services.plaid_health import blocked_error, record_error, record_success
from src.services.account_values import latest_account_values, record_account_value
from src.api.schemas import (
    SidebarResponse,
    InvestmentHoldingsResponse,
    InvestmentHistoryResponse,
    PlaidBalanceItem,
)

router = APIRouter()
logger = logging.getLogger(__name__)

SNAPSHOT_REFRESH_INTERVAL = timedelta(days=14)


def _get_retirement_import_sources() -> set[str]:
    """Return all source aliases for retirement-domain plugins (for matching SyncLog records)."""
    sources: set[str] = set()
    for plugin in get_all_sources().values():
        if plugin.domain == "retirement":
            sources.add(plugin.label)
            sources.update(plugin.source_aliases)
    return sources


def _get_investment_sources() -> set[str]:
    """Return labels of investment-domain plugins."""
    return {p.label for p in get_all_sources().values() if p.domain == "investments"}


def _get_retirement_source_label() -> str:
    """Return the label of the first retirement-domain plugin (for sidebar display)."""
    for p in get_all_sources().values():
        if p.domain == "retirement":
            return p.label
    return "Retirement"


def _plaid_institution_key(log: SyncLog) -> str:
    institution = ((log.extra_data or {}).get("institution_name") or "").strip()
    if institution:
        return institution.casefold()
    return (log.plaid_item_id or log.id or "unknown").casefold()


def _connected_plaid_logs(db: Session) -> list[SyncLog]:
    logs = (
        db.query(SyncLog)
        .filter(SyncLog.source == "plaid", SyncLog.status == "connected")
        .order_by(SyncLog.created_at.desc())
        .all()
    )
    grouped: dict[str, SyncLog] = {}
    for log in logs:
        key = _plaid_institution_key(log)
        grouped.setdefault(key, log)
    return list(grouped.values())


def _account_fingerprints(accounts: list[dict]) -> list[dict[str, str]]:
    """Keep only stable, non-secret fields needed to detect duplicate Items."""
    fingerprints = []
    for account in accounts:
        fingerprint = {
            key: str(account.get(key) or "").strip()
            for key in ("mask", "name", "type", "subtype")
        }
        if fingerprint["mask"] or fingerprint["name"]:
            fingerprints.append(fingerprint)
    return fingerprints


def _upsert_connected_accounts(
    db: Session, log: SyncLog, institution: str, accounts: list[dict], synced_at: datetime,
) -> dict[str, ConnectedAccount]:
    """Persist the accounts within an Item and return them by Plaid account id."""
    source_key = classify_source(institution) or institution
    seen: set[str] = set()
    result: dict[str, ConnectedAccount] = {}
    for account in accounts:
        external_id = str(account.get("account_id") or "")
        if not external_id:
            continue
        seen.add(external_id)
        row = db.query(ConnectedAccount).filter(
            ConnectedAccount.sync_log_id == log.id,
            ConnectedAccount.external_account_id == external_id,
        ).first()
        profile = get_plaid_account_profile(institution, account)
        if row is None and account.get("mask"):
            candidates = db.query(ConnectedAccount).filter(
                ConnectedAccount.sync_log_id == log.id,
                ConnectedAccount.mask == str(account["mask"]),
                ConnectedAccount.account_type == str(account.get("type") or ""),
                ConnectedAccount.subtype == str(account.get("subtype") or ""),
                ConnectedAccount.external_account_id.notin_([str(a.get("account_id") or "") for a in accounts]),
            ).all()
            if len(candidates) == 1:
                row = candidates[0]
                old_id = row.external_account_id
                for model, column in [(Transaction, Transaction.plaid_account_id),
                                      (AccountActivity, AccountActivity.account_id),
                                      (InvestmentHoldingSnapshot, InvestmentHoldingSnapshot.plaid_account_id)]:
                    db.query(model).filter(column == old_id, model.source == institution).update({column: external_id}, synchronize_session=False)
                row.external_account_id = external_id
        if row is None:
            row = ConnectedAccount(sync_log_id=log.id, external_account_id=external_id)
            db.add(row)
        row.source_key = source_key
        row.source = institution
        row.name = str(account.get("name") or institution)
        row.display_name = profile["display_name"]
        row.mask = str(account.get("mask") or "")[-4:] or None
        row.account_type = str(account.get("type") or "") or None
        row.subtype = str(account.get("subtype") or "") or None
        row.account_group = profile["group"]
        row.detail_view = profile["detail_view"] or None
        row.currency = str(account.get("currency") or "USD")
        row.active = True
        row.last_seen_at = synced_at
        db.flush()
        result[external_id] = row
    if seen:
        db.query(ConnectedAccount).filter(
            ConnectedAccount.sync_log_id == log.id,
            ConnectedAccount.external_account_id.notin_(seen),
        ).update({ConnectedAccount.active: False}, synchronize_session=False)
    relink_orphaned_account_rows(db, institution)
    return result


def _has_duplicate_connection(
    db: Session,
    institution_name: str,
    institution_id: str | None,
    fingerprints: list[dict[str, str]],
) -> bool:
    institution_key = institution_name.strip().casefold()
    for log in _connected_plaid_logs(db):
        extra = log.extra_data or {}
        same_institution = bool(
            institution_id
            and extra.get("institution_id")
            and institution_id == extra.get("institution_id")
        ) or bool(
            institution_key
            and institution_key == str(extra.get("institution_name") or "").strip().casefold()
        )
        if not same_institution:
            continue
        # One Plaid Item per institution. Update mode is the supported way to
        # repair consent or add accounts without duplicating transaction IDs.
        return True
    return False


# Plaid raises these when an Item was never linked with the investments product. They are a
# permanent property of the Item, not a transient sync failure, so we stop asking rather
# than resurfacing the same error on every sync.
_NO_INVESTMENTS_ERROR_CODES = {
    "ADDITIONAL_CONSENT_REQUIRED",
    "INVALID_PRODUCT",
    "PRODUCTS_NOT_SUPPORTED",
}


def _item_supports_investments(log: SyncLog) -> bool:
    """Whether it is worth asking this Item for holdings.

    Only returns False on positive evidence: the Item records its products and investments
    is absent. Items linked before products were tracked record nothing, and those must
    still be attempted or genuine brokerage accounts would silently stop syncing.
    """
    extra = log.extra_data or {}
    if extra.get("investments_unavailable"):
        return False
    products = extra.get("plaid_products")
    if not products:
        return True
    return "investments" in products


# Plaid returns this when the Item no longer exists on its side: removed via /item/remove,
# or access revoked at the institution. Update mode cannot repair it — the only way back is
# to disconnect locally and link the institution again — so it gets its own state rather
# than being reported as a sync failure the user could retry.
ITEM_GONE_ERROR_CODE = "ITEM_NOT_FOUND"


def _plaid_error_code(exc: Exception) -> str | None:
    body = getattr(exc, "body", None)
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except Exception:
        return None
    return parsed.get("error_code") if isinstance(parsed, dict) else None


def _mark_item_gone(db: Session, log: SyncLog, exc: Exception) -> None:
    """Record that this Item is unusable so the UI stops offering Reconnect."""
    extra = dict(log.extra_data or {})
    extra["last_sync_error"] = _plaid_sync_error_payload(exc)
    extra["item_gone"] = True
    log.extra_data = extra
    db.commit()


def _plaid_sync_error_payload(exc: Exception) -> dict:
    payload = {
        "type": type(exc).__name__,
        "message": str(exc),
        "code": None,
        "display_message": None,
        "documentation_url": None,
    }
    body = getattr(exc, "body", None)
    if body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                payload["code"] = parsed.get("error_code")
                payload["display_message"] = parsed.get("display_message") or parsed.get("error_message")
                payload["documentation_url"] = parsed.get("documentation_url")
        except Exception:
            pass
    return payload


def _transaction_balance_map(db: Session, target_currency: str | None = None) -> dict[str, float]:
    if target_currency:
        ensure_rates_fresh(db)
        lr = latest_rate_subquery(db)
        rows = (
            db.query(
                Transaction.source,
                func.sum(Transaction._amount * lr.c.rate).label("balance"),
            )
            .join(lr, and_(
                lr.c.from_currency == Transaction.currency,
                lr.c.to_currency == literal(target_currency),
            ))
            .group_by(Transaction.source)
            .all()
        )
    else:
        rows = (
            db.query(
                Transaction.source,
                func.sum(Transaction._amount).label("balance"),
            )
            .group_by(Transaction.source)
            .all()
        )
    return {row.source: round(float(row.balance or 0), 2) for row in rows}


def _classify_sidebar_group(
    source: str,
    *,
    ledger_balance: float | None = None,
    plaid_account_types: list[str] | None = None,
) -> str:
    types = {t for t in (plaid_account_types or []) if t}

    # Try registry-based classification
    all_sources = get_all_sources()
    source_key = classify_source(source)
    plugin = all_sources.get(source_key)

    if plugin:
        if plugin.domain == "retirement":
            return "retirement"
        if plugin.domain == "investments":
            return "investment"
        if plugin.is_credit_card:
            return "credit_card"

    # Plaid account type hints
    if "investment" in types:
        return "investment"
    if types and types <= {"credit", "loan"}:
        return "credit_card"

    # Heuristic: negative ledger balance likely means credit card (unless it's a known non-CC)
    investment_sources = _get_investment_sources()
    if ledger_balance is not None and ledger_balance < 0 and source not in investment_sources:
        # Exclude bank accounts from the negative-balance heuristic
        if not plugin or plugin.is_credit_card:
            return "credit_card"

    return "bank_account"


def _signed_snapshot_value(group: str, value: float) -> float:
    if group == "credit_card":
        return -abs(value)
    return round(value, 2)


def _normalize_sidebar_group(group: str | None) -> str:
    return {
        "bank_accounts": "bank_account",
        "credit_cards": "credit_card",
        "brokerage": "investment",
    }.get(group or "", group or "bank_account")


def _persist_account_snapshot(
    db: Session,
    *,
    source: str,
    account_group: str,
    connection_state: str,
    current_value: float,
    synced_at: datetime,
    account_key: str | None = None,
    account_name: str | None = None,
    currency: str = "USD",
) -> bool:
    snapshot_value = round(current_value, 2)
    source_key = classify_source(source) or source
    latest = (
        db.query(SourceBalanceHistory)
        .filter(
            SourceBalanceHistory.source_key == source_key,
            SourceBalanceHistory.account_key == account_key,
            SourceBalanceHistory.account_group == account_group,
            SourceBalanceHistory.currency == currency,
            SourceBalanceHistory.provenance == "account_snapshot",
        )
        .order_by(SourceBalanceHistory.date.desc(), SourceBalanceHistory.created_at.desc())
        .first()
    )
    if latest is not None and round(float(latest._value), 2) == snapshot_value:
        return False

    db.add(
        AccountSnapshot(
            source=source,
            account_key=account_key,
            account_name=account_name,
            account_group=account_group,
            connection_state=connection_state,
            current_value=snapshot_value,
            currency=currency,
            synced_at=synced_at,
            created_at=synced_at,
        )
    )
    if account_group == "investment" and account_key is None:
        _persist_investment_period_fact(
            db,
            source=source,
            source_key=source_key,
            previous=latest,
            ending_value=snapshot_value,
            period_end=synced_at.date(),
        )
    record_account_value(
        db,
        source=source,
        account_group=account_group,
        value=snapshot_value,
        observed_at=synced_at,
        provenance="account_snapshot",
        currency=currency,
        account_key=account_key,
        account_name=account_name,
    )
    return True


def _persist_investment_period_fact(
    db: Session,
    *,
    source: str,
    source_key: str,
    previous: SourceBalanceHistory | None,
    ending_value: float,
    period_end: date,
) -> None:
    query = db.query(AccountActivity).filter(
        (AccountActivity.source_key == source_key) | (AccountActivity.source == source),
        AccountActivity.date <= period_end,
        AccountActivity.pending.is_(False),
        AccountActivity.currency == "USD",
        AccountActivity.activity_type == "transfer",
    )
    if previous is not None:
        query = query.filter(AccountActivity.date > previous.date)
    inflow = round(sum(float(row._amount or 0) for row in query.all()), 2)
    beginning_value = round(float(previous._value), 2) if previous is not None else None
    market_gain = round(ending_value - beginning_value - inflow, 2) if beginning_value is not None else 0.0
    fact = (
        db.query(InvestmentPeriodFact)
        .filter(InvestmentPeriodFact.source == source, InvestmentPeriodFact.period_end == period_end)
        .first()
    )
    if fact is None:
        fact = InvestmentPeriodFact(source=source, period_end=period_end)
        db.add(fact)
    fact.source_key = source_key
    fact.period_start = previous.date if previous is not None else None
    fact._beginning_value = beginning_value
    fact._ending_value = ending_value
    fact._inflow = inflow
    fact._market_gain = market_gain
    fact.currency = "USD"
    fact.provenance = "plaid_snapshot_activity"
    fact.created_at = datetime.utcnow()


def _plaid_activity_destination(log: SyncLog) -> str:
    institution = (log.extra_data or {}).get("institution_name", "Unknown")
    return plaid_transactions_destination(institution)


def _persist_investment_holdings_snapshot(
    db: Session,
    *,
    source: str,
    holdings: list[dict],
    accounts: list[dict],
    synced_at: datetime,
) -> None:
    account_names = {
        str(account.get("account_id") or ""): account.get("name")
        for account in accounts
    }
    for holding in holdings:
        db.add(
            InvestmentHoldingSnapshot(
                source=source,
                plaid_account_id=holding.get("account_id"),
                account_name=account_names.get(str(holding.get("account_id") or ""), None),
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


def _latest_investment_holdings(db: Session, currency: str = "USD") -> dict:
    """Return latest holdings with rate-converted value and cost_basis."""
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)

    # Subquery: find the max synced_at per source
    latest_times = (
        db.query(
            InvestmentHoldingSnapshot.source,
            func.max(InvestmentHoldingSnapshot.synced_at).label("max_synced_at"),
        )
        .group_by(InvestmentHoldingSnapshot.source)
        .subquery()
    )
    rows = (
        db.query(
            InvestmentHoldingSnapshot,
            (InvestmentHoldingSnapshot._value * lr.c.rate).label("converted_value"),
            (InvestmentHoldingSnapshot._cost_basis * lr.c.rate).label("converted_cost_basis"),
        )
        .join(
            latest_times,
            (InvestmentHoldingSnapshot.source == latest_times.c.source)
            & (InvestmentHoldingSnapshot.synced_at == latest_times.c.max_synced_at),
        )
        .join(lr, and_(
            lr.c.from_currency == InvestmentHoldingSnapshot.currency,
            lr.c.to_currency == literal(currency),
        ))
        .order_by(
            InvestmentHoldingSnapshot.source.asc(),
            InvestmentHoldingSnapshot.created_at.desc(),
        )
        .all()
    )

    holdings: list[dict] = []
    accounts_by_key: dict[tuple[str, str | None], dict] = {}

    for row in rows:
        snapshot = row.InvestmentHoldingSnapshot
        connected = db.query(ConnectedAccount).filter(
            ConnectedAccount.external_account_id == snapshot.plaid_account_id,
            ConnectedAccount.source == snapshot.source,
        ).first()
        holdings.append(
            {
                "account_id": snapshot.plaid_account_id,
                "account_key": connected.id if connected else None,
                "security_id": snapshot.security_id,
                "ticker": snapshot.ticker,
                "name": snapshot.name,
                "quantity": float(snapshot.quantity or 0),
                "price": float(snapshot.price or 0),
                "value": round(float(row.converted_value or 0), 2),
                "cost_basis": round(float(row.converted_cost_basis or 0), 2) if row.converted_cost_basis is not None else None,
                "type": snapshot.type,
                "source": snapshot.source,
                "synced_at": snapshot.synced_at.isoformat() if snapshot.synced_at else None,
            }
        )
        account_key = (snapshot.source, snapshot.plaid_account_id)
        if account_key not in accounts_by_key:
            accounts_by_key[account_key] = {
                "account_id": snapshot.plaid_account_id,
                "name": snapshot.account_name,
                "source": snapshot.source,
                "synced_at": snapshot.synced_at.isoformat() if snapshot.synced_at else None,
            }

    return {
        "holdings": holdings,
        "accounts": list(accounts_by_key.values()),
    }


def _investment_holdings_history(db: Session, currency: str = "USD") -> list[dict]:
    """Read persisted investment balances and period facts without reconstruction."""
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    rows = (
        db.query(
            SourceBalanceHistory,
            (SourceBalanceHistory._value * lr.c.rate).label("value"),
            InvestmentPeriodFact,
            (InvestmentPeriodFact._beginning_value * lr.c.rate).label("beginning_value"),
            (InvestmentPeriodFact._ending_value * lr.c.rate).label("ending_value"),
            (InvestmentPeriodFact._inflow * lr.c.rate).label("inflow"),
            (InvestmentPeriodFact._market_gain * lr.c.rate).label("market_gain"),
        )
        .join(lr, and_(lr.c.from_currency == SourceBalanceHistory.currency, lr.c.to_currency == literal(currency)))
        .outerjoin(InvestmentPeriodFact, and_(
            SourceBalanceHistory.account_key.is_(None),
            InvestmentPeriodFact.source == SourceBalanceHistory.source,
            InvestmentPeriodFact.period_end == SourceBalanceHistory.date,
            InvestmentPeriodFact.currency == SourceBalanceHistory.currency,
        ))
        .filter(SourceBalanceHistory.account_group.in_(["investment", "brokerage"]))
        .order_by(SourceBalanceHistory.date.asc(), SourceBalanceHistory.source.asc())
        .all()
    )
    history = []
    for row in rows:
        item = {
            "synced_at": f"{row.SourceBalanceHistory.date.isoformat()}T12:00:00",
            "source": row.SourceBalanceHistory.source,
            "account_key": row.SourceBalanceHistory.account_key,
            "account_name": row.SourceBalanceHistory.account_name,
            "value": round(float(row.value or 0), 2),
        }
        if row.InvestmentPeriodFact is not None:
            for name in ("beginning_value", "ending_value", "inflow", "market_gain"):
                value = getattr(row, name)
                if value is not None:
                    item[name] = round(float(value), 2)
        history.append(item)
    return history


def _current_retirement_payload(db: Session) -> dict:
    retirement_sources = _get_retirement_import_sources()
    logs = (
        db.query(SyncLog)
        .filter(
            SyncLog.sync_type == "import_retirement_csv",
            SyncLog.status == "success",
            SyncLog.source.in_(retirement_sources) if retirement_sources else SyncLog.source == "__none__",
        )
        .order_by(SyncLog.created_at.asc())
        .all()
    )
    if logs:
        merged: list[dict] = []
        seen_ids: set[str] = set()
        for log in logs:
            payload = (log.extra_data or {}).get("payload") or {}
            for txn in payload.get("transactions", []):
                source_id = txn.get("source_id")
                if source_id and source_id in seen_ids:
                    continue
                if source_id:
                    seen_ids.add(source_id)
                merged.append(txn)
        merged.sort(key=lambda txn: (txn["date"], txn["type"], txn["source"], txn["fund"], txn.get("source_id", "")))
        return {
            "transactions": merged,
            "summary": summarize_retirement_transactions(merged),
        }
    return {"transactions": [], "summary": {}}


def _refresh_sidebar_snapshots(
    db: Session,
    logs: list[SyncLog] | None = None,
    errors: list[dict] | None = None,
) -> list[dict]:
    if settings.is_demo:
        return []

    synced_at = datetime.utcnow()
    ledger_balances = _transaction_balance_map(db)
    snapshots: list[dict] = []
    plaid_logs = logs if logs is not None else _connected_plaid_logs(db)

    for log in plaid_logs:
        if blocked_error(log):
            continue
        institution = (log.extra_data or {}).get("institution_name", "Unknown")
        access_token = (log.extra_data or {}).get("access_token")
        if not access_token:
            continue
        extra_data = dict(log.extra_data or {})
        last_snapshot_refresh_at = extra_data.get("last_snapshot_refresh_at")
        should_refresh = True
        if last_snapshot_refresh_at:
            try:
                last_dt = datetime.fromisoformat(last_snapshot_refresh_at.replace("Z", "+00:00"))
                should_refresh = datetime.utcnow() - last_dt.replace(tzinfo=None) >= SNAPSHOT_REFRESH_INTERVAL
            except Exception:
                should_refresh = True
        if not db.query(ConnectedAccount.id).filter(ConnectedAccount.sync_log_id == log.id).first():
            should_refresh = True
        if extra_data.get("sync_stage_errors") or extra_data.get("last_sync_error"):
            should_refresh = True
        if not should_refresh:
            continue

        holdings_succeeded = False
        usage_id = None
        holdings = None
        if not _item_supports_investments(log):
            # Transactions-only Item: asking for holdings just produces a recurring
            # ADDITIONAL_CONSENT_REQUIRED error and burns an API call.
            pass
        else:
          try:
            usage_id = record_plaid_usage(
                db,
                endpoint="investments_holdings_get",
                institution=institution,
                plaid_item_id=log.plaid_item_id,
                metadata={"status": "attempted"},
            )
            # Commit first: never hold SQLite's write lock across a network call.
            db.commit()
            holdings = get_investment_holdings(access_token)
            finish_plaid_usage(db, usage_id, success=True)
            holdings_succeeded = True
            record_success(log, "holdings")
            extra_data = dict(log.extra_data or {})
          except Exception as exc:
            if usage_id:
                finish_plaid_usage(db, usage_id, success=False, error_code=type(exc).__name__)
            payload = _plaid_sync_error_payload(exc)
            holdings = None
            if payload.get("code") in _NO_INVESTMENTS_ERROR_CODES:
                # Remember it so later syncs skip the call instead of re-reporting a
                # permanent property of the Item as a sync failure.
                logger.info("%s has no investments product; skipping holdings from now on", institution)
                extra_data["investments_unavailable"] = payload.get("code")
                log.extra_data = extra_data
            else:
                logger.exception("Plaid holdings refresh failed for %s", institution)
                error = {"source": institution, "stage": "holdings", **payload}
                if errors is not None:
                    errors.append(error)
                record_error(log, "holdings", error)
                extra_data = dict(log.extra_data or {})
                if blocked_error(log):
                    continue
        if holdings:
            _persist_investment_holdings_snapshot(
                db,
                source=institution,
                holdings=holdings.get("holdings", []),
                accounts=holdings.get("accounts", []),
                synced_at=synced_at,
            )
        investment_value = round(
            sum(float(item.get("value") or 0) for item in (holdings or {}).get("holdings", [])),
            2,
        )

        usage_id = None
        try:
            usage_id = record_plaid_usage(
                db,
                endpoint="accounts_balance_get",
                institution=institution,
                plaid_item_id=log.plaid_item_id,
                metadata={"status": "attempted"},
            )
            # Commit first: never hold SQLite's write lock across a network call.
            db.commit()
            accounts = get_account_balances(access_token)
            finish_plaid_usage(db, usage_id, success=True)
            record_success(log, "balances")
            extra_data = dict(log.extra_data or {})
        except Exception as exc:
            if usage_id:
                finish_plaid_usage(db, usage_id, success=False, error_code=type(exc).__name__)
            logger.exception("Plaid balance refresh failed for %s", institution)
            error = {"source": institution, "stage": "balances", **_plaid_sync_error_payload(exc)}
            if errors is not None:
                errors.append(error)
            record_error(log, "balances", error)
            extra_data = dict(log.extra_data or {})
            accounts = []

        if accounts:
            extra_data["account_fingerprints"] = _account_fingerprints(accounts)

        connected_accounts = _upsert_connected_accounts(db, log, institution, accounts, synced_at)
        holdings_by_account: dict[str, float] = {}
        for holding in (holdings or {}).get("holdings", []):
            external_id = str(holding.get("account_id") or "")
            holdings_by_account[external_id] = holdings_by_account.get(external_id, 0) + float(holding.get("value") or 0)

        for account in accounts:
            external_id = str(account.get("account_id") or "")
            connected = connected_accounts.get(external_id)
            if connected is None:
                continue
            raw_value = holdings_by_account.get(external_id)
            if raw_value is None:
                raw_value = account.get("current")
            if raw_value is None:
                continue
            current_value = round(_signed_snapshot_value(connected.account_group, float(raw_value)), 2)
            persisted = _persist_account_snapshot(
                db,
                source=institution,
                account_key=connected.id,
                account_name=connected.display_name,
                account_group=connected.account_group,
                connection_state="plaid",
                current_value=current_value,
                currency=connected.currency,
                synced_at=synced_at,
            )
            if persisted:
                snapshots.append({
                    "source": connected.display_name,
                    "account_key": connected.id,
                    "group": connected.account_group,
                    "balance": current_value,
                })

        plugin = get_all_sources().get(classify_source(institution))
        if accounts and (holdings_succeeded or not _item_supports_investments(log)):
            extra_data["last_snapshot_refresh_at"] = datetime.utcnow().isoformat()
            log.extra_data = extra_data

    return snapshots


def _refresh_investment_holdings(db: Session, logs: list[SyncLog] | None = None) -> dict:
    if settings.is_demo:
        return {"holdings": [], "accounts": []}

    synced_at = datetime.utcnow()
    plaid_logs = logs if logs is not None else _connected_plaid_logs(db)
    all_holdings: list[dict] = []
    all_accounts: list[dict] = []

    for log in plaid_logs:
        if blocked_error(log):
            continue
        institution = (log.extra_data or {}).get("institution_name", "Unknown")
        access_token = (log.extra_data or {}).get("access_token")
        if not access_token:
            continue
        if not _item_supports_investments(log):
            continue
        try:
            # Commit first: never hold SQLite's write lock across a network call.
            db.commit()
            data = get_investment_holdings(access_token)
            record_plaid_usage(
                db,
                endpoint="investments_holdings_get",
                institution=institution,
                plaid_item_id=log.plaid_item_id,
            )
        except Exception:
            continue
        holdings = data.get("holdings", [])
        accounts = data.get("accounts", [])
        if not holdings:
            continue
        _persist_investment_holdings_snapshot(
            db,
            source=institution,
            holdings=holdings,
            accounts=accounts,
            synced_at=synced_at,
        )
        for holding in holdings:
            all_holdings.append({**holding, "source": institution, "synced_at": synced_at.isoformat()})
        for account in accounts:
            all_accounts.append({**account, "source": institution, "synced_at": synced_at.isoformat()})

    return {"holdings": all_holdings, "accounts": all_accounts}


def _fallback_icon(source: str) -> str:
    from pathlib import Path
    icons_dir = Path(__file__).resolve().parents[3] / "plugins" / "icons"
    slug = source.lower().split()[0]
    candidates = [f"{slug}.png", f"{source.lower().replace(' ', '_')}.png"]
    for name in candidates:
        if (icons_dir / name).exists():
            return f"/api/plugin-icons/{name}"
    return ""


def _build_sidebar_accounts(db: Session, target_currency: str = "USD") -> list[dict]:
    if settings.is_demo:
        return get_demo_sidebar_accounts()

    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)

    # Build rate map for snapshot currency conversion
    rate_rows = db.query(lr.c.from_currency, lr.c.rate).filter(lr.c.to_currency == target_currency).all()
    rate_map: dict[str, float] = {row[0]: float(row[1]) for row in rate_rows}

    ledger_balances = _transaction_balance_map(db, target_currency=target_currency)
    latest_values = latest_account_values(db)
    connected_logs = _connected_plaid_logs(db)
    connected_by_source = {
        (log.extra_data or {}).get("institution_name", "Unknown"): log
        for log in connected_logs
    }

    # Normalize source names: merge aliases into canonical plugin labels
    all_plugins = get_all_sources()
    alias_to_label: dict[str, str] = {}
    for plugin in all_plugins.values():
        for alias in plugin.source_aliases:
            alias_to_label[alias] = plugin.label
        alias_to_label[plugin.label] = plugin.label

    def _canonical(source_name: str) -> str:
        return alias_to_label.get(source_name, source_name)

    # Merge aliases into one canonical valuation row.
    merged_values: dict[str, "SourceBalanceHistory"] = {}
    for source, value_row in latest_values.items():
        if value_row.account_key:
            continue
        canon = _canonical(source)
        existing = merged_values.get(canon)
        if existing is None or (value_row.date, value_row.created_at) > (existing.date, existing.created_at):
            merged_values[canon] = value_row
    latest_values = merged_values

    # Merge ledger balances by canonical name (sum if multiple aliases)
    merged_ledger: dict[str, float] = {}
    for source, bal in ledger_balances.items():
        canon = _canonical(source)
        merged_ledger[canon] = merged_ledger.get(canon, 0) + (bal or 0)
    ledger_balances = merged_ledger

    # Merge connected logs by canonical name
    merged_connected: dict[str, object] = {}
    for source, log in connected_by_source.items():
        merged_connected[_canonical(source)] = log
    connected_by_source = merged_connected

    sources = set(ledger_balances) | set(latest_values) | set(connected_by_source)
    investment_sources = _get_investment_sources()

    group_order = {"bank_account": 0, "credit_card": 1, "investment": 2, "retirement": 3}
    rows = []
    for source in sources:
        value_row = latest_values.get(source)
        log = connected_by_source.get(source)
        ledger_balance = ledger_balances.get(source)
        inferred_group = _classify_sidebar_group(source, ledger_balance=ledger_balance)
        # For investment and retirement sources, always use inferred group from registry
        source_key = classify_source(source)
        plugin = get_all_sources().get(source_key)
        force_inferred = (
            source in investment_sources
            or (plugin and plugin.domain in ("investments", "retirement"))
        )
        account_group = _normalize_sidebar_group(
            inferred_group
            if force_inferred
            else value_row.account_group if value_row else inferred_group
        )
        connection_state = "plaid" if log else "manual"

        # Convert snapshot balance to target currency via rate_map
        snapshot_currency = value_row.currency if value_row else (plugin.currency if plugin else "USD")
        raw_snapshot_balance = round(float(value_row._value), 2) if value_row and value_row._value is not None else None
        if raw_snapshot_balance is not None:
            snap_rate = rate_map.get(snapshot_currency, 1.0)
            snapshot_balance = round(raw_snapshot_balance * snap_rate, 2)
        else:
            snapshot_balance = None

        balance = snapshot_balance
        if balance is None:
            balance = ledger_balance

        rows.append(
            {
                "source": source,
                "source_key": source_key,
                "group": account_group,
                "connection_state": connection_state,
                "balance": round(balance, 2) if balance is not None else None,
                "ledger_balance": round(ledger_balance, 2) if ledger_balance is not None else None,
                "snapshot_balance": snapshot_balance,
                "currency": target_currency,
                "last_synced": (
                    value_row.created_at.isoformat()
                    if value_row and value_row.created_at
                    else (log.extra_data or {}).get("last_sync_at") if log else None
                ),
                "filter_source": source if account_group in {"bank_account", "credit_card"} and source in ledger_balances else None,
                "icon_url": plugin.icon_url if plugin else _fallback_icon(source),
            }
        )

    connected = db.query(ConnectedAccount).join(SyncLog, SyncLog.id == ConnectedAccount.sync_log_id).filter(
        ConnectedAccount.active.is_(True), SyncLog.status == "connected",
    ).all()
    represented = {classify_source(account.source) for account in connected}
    rows = [row for row in rows if row["source_key"] not in represented]
    for account in connected:
        value = db.query(
            SourceBalanceHistory, (SourceBalanceHistory._value * lr.c.rate).label("converted"),
        ).join(lr, and_(lr.c.from_currency == SourceBalanceHistory.currency, lr.c.to_currency == literal(target_currency))).filter(
            SourceBalanceHistory.account_key == account.id,
        ).order_by(SourceBalanceHistory.date.desc(), SourceBalanceHistory.created_at.desc()).first()
        ledger = db.query(func.sum(Transaction._amount * lr.c.rate)).join(
            lr, and_(lr.c.from_currency == Transaction.currency, lr.c.to_currency == literal(target_currency)),
        ).filter(Transaction.plaid_account_id == account.external_account_id).scalar()
        plugin = all_plugins.get(account.source_key)
        balance = round(float(value.converted), 2) if value else None
        rows.append({
            "source": account.display_name, "source_key": account.source_key,
            "account_key": account.id, "provider_source": account.source,
            "account_last4": account.mask,
            "group": account.account_group, "connection_state": "plaid",
            "balance": balance, "snapshot_balance": balance,
            "ledger_balance": round(float(ledger), 2) if ledger is not None else None,
            "currency": target_currency, "filter_source": f"account:{account.id}",
            "last_synced": account.last_seen_at.isoformat(),
            "icon_url": plugin.icon_url if plugin else "",
        })
    rows.sort(key=lambda row: (group_order.get(row["group"], 99), row["source"]))
    return rows


@router.post("/csv")
async def upload_csv(
    file: UploadFile = File(...),
    source: str = Form(...),
    db: Session = Depends(get_db),
):
    """Import CSV file."""
    try:
        preview = preview_import(
            db,
            file_bytes=await file.read(),
            filename=file.filename or "upload.csv",
            source=source,
            kind="csv",
        )
        result = commit_import(db, preview["import_id"])
        return {
            "status": result["status"],
            "imported": result["imported"],
            "skipped": result["skipped"],
            "duplicates": result.get("duplicate_count", 0),
            "import_id": result["import_id"],
            "filename": file.filename,
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/plaid/link-token")
def get_link_token(products: str = "transactions", account_id: str | None = None, db: Session = Depends(get_db)):
    """Get Plaid Link token for frontend. Pass products=investments for investment accounts."""
    if settings.is_demo:
        return {"link_token": f"demo-link-{products.replace(',', '-')}"}
    try:
        access_token = None
        requested_products = products
        if account_id:
            log = db.query(SyncLog).filter(SyncLog.id == account_id, SyncLog.source == "plaid", SyncLog.status == "connected").first()
            if not log:
                raise HTTPException(status_code=404, detail="Plaid connection not found")
            access_token = (log.extra_data or {}).get("access_token")
            if not access_token:
                raise HTTPException(status_code=400, detail="Plaid access token missing for reconnect")
            existing_products = (log.extra_data or {}).get("plaid_products")
            if existing_products:
                requested_products = ",".join(existing_products)
            if (blocked_error(log) or {}).get("action") == "relink":
                access_token = None
        try:
            token = create_link_token(products=requested_products.split(","), access_token=access_token)
        except Exception as exc:
            code = _plaid_error_code(exc)
            if code == ITEM_GONE_ERROR_CODE and account_id:
                # Nothing to reconnect to. Flag the row so the UI offers the right action.
                _mark_item_gone(db, log, exc)
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This connection no longer exists at Plaid, so it cannot be "
                        "reconnected. Remove it here and add the institution again."
                    ),
                ) from exc
            if code:
                payload = _plaid_sync_error_payload(exc)
                raise HTTPException(
                    status_code=502,
                    detail=f"{code}: {payload.get('display_message') or payload.get('message')}",
                ) from exc
            raise
        return {"link_token": token}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/plaid/exchange")
def exchange_token(
    public_token: str,
    institution_name: str = "",
    institution_id: str | None = None,
    account_fingerprints: str = "[]",
    account_id: str | None = None,
    products: str = "transactions",
    db: Session = Depends(get_db),
):
    """Exchange public token and store access token."""
    if settings.is_demo:
        return {"status": "connected", "item_id": f"demo-{institution_name or 'plaid'}"}
    try:
        try:
            fingerprints = json.loads(account_fingerprints)
            if not isinstance(fingerprints, list):
                fingerprints = []
        except (TypeError, ValueError):
            fingerprints = []

        if account_id:
            existing_log = db.query(SyncLog).filter(
                SyncLog.id == account_id,
                SyncLog.source == "plaid",
                SyncLog.status == "connected",
            ).first()
            if not existing_log:
                raise HTTPException(status_code=404, detail="Plaid connection not found")
            extra = dict(existing_log.extra_data or {})
            if (blocked_error(existing_log) or {}).get("action") == "relink":
                if not institution_name or classify_source(institution_name) != classify_source(extra.get("institution_name", "")):
                    raise HTTPException(400, "Select the same institution when replacing this connection")
                replacement_token, replacement_id, _ = exchange_public_token(public_token)
                extra["access_token"] = replacement_token
                existing_log.plaid_item_id = replacement_id
                existing_log.plaid_cursor = None
                extra.pop("item_gone", None)
                extra.pop("last_investment_transactions_sync_at", None)
                extra.pop("investments_unavailable", None)
                record_plaid_usage(db, endpoint="item_public_token_exchange", institution=institution_name, plaid_item_id=replacement_id)
            if institution_name:
                extra["institution_name"] = institution_name
            if institution_id:
                extra["institution_id"] = institution_id
            if fingerprints:
                extra["account_fingerprints"] = fingerprints
            if products and not extra.get("plaid_products"):
                extra["plaid_products"] = [item for item in products.split(",") if item]
            extra.pop("last_snapshot_refresh_at", None)
            extra.pop("last_sync_error", None)
            extra.pop("sync_stage_errors", None)
            extra.pop("retry_after", None)
            extra.pop("retry_count", None)
            existing_log.extra_data = extra
            db.commit()
            return {"status": "connected", "item_id": existing_log.plaid_item_id}

        access_token, item_id, _ = exchange_public_token(public_token)

        if not account_id and _has_duplicate_connection(
            db,
            institution_name=institution_name,
            institution_id=institution_id,
            fingerprints=fingerprints,
        ):
            # Plaid can hand back the *same* Item for a re-link of an institution that is
            # already connected. Removing it then destroys the live connection and leaves the
            # stored token pointing at nothing, which surfaces later as ITEM_NOT_FOUND on a
            # row still marked "connected". Only revoke a genuinely new Item.
            already_known = bool(item_id) and db.query(SyncLog).filter(
                SyncLog.source == "plaid",
                SyncLog.plaid_item_id == item_id,
            ).first() is not None
            if not already_known:
                try:
                    remove_item(access_token, db=db, institution=institution_name)
                except Exception:
                    logger.warning("Could not revoke duplicate Plaid Item", exc_info=True)
            raise HTTPException(
                status_code=409,
                detail=f"{institution_name or 'This institution'} is already connected. Use Reconnect to update its accounts.",
            )

        existing_log = None
        if not existing_log and item_id:
            existing_log = db.query(SyncLog).filter(SyncLog.plaid_item_id == item_id, SyncLog.source == "plaid").order_by(SyncLog.created_at.desc()).first()

        if existing_log:
            extra = dict(existing_log.extra_data or {})
            extra["access_token"] = access_token
            if institution_name:
                extra["institution_name"] = institution_name
            if institution_id:
                extra["institution_id"] = institution_id
            if fingerprints:
                extra["account_fingerprints"] = fingerprints
            extra["plaid_products"] = [item for item in products.split(",") if item]
            existing_log.extra_data = extra
            extra.pop("last_snapshot_refresh_at", None)
            extra.pop("last_sync_error", None)
            existing_log.extra_data = extra
            existing_log.plaid_item_id = item_id
            existing_log.status = "connected"
            log = existing_log
        else:
            log = SyncLog(
                source="plaid",
                sync_type="plaid",
                plaid_item_id=item_id,
                status="connected",
                extra_data={
                    "access_token": access_token,
                    "institution_name": institution_name,
                    "institution_id": institution_id,
                    "account_fingerprints": fingerprints,
                    "plaid_products": [item for item in products.split(",") if item],
                },
            )
            db.add(log)
        record_plaid_usage(
            db,
            endpoint="item_public_token_exchange",
            institution=institution_name or None,
            plaid_item_id=item_id,
        )
        upsert_product_enrollments(
            db,
            item_id,
            [item for item in products.split(",") if item],
        )
        db.commit()
        
        return {"status": "connected", "item_id": item_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/plaid/sync")
def sync_plaid(db: Session = Depends(get_db)):
    """Sync transactions from all connected Plaid accounts."""
    if settings.is_demo:
        return {"status": "demo", "added": 0, "message": "Demo data is pre-seeded and isolated from live accounts"}
    with job_lock.try_acquire("plaid sync") as acquired:
        if not acquired:
            return {
                "status": "already_running",
                "added": 0,
                "message": f"Busy: {job_lock.current_holder() or 'another job'} is running",
            }
        return _sync_plaid_locked(db)


def _sync_plaid_locked(db: Session):
    logs = _connected_plaid_logs(db)
    
    if not logs:
        snapshots = _refresh_sidebar_snapshots(db, logs=[])
        db.commit()
        return {"status": "no_accounts", "message": "No Plaid accounts connected", "snapshots_refreshed": len(snapshots)}
    
    matcher = RuleMatcher()
    total_counts = {
        "added": 0,
        "updated": 0,
        "removed": 0,
        "pending_promotions": 0,
        "skipped_pending": 0,
        "skipped_statement_overlap": 0,
    }
    account_results = []
    sync_errors: list[dict] = []
    
    for log in logs:
        blocked = blocked_error(log)
        if blocked:
            sync_errors.append({"source": (log.extra_data or {}).get("institution_name", "Unknown"), **blocked})
            continue
        access_token = log.extra_data.get("access_token") if log.extra_data else None
        if not access_token:
            continue
        
        institution = (log.extra_data or {}).get("institution_name", "plaid")
        account_counts = {
            "added": 0,
            "updated": 0,
            "removed": 0,
            "pending_promotions": 0,
            "skipped_pending": 0,
            "skipped_statement_overlap": 0,
        }
        
        try:
          cursor = log.plaid_cursor
          has_more = True
          destination = _plaid_activity_destination(log)
          while has_more:
              # Release the write lock before every network round-trip. Usage telemetry and
              # the previous page's rows leave this session dirty, and holding SQLite's
              # single writer across a multi-second Plaid call is what made concurrent
              # requests fail with "database is locked".
              db.commit()
              result = sync_transactions(access_token, cursor)
              record_plaid_usage(
                  db,
                  endpoint="transactions_sync",
                  institution=institution,
                  plaid_item_id=log.plaid_item_id,
              )
              if destination == "account_activity":
                  counts = apply_account_activity_batch(
                      db=db,
                      institution=institution,
                      added=result["added"],
                      modified=result.get("modified", []),
                      removed=result.get("removed", []),
                  )
              else:
                  counts = apply_plaid_sync_batch(
                      db=db,
                      institution=institution,
                      added=result["added"],
                      modified=result.get("modified", []),
                      removed=result.get("removed", []),
                      matcher=matcher,
                  )
              for key, value in counts.as_dict().items():
                  total_counts[key] = total_counts.get(key, 0) + value
                  account_counts[key] = account_counts.get(key, 0) + value
              cursor = result["cursor"]
              has_more = result["has_more"]
              log.plaid_cursor = cursor
              db.commit()
          extra_data = dict(log.extra_data or {})
          plugin = get_all_sources().get(classify_source(institution))
          if plugin and plugin.domain == "investments" and _item_supports_investments(log):
              previous_investment_sync = extra_data.get("last_investment_transactions_sync_at")
              investment_start = date(2010, 1, 1)
              if previous_investment_sync:
                  investment_start = datetime.fromisoformat(previous_investment_sync.replace("Z", "+00:00")).date() - timedelta(days=7)
              db.commit()
              investment_transactions = get_investment_transactions(
                  access_token,
                  start_date=investment_start,
                  end_date=date.today(),
              )
              record_plaid_usage(
                  db,
                  endpoint="investments_transactions_get",
                  institution=institution,
                  plaid_item_id=log.plaid_item_id,
              )
              investment_counts = apply_account_activity_batch(
                  db=db,
                  institution=institution,
                  added=investment_transactions,
                  modified=[],
                  removed=[],
              )
              for key, value in investment_counts.as_dict().items():
                  total_counts[key] = total_counts.get(key, 0) + value
                  account_counts[key] = account_counts.get(key, 0) + value
              extra_data["last_investment_transactions_sync_at"] = datetime.utcnow().isoformat()
          extra_data["last_sync_at"] = datetime.utcnow().isoformat()
          extra_data["last_sync_counts"] = account_counts
          extra_data["activity_destination"] = destination
          log.extra_data = extra_data
          record_success(log, "transactions")
          log.plaid_cursor = cursor
          log.record_count = (log.record_count or 0) + account_counts["added"]
          log.status = "connected"
          db.commit()
          account_results.append({"source": institution, **account_counts})
        except Exception as exc:
          logger.exception("Plaid sync failed for %s", institution)
          db.rollback()
          extra_data = dict(log.extra_data or {})
          record_error(log, "transactions", _plaid_sync_error_payload(exc))
          extra_data = dict(log.extra_data or {})
          db.commit()
          sync_errors.append({"source": institution, "stage": "transactions", **extra_data["last_sync_error"]})
          account_results.append({
              "source": institution,
              "status": "error",
              "error": extra_data["last_sync_error"],
              **account_counts,
          })
          continue  # skip accounts that error (e.g. consent required)
    
    snapshots = _refresh_sidebar_snapshots(db, logs=logs, errors=sync_errors)
    db.commit()
    return {
        "status": "partial_error" if sync_errors else "synced",
        **total_counts,
        "accounts": account_results,
        "snapshots_refreshed": len(snapshots),
        "errors": sync_errors,
    }


@router.get("/status")
def sync_status(db: Session = Depends(get_db)):
    """Get sync status for all accounts."""
    logs = db.query(SyncLog).all()
    return {
        "accounts": [
            {
                "source": log.source,
                "account_id": None,
                "status": log.status,
                "last_sync": (log.extra_data or {}).get("last_sync_at"),
                "records_synced": int(log.record_count or 0),
            }
            for log in logs
        ]
    }


@router.get("/plaid/usage")
def get_plaid_usage(db: Session = Depends(get_db)):
    return plaid_usage_summary(db)


@router.get("/plaid/investments", response_model=InvestmentHoldingsResponse)
def get_investments(db: Session = Depends(get_db), refresh: bool = False, currency: str = Query(...)):
    """Return DB-backed investment holdings. Optional refresh pulls live Plaid first."""
    if settings.is_demo:
        result = get_demo_investments()
        result["currency"] = currency
        return result
    if refresh:
        _refresh_investment_holdings(db)
        db.commit()
    latest = _latest_investment_holdings(db, currency=currency)
    if not latest["holdings"]:
        # One-time bootstrap path so the schema addition does not render the page empty.
        _refresh_investment_holdings(db)
        db.commit()
        latest = _latest_investment_holdings(db, currency=currency)
    latest["currency"] = currency
    return latest


@router.get("/plaid/investments/history", response_model=InvestmentHistoryResponse)
def get_investment_history(db: Session = Depends(get_db), currency: str = Query(...)):
    if settings.is_demo:
        return {"currency": currency, "history": []}
    history = _investment_holdings_history(db, currency=currency)
    return {"currency": currency, "history": history}


@router.get("/plaid/investments/activity")
def get_investment_activity(
    source: str = Query(...),
    db: Session = Depends(get_db),
    currency: str = Query(...),
):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    account = db.get(ConnectedAccount, source)
    if account:
        activity_rows = db.query(AccountActivity, (AccountActivity._amount * lr.c.rate).label("converted")).join(
            lr, and_(lr.c.from_currency == AccountActivity.currency, lr.c.to_currency == literal(currency)),
        ).filter(AccountActivity.account_id == account.external_account_id).all()
        ledger_rows = db.query(Transaction, (Transaction._amount * lr.c.rate).label("converted")).join(
            lr, and_(lr.c.from_currency == Transaction.currency, lr.c.to_currency == literal(currency)),
        ).filter(Transaction.plaid_account_id == account.external_account_id).all()
        activity = [{"id": r.AccountActivity.id, "date": r.AccountActivity.date.isoformat(),
                     "description": r.AccountActivity.description, "merchant": r.AccountActivity.merchant,
                     "type": r.AccountActivity.activity_type, "amount": round(float(r.converted), 2),
                     "pending": r.AccountActivity.pending, "account_last4": r.AccountActivity.account_last4} for r in activity_rows]
        activity.extend({"id": r.Transaction.id, "date": r.Transaction.date.isoformat(),
                         "description": r.Transaction.merchant_raw, "merchant": r.Transaction.merchant_clean,
                         "type": "other", "amount": round(float(r.converted), 2),
                         "pending": r.Transaction.pending, "account_last4": r.Transaction.account_last4} for r in ledger_rows)
        return {"currency": currency, "activity": sorted(activity, key=lambda r: (r["date"], r["id"]), reverse=True)}
    rates = db.query(lr.c.from_currency, lr.c.rate).filter(lr.c.to_currency == currency).all()
    rate_map = {row[0]: float(row[1]) for row in rates}
    source_key = classify_source(source) or source
    rows = (
        db.query(AccountActivity)
        .filter((AccountActivity.source_key == source_key) | (AccountActivity.source == source))
        .order_by(AccountActivity.date.desc(), AccountActivity.created_at.desc())
        .all()
    )
    return {
        "currency": currency,
        "activity": [
            {
                "id": row.id,
                "date": row.date.isoformat(),
                "description": row.description,
                "merchant": row.merchant,
                "type": row.activity_type,
                "amount": round(float(row._amount or 0) * rate_map.get(row.currency, 1.0), 2),
                "pending": row.pending,
            }
            for row in rows
        ],
    }


@router.get("/connected")
def connected_accounts(db: Session = Depends(get_db)):
    """List all connected Plaid institutions (fast, no balance fetch)."""
    return [
        {"source": (log.extra_data or {}).get("institution_name", "Unknown"), "type": "plaid"}
        for log in _connected_plaid_logs(db)
    ]


@router.get("/sidebar-accounts", response_model=SidebarResponse)
def sidebar_accounts(db: Session = Depends(get_db), currency: str = Query(...)):
    """Return grouped, DB-backed sidebar account state."""
    accounts = _build_sidebar_accounts(db, target_currency=currency)
    # Ensure each account has currency set (demo accounts may omit it)
    for acct in accounts:
        if "currency" not in acct:
            acct["currency"] = currency
    # Show USD ↔ display currency rate (only if display != USD)
    exchange_rates = []
    if currency != "USD":
        ensure_rates_fresh(db)
        lr = latest_rate_subquery(db)
        rate_row = db.query(lr.c.rate).filter(
            lr.c.from_currency == "USD", lr.c.to_currency == currency
        ).first()
        if rate_row:
            exchange_rates = [{"from_currency": currency, "to": "USD", "rate": round(float(rate_row[0]), 2)}]

    return {"currency": currency, "accounts": accounts, "exchange_rates": exchange_rates}


@router.get("/plaid/balances", response_model=list[PlaidBalanceItem])
def plaid_balances(db: Session = Depends(get_db), all: bool = False, currency: str = Query(...)):
    """Fetch live Plaid balances. Default: only accounts without transactions. all=true: all accounts (parallel)."""
    if settings.is_demo:
        return [{**item, "currency": currency} for item in get_demo_plaid_balances()]

    import concurrent.futures
    from src.models import Transaction

    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    rate_rows = db.query(lr.c.from_currency, lr.c.rate).filter(lr.c.to_currency == currency).all()
    rate_map = {row[0]: float(row[1]) for row in rate_rows}

    txn_sources = {r[0] for r in db.query(Transaction.source).distinct().all()} if not all else set()
    inv_sources = _get_investment_sources()

    logs = db.query(SyncLog).filter(SyncLog.source == "plaid", SyncLog.status == "connected").all()
    to_fetch = []
    for log in logs:
        if blocked_error(log):
            continue
        institution = (log.extra_data or {}).get("institution_name", "Unknown")
        if institution in inv_sources:
            continue
        if not all and institution in txn_sources:
            continue
        access_token = (log.extra_data or {}).get("access_token")
        if access_token:
            to_fetch.append((institution, access_token))

    def fetch_one(args):
        institution, token = args
        try:
            accts = get_account_balances(token)
            raw_balance = sum(
                a["current"] * rate_map[a.get("iso_currency_code") or "USD"]
                for a in accts
                if a["current"] is not None
            )
            return {"source": institution, "balance": round(raw_balance, 2), "currency": currency}
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(to_fetch) or 1) as pool:
        results = pool.map(fetch_one, to_fetch)
    return [r for r in results if r]
