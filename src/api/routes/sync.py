from fastapi import APIRouter, Depends, Query, UploadFile, File, Form, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import tempfile
import os
import json

from sqlalchemy import and_, func, literal

from src.models import get_db, SyncLog, Transaction, AccountSnapshot, InvestmentHoldingSnapshot, ExchangeRate
from src.config import settings
from src.demo import get_demo_investments, get_demo_plaid_balances, get_demo_sidebar_accounts
from src.ingestion.plaid_client import create_link_token, exchange_public_token, sync_transactions, get_investment_holdings, get_account_balances
from src.ingestion.csv_importer import import_csv_file
from src.ingestion.import_service import commit_import, preview_import
from src.ingestion.plaid_sync import apply_plaid_sync_batch
from src.ingestion.plaid_usage import plaid_usage_summary, record_plaid_usage
from src.processing.categorizer import RuleMatcher
from src.processing.parse_retirement import summarize_retirement_transactions
from src.plugins.registry import get_all_sources, get_credit_card_sources, classify_source
from src.services.exchange_rates import latest_rate_subquery, ensure_rates_fresh
from src.api.schemas import (
    SidebarResponse,
    InvestmentHoldingsResponse,
    InvestmentHistoryResponse,
    PlaidBalanceItem,
)

router = APIRouter()
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


def _persist_account_snapshot(
    db: Session,
    *,
    source: str,
    account_group: str,
    connection_state: str,
    current_value: float,
    synced_at: datetime,
) -> None:
    db.add(
        AccountSnapshot(
            source=source,
            account_group=account_group,
            connection_state=connection_state,
            current_value=round(current_value, 2),
            synced_at=synced_at,
            created_at=synced_at,
        )
    )


