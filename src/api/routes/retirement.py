from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import and_, func, literal

from src.config import settings
from src.demo import get_demo_retirement
from src.models import SyncLog, ExchangeRate, get_db
from src.processing.parse_retirement import summarize_retirement_transactions
from src.plugins.registry import get_all_sources
from src.services.exchange_rates import latest_rate_subquery, ensure_rates_fresh
from src.api.schemas import RetirementResponse

router = APIRouter()


def _get_retirement_import_sources() -> set[str]:
    """Return all source aliases for retirement-domain plugins."""
    sources: set[str] = set()
    for plugin in get_all_sources().values():
        if plugin.domain == "retirement":
            sources.add(plugin.label)
            sources.update(plugin.source_aliases)
    return sources


def _retirement_name_variants(value: str | None) -> set[str]:
    raw = (value or "").strip()
    if not raw:
        return set()
    humanized = raw.replace("_", " ").replace("-", " ").strip()
    variants = {raw, humanized}
    lowered = humanized.lower()
    compact = lowered.replace("401k", "").replace("(", "").replace(")", "").strip()
    title = compact.title() if compact else raw
    if compact:
        variants.add(title)
        variants.add(f"{title} 401k")
        variants.add(f"401k ({title})")
    return {item for item in variants if item}


def _resolve_retirement_sources(source_key: str | None = None) -> set[str]:
    if not source_key:
        names: set[str] = set()
        for plugin in get_all_sources().values():
            if plugin.domain != "retirement":
                continue
            names |= _retirement_name_variants(plugin.label)
            for alias in plugin.source_aliases:
                names |= _retirement_name_variants(alias)
            names |= _retirement_name_variants(plugin.source_key)
        return names

    names = _retirement_name_variants(source_key)
    all_sources = get_all_sources()
    plugin = all_sources.get(source_key)
    if plugin:
        names |= _retirement_name_variants(plugin.label)
        for alias in plugin.source_aliases:
            names |= _retirement_name_variants(alias)
        names |= _retirement_name_variants(plugin.source_key)
    return names

def _imported_retirement(db: Session, source_key: str | None = None) -> dict:
    retirement_sources = _resolve_retirement_sources(source_key) if source_key else (_get_retirement_import_sources() | _resolve_retirement_sources())
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
    if not logs:
        return {"transactions": [], "summary": {}}

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
    summary = summarize_retirement_transactions(merged)
    return {
        "source_type": source_key,
        "transactions": merged,
        "summary": summary,
        "activity": _synthesize_retirement_activity(merged),
        "statements": _synthesize_retirement_statements(merged),
    }


def _imported_statement_retirement(db: Session, source_key: str) -> dict:
    source_names = _resolve_retirement_sources(source_key)

    logs = (
        db.query(SyncLog)
        .filter(
            SyncLog.source.in_(source_names),
            SyncLog.status == "success",
            SyncLog.sync_type.in_(["import_retirement_csv", "import_retirement_statement_pdf"]),
        )
        .order_by(SyncLog.created_at.asc())
        .all()
    )
    if not logs:
        return {"source_type": source_key, "plan_name": None, "summary": {}, "statements": [], "activity": []}

    plan_name: str | None = None
    history_date_range: str | None = None
    statements_by_end: dict[str, dict] = {}
    activity_by_id: dict[str, dict] = {}

    for log in logs:
        payload = (log.extra_data or {}).get("payload") or {}
        plan_name = plan_name or payload.get("plan_name")
        history_date_range = history_date_range or payload.get("date_range")
        for statement in payload.get("statements", []) or []:
            period_end = str(statement.get("period_end") or "")
            if period_end:
                statements_by_end[period_end] = statement
        for row in payload.get("activity", []) or []:
            key = f"{row.get('date')}|{row.get('investment')}|{row.get('transaction_type')}|{row.get('amount')}|{row.get('shares')}"
            activity_by_id.setdefault(key, row)

    statements = [statements_by_end[key] for key in sorted(statements_by_end)]
    activity = sorted(activity_by_id.values(), key=lambda row: (row["date"], row["transaction_type"], row["investment"]))

    summary = {}
    if statements:
        latest = statements[-1]
        total_employee = round(sum(float(row.get("employee_contributions") or 0) for row in statements), 2)
        total_employer = round(sum(float(row.get("employer_contributions") or 0) for row in statements), 2)
        total_market = round(sum(float(row.get("market_change") or 0) for row in statements), 2)
        summary = {
            "balance": float(latest.get("ending_balance") or 0),
            "employee_contributed": total_employee,
            "employer_match": total_employer,
            "total_contributed": round(total_employee + total_employer, 2),
            "gain": total_market,
            "vested_balance": float(latest.get("vested_balance") or 0),
            "rate_of_return": float(latest.get("rate_of_return") or 0),
            "date_range": [str(statements[0].get("period_start") or ""), str(latest.get("period_end") or "")],
            "latest_statement_end": str(latest.get("period_end") or ""),
        }

    return {
        "source_type": source_key,
        "plan_name": plan_name,
        "history_date_range": history_date_range,
        "summary": summary,
        "statements": statements,
        "activity": activity,
    }


def _is_employer_contribution_source(source: str | None) -> bool:
    label = (source or "").lower()
    return "match" in label or "employer" in label or "company" in label