def _latest_sidebar_snapshots(db: Session) -> dict[str, AccountSnapshot]:
    # Subquery to find the max synced_at per source
    latest_times = (
        db.query(
            AccountSnapshot.source,
            func.max(AccountSnapshot.synced_at).label("max_synced_at"),
        )
        .group_by(AccountSnapshot.source)
        .subquery()
    )
    rows = (
        db.query(AccountSnapshot)
        .join(
            latest_times,
            (AccountSnapshot.source == latest_times.c.source)
            & (AccountSnapshot.synced_at == latest_times.c.max_synced_at),
        )
        .all()
    )
    # If multiple snapshots share the same max synced_at for a source, pick by highest value
    latest: dict[str, AccountSnapshot] = {}
    for row in rows:
        existing = latest.get(row.source)
        if existing is None or (row._current_value or 0) > (existing._current_value or 0):
            latest[row.source] = row
    return latest


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
        holdings.append(
            {
                "account_id": snapshot.plaid_account_id,
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
    """Return investment holdings history with rate-converted values."""
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)

    manual_history: list[dict] = []
    manual_sources: set[tuple[str, str]] = set()
    logs = (
        db.query(SyncLog)
        .filter(SyncLog.sync_type == "import_investment_csv", SyncLog.status == "success")
        .order_by(SyncLog.created_at.asc())
        .all()
    )

    # Build rate map for manual (SyncLog) history entries
    rate_rows = db.query(lr.c.from_currency, lr.c.rate).filter(lr.c.to_currency == currency).all()
    rate_map: dict[str, float] = {row[0]: float(row[1]) for row in rate_rows}

    for log in logs:
        payload = (log.extra_data or {}).get("payload") or {}
        account = payload.get("account") or {}
        summary = payload.get("summary") or {}
        statement_date = str(account.get("statement_date") or "")
        source = str(log.source or "")
        if not statement_date or not source:
            continue
        synced_at = f"{statement_date}T12:00:00"
        # Determine source currency from plugin
        from src.plugins.registry import get_all_sources, classify_source
        source_key = classify_source(source)
        plugin = get_all_sources().get(source_key)
        src_currency = plugin.currency if plugin else "USD"
        rate = rate_map.get(src_currency, 1.0)

        ending_value = round(float(summary.get("ending_value") or summary.get("ending_net_value") or 0) * rate, 2)
        beginning_value = round(float(summary.get("beginning_value") or 0) * rate, 2)
        market_gain = round(float(summary.get("change_in_investment") or 0) * rate, 2)
        inflow = round(ending_value - beginning_value - market_gain, 2)
        manual_history.append(
            {
                "synced_at": synced_at,
                "source": source,
                "value": ending_value,
                "beginning_value": beginning_value,
                "market_gain": market_gain,
                "inflow": inflow,
                "ending_value": ending_value,
            }
        )
        manual_sources.add((source, synced_at))

    # Aggregate holdings values per (source, synced_at) with rate JOIN
    agg_rows = (
        db.query(
            InvestmentHoldingSnapshot.source,
            InvestmentHoldingSnapshot.synced_at,
            func.sum(InvestmentHoldingSnapshot._value * lr.c.rate).label("total_value"),
        )
        .join(lr, and_(
            lr.c.from_currency == InvestmentHoldingSnapshot.currency,
            lr.c.to_currency == literal(currency),
        ))
        .group_by(InvestmentHoldingSnapshot.source, InvestmentHoldingSnapshot.synced_at)
        .order_by(InvestmentHoldingSnapshot.synced_at.asc(), InvestmentHoldingSnapshot.source.asc())
        .all()
    )
    history = manual_history + [
        {"synced_at": row.synced_at.isoformat() if row.synced_at else "", "source": row.source, "value": round(float(row.total_value or 0), 2)}
        for row in agg_rows
        if (row.source, row.synced_at.isoformat() if row.synced_at else "") not in manual_sources
    ]
    history.sort(key=lambda item: (item["synced_at"], item["source"]))
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


def _refresh_sidebar_snapshots(db: Session, logs: list[SyncLog] | None = None) -> list[dict]:
    if settings.is_demo:
        return []

    synced_at = datetime.utcnow()
    ledger_balances = _transaction_balance_map(db)
    snapshots: list[dict] = []
    plaid_logs = logs if logs is not None else _connected_plaid_logs(db)

    for log in plaid_logs:
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
        if not should_refresh:
            continue

        try:
            holdings = get_investment_holdings(access_token)
            record_plaid_usage(
                db,
                endpoint="investments_holdings_get",
                institution=institution,
                plaid_item_id=log.plaid_item_id,
            )
        except Exception:
            holdings = None
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

        try:
            accounts = get_account_balances(access_token)
            record_plaid_usage(
                db,
                endpoint="accounts_balance_get",
                institution=institution,
                plaid_item_id=log.plaid_item_id,
            )
        except Exception:
            accounts = []

        non_investment_accounts = [
            account
            for account in accounts
            if account.get("current") is not None and account.get("type") != "investment"
        ]
        if non_investment_accounts:
            group = _classify_sidebar_group(
                institution,
                ledger_balance=ledger_balances.get(institution),
                plaid_account_types=[str(account.get("type") or "") for account in non_investment_accounts],
            )
            current_value = round(_signed_snapshot_value(
                group,
                sum(float(account["current"]) for account in non_investment_accounts if account.get("current") is not None),
            ), 2)
            # Known investment sources should prefer holdings value when available
            # rather than writing an additional same-source cash snapshot that can mask brokerage value.
            if institution not in _get_investment_sources() or investment_value <= 0:
                _persist_account_snapshot(
                    db,
                    source=institution,
                    account_group=group,
                    connection_state="plaid",
                    current_value=current_value,
                    synced_at=synced_at,
                )
                snapshots.append({"source": institution, "group": group, "balance": current_value})

        if investment_value:
            _persist_account_snapshot(
                db,
                source=institution,
                account_group="investment",
                connection_state="plaid",
                current_value=investment_value,
                synced_at=synced_at,
            )
            snapshots.append({"source": institution, "group": "investment", "balance": investment_value})

        extra_data["last_snapshot_refresh_at"] = datetime.utcnow().isoformat()
        log.extra_data = extra_data

    for p in get_all_sources().values():
        if p.domain != "retirement":
            continue
        source_names = {p.label} | set(p.source_aliases)
        ret_logs = (
            db.query(SyncLog)
            .filter(
                SyncLog.sync_type.in_(["import_retirement_csv", "import_retirement_statement_pdf"]),
                SyncLog.status == "success",
                SyncLog.source.in_(source_names),
            )
            .order_by(SyncLog.created_at.desc())
            .first()
        )
        if not ret_logs:
            continue
        payload = (ret_logs.extra_data or {}).get("payload") or {}
        statements = payload.get("statements") or []
        if statements:
            latest = max(statements, key=lambda s: str(s.get("period_end") or ""))
            balance = float(latest.get("ending_balance") or 0)
        else:
            txns = payload.get("transactions") or []
            summary = summarize_retirement_transactions(txns) if txns else {}
            balance = float(summary.get("balance") or 0)
        if balance:
            _persist_account_snapshot(
                db,
                source=p.label,
                account_group="retirement",
                connection_state="manual",
                current_value=balance,
                synced_at=synced_at,
            )
            snapshots.append({"source": p.label, "group": "retirement", "balance": round(balance, 2)})

    return snapshots


def _refresh_investment_holdings(db: Session, logs: list[SyncLog] | None = None) -> dict:
    if settings.is_demo:
        return {"holdings": [], "accounts": []}

    synced_at = datetime.utcnow()
    plaid_logs = logs if logs is not None else _connected_plaid_logs(db)
    all_holdings: list[dict] = []
    all_accounts: list[dict] = []

    for log in plaid_logs:
        institution = (log.extra_data or {}).get("institution_name", "Unknown")
        access_token = (log.extra_data or {}).get("access_token")
        if not access_token:
            continue
        try:
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
    latest_snapshots = _latest_sidebar_snapshots(db)
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

    # Merge snapshots that map to the same canonical source (keep the latest)
    merged_snapshots: dict[str, "AccountSnapshot"] = {}
    for source, snap in latest_snapshots.items():
        canon = _canonical(source)
        existing = merged_snapshots.get(canon)
        if existing is None or (snap.synced_at or "") > (existing.synced_at or ""):
            merged_snapshots[canon] = snap
    latest_snapshots = merged_snapshots

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

    sources = set(ledger_balances) | set(latest_snapshots) | set(connected_by_source)
    investment_sources = _get_investment_sources()

    group_order = {"bank_account": 0, "credit_card": 1, "investment": 2, "retirement": 3}
    rows = []
    for source in sources:
        snapshot = latest_snapshots.get(source)
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
        account_group = (
            inferred_group
            if force_inferred
            else snapshot.account_group if snapshot else inferred_group
        )
        connection_state = snapshot.connection_state if snapshot else ("plaid" if log else "manual")

        # Convert snapshot balance to target currency via rate_map
        snapshot_currency = getattr(snapshot, "currency", "USD") if snapshot else (plugin.currency if plugin else "USD")
        raw_snapshot_balance = round(float(snapshot._current_value), 2) if snapshot and snapshot._current_value is not None else None
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
                    snapshot.synced_at.isoformat()
                    if snapshot and snapshot.synced_at
                    else (log.extra_data or {}).get("last_sync_at") if log else None
                ),
                "filter_source": source if account_group in {"bank_account", "credit_card"} and source in ledger_balances else None,
                "icon_url": plugin.icon_url if plugin else _fallback_icon(source),
            }
        )

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
        if account_id:
            log = db.query(SyncLog).filter(SyncLog.id == account_id, SyncLog.source == "plaid", SyncLog.status == "connected").first()
            if not log:
                raise HTTPException(status_code=404, detail="Plaid connection not found")
            access_token = (log.extra_data or {}).get("access_token")
            if not access_token:
                raise HTTPException(status_code=400, detail="Plaid access token missing for reconnect")
        token = create_link_token(products=products.split(","), access_token=access_token)
        return {"link_token": token}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/plaid/exchange")
def exchange_token(public_token: str, institution_name: str = "", account_id: str | None = None, db: Session = Depends(get_db)):
    """Exchange public token and store access token."""
    if settings.is_demo:
        return {"status": "connected", "item_id": f"demo-{institution_name or 'plaid'}"}
    try:
        access_token, item_id, _ = exchange_public_token(public_token)

        existing_log = None
        if account_id:
            existing_log = db.query(SyncLog).filter(SyncLog.id == account_id, SyncLog.source == "plaid").first()
        if not existing_log and item_id:
            existing_log = db.query(SyncLog).filter(SyncLog.plaid_item_id == item_id, SyncLog.source == "plaid").order_by(SyncLog.created_at.desc()).first()

        if existing_log:
            extra = dict(existing_log.extra_data or {})
            extra["access_token"] = access_token
            if institution_name:
                extra["institution_name"] = institution_name
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
                extra_data={"access_token": access_token, "institution_name": institution_name},
            )
            db.add(log)
        record_plaid_usage(
            db,
            endpoint="item_public_token_exchange",
            institution=institution_name or None,
            plaid_item_id=item_id,
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
    
    for log in logs:
        access_token = log.extra_data.get("access_token") if log.extra_data else None
        if not access_token:
            continue
        
        institution = (log.extra_data or {}).get("institution_name", "plaid")
        cursor = log.plaid_cursor
        has_more = True
        account_counts = {
            "added": 0,
            "updated": 0,
            "removed": 0,
            "pending_promotions": 0,
            "skipped_pending": 0,
            "skipped_statement_overlap": 0,
        }
        
        try:
          while has_more:
            result = sync_transactions(access_token, cursor)
            record_plaid_usage(
                db,
                endpoint="transactions_sync",
                institution=institution,
                plaid_item_id=log.plaid_item_id,
            )
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
        
          extra_data = dict(log.extra_data or {})
          extra_data["last_sync_at"] = datetime.utcnow().isoformat()
          extra_data["last_sync_counts"] = account_counts
          extra_data.pop("last_sync_error", None)
          log.extra_data = extra_data
          log.plaid_cursor = cursor
          log.record_count = (log.record_count or 0) + account_counts["added"]
          log.status = "connected"
          account_results.append({"source": institution, **account_counts})
        except Exception as exc:
          extra_data = dict(log.extra_data or {})
          extra_data["last_sync_error"] = _plaid_sync_error_payload(exc)
          log.extra_data = extra_data
          account_results.append({
              "source": institution,
              "status": "error",
              "error": extra_data["last_sync_error"],
              **account_counts,
          })
          continue  # skip accounts that error (e.g. consent required)
    
    snapshots = _refresh_sidebar_snapshots(db, logs=logs)
    db.commit()
    return {"status": "synced", **total_counts, "accounts": account_results, "snapshots_refreshed": len(snapshots)}


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