def _synthesize_retirement_activity(transactions: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for txn in transactions:
        amount = float(txn.get("amount") or 0)
        rows.append({
            "date": str(txn.get("date") or ""),
            "investment": str(txn.get("fund") or txn.get("source") or "Retirement"),
            "transaction_type": str(txn.get("type") or txn.get("source") or "Contribution"),
            "amount": round(amount, 2),
            "shares": round(float(txn.get("units") or 0), 6),
            "unit_price": round(float(txn.get("unit_price") or 0), 6),
        })
    rows.sort(key=lambda row: (row["date"], row["transaction_type"], row["investment"]))
    return rows


def _synthesize_retirement_statements(transactions: list[dict]) -> list[dict]:
    if not transactions:
        return []
    monthly: dict[str, dict] = {}
    running_units = 0.0
    current_price = 0.0
    running_balance = 0.0

    ordered = sorted(transactions, key=lambda txn: (str(txn.get("date") or ""), str(txn.get("type") or ""), str(txn.get("fund") or "")))
    for txn in ordered:
        txn_date = str(txn.get("date") or "")
        if not txn_date:
            continue
        month = txn_date[:7]
        amount = float(txn.get("amount") or 0)
        units = float(txn.get("units") or 0)
        current_price = float(txn.get("unit_price") or current_price or 0)
        beginning_balance = running_balance
        running_units += units
        running_balance = running_units * current_price

        bucket = monthly.setdefault(month, {
            "period_start": f"{month}-01",
            "period_end": txn_date,
            "beginning_balance": beginning_balance,
            "employee_contributions": 0.0,
            "employer_contributions": 0.0,
            "market_change": 0.0,
            "ending_balance": running_balance,
            "vested_balance": running_balance,
            "rate_of_return": 0.0,
            "source_file": "imported_history",
        })
        bucket["period_end"] = txn_date
        if amount > 0:
            if _is_employer_contribution_source(str(txn.get("source") or "")):
                bucket["employer_contributions"] += amount
            else:
                bucket["employee_contributions"] += amount
        elif amount < 0:
            bucket["market_change"] += amount
        bucket["ending_balance"] = running_balance
        bucket["vested_balance"] = running_balance

    statements = []
    prior_ending = 0.0
    for month in sorted(monthly):
        row = monthly[month]
        row["beginning_balance"] = round(prior_ending, 2)
        contributions = row["employee_contributions"] + row["employer_contributions"]
        row["market_change"] = round(row["ending_balance"] - row["beginning_balance"] - contributions, 2)
        denom = row["beginning_balance"] + contributions
        row["rate_of_return"] = round((row["market_change"] / denom), 4) if denom else 0.0
        row["employee_contributions"] = round(row["employee_contributions"], 2)
        row["employer_contributions"] = round(row["employer_contributions"], 2)
        row["ending_balance"] = round(row["ending_balance"], 2)
        row["vested_balance"] = round(row["vested_balance"], 2)
        statements.append(row)
        prior_ending = row["ending_balance"]
    return statements


@router.get("/", response_model=RetirementResponse)
def get_retirement(
    source: str = Query(None),
    currency: str = Query(...),
    db: Session = Depends(get_db),
):
    if settings.is_demo:
        result = get_demo_retirement()
        result["currency"] = currency
        return result

    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    # Build rate map for converting retirement amounts
    rate_rows = db.query(lr.c.from_currency, lr.c.rate).filter(lr.c.to_currency == currency).all()
    rate_map: dict[str, float] = {row[0]: float(row[1]) for row in rate_rows}

    all_sources = get_all_sources()
    retirement_sources = [k for k, p in all_sources.items() if p.domain == "retirement"]
    if source and source in all_sources:
        plugin = all_sources[source]
        source_currency = plugin.currency if plugin else "USD"
        rate = rate_map.get(source_currency, 1.0)
        if "retirement_statement_pdf" in plugin.allowed_kinds:
            result = _imported_statement_retirement(db, source)
            if result.get("statements") or result.get("activity"):
                return _convert_retirement_result(result, rate, currency)
        result = _imported_retirement(db, source_key=source)
        return _convert_retirement_result(result, rate, currency)
    if not source and retirement_sources:
        source = retirement_sources[0]
    if source:
        plugin = all_sources.get(source)
        source_currency = plugin.currency if plugin else "USD"
        rate = rate_map.get(source_currency, 1.0)
        result = _imported_retirement(db, source_key=source)
        return _convert_retirement_result(result, rate, currency)
    return {"currency": currency, "transactions": [], "summary": {}}


def _convert_retirement_result(result: dict, rate: float, target_currency: str = "USD") -> dict:
    """Apply currency conversion rate to retirement result amounts."""
    result["currency"] = target_currency
    if rate == 1.0:
        return result
    # Convert summary amounts
    if result.get("summary"):
        summary = result["summary"]
        for key in ("balance", "employee_contributed", "employer_match", "total_contributed", "gain", "vested_balance"):
            if key in summary and summary[key] is not None:
                summary[key] = round(float(summary[key]) * rate, 2)
    # Convert statement amounts
    for stmt in result.get("statements", []):
        for key in ("ending_balance", "beginning_balance", "employee_contributions", "employer_contributions", "market_change", "vested_balance"):
            if key in stmt and stmt[key] is not None:
                stmt[key] = round(float(stmt[key]) * rate, 2)
    # Convert transaction amounts
    for txn in result.get("transactions", []):
        if "amount" in txn and txn["amount"] is not None:
            txn["amount"] = round(float(txn["amount"]) * rate, 2)
    for row in result.get("activity", []):
        if "amount" in row and row["amount"] is not None:
            row["amount"] = round(float(row["amount"]) * rate, 2)
    return result
