from collections import defaultdict
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, literal
from datetime import date, timedelta
from typing import Optional

from src.models import get_db, Transaction, AccountSnapshot, ExchangeRate, SourceBalanceHistory
from src.processing.subscriptions import detect_subscriptions, get_monthly_subscription_total
from src.plugins.registry import get_all_sources, get_credit_card_sources, classify_source, get_source_label
from src.services.exchange_rates import latest_rate_subquery, ensure_rates_fresh
from src.api.schemas import (
    SummaryResponse,
    CategoryBreakdownItem,
    MerchantBreakdownItem,
    AccountBalanceItem,
    NetWorthResponse,
)

router = APIRouter()
INTERNAL_TRANSFER_WINDOW_DAYS = 10
CORE_EXPENSE_EXCLUDED_TOP_CATEGORIES = {"Income", "Salary", "Investment", "Remittance", "Credit Card"}

def _parse_dates(start_date: Optional[str], end_date: Optional[str]) -> tuple[date, date]:
    today = date.today()
    end = date.fromisoformat(end_date) if end_date else today
    start = date.fromisoformat(start_date) if start_date else (today - timedelta(days=365))
    return start, end


def _apply_source_filter(query, source: Optional[str]):
    if source:
        sources = [s.strip() for s in source.split(',')]
        query = query.filter(Transaction.source.in_(sources))
    return query


def _apply_category_filter(query, category: Optional[str]):
    if category:
        query = query.filter(Transaction.category.like(f"{category}%"))
    return query


def _effective_date_expr():
    return func.coalesce(Transaction.authorized_date, Transaction.date)


def _effective_date(txn: Transaction) -> date:
    return txn.authorized_date or txn.date


def _core_expense_match(category: str | None, amount: float, include_rent: bool) -> bool:
    top = _top_category(category)
    if top in CORE_EXPENSE_EXCLUDED_TOP_CATEGORIES:
        return False
    if not include_rent and top == "Rent":
        return False
    return True


def _cc_transfer_subcategory(txn: Transaction) -> Optional[str]:
    category = txn.category or ""
    if not category.startswith("Credit Card/"):
        return None
    _, sub = category.split("/", 1)
    return sub or None


def _top_category(category: str | None) -> str:
    return (category or "Uncategorized").split("/", 1)[0]


def _match_internal_transfer_ids(txns: list[Transaction]) -> tuple[set[str], float]:
    """Match mirrored credit-card payment legs across accounts."""
    negatives: dict[tuple[str, int], list[Transaction]] = defaultdict(list)
    positives: dict[tuple[str, int], list[Transaction]] = defaultdict(list)

    for txn in txns:
        sub = _cc_transfer_subcategory(txn)
        if not sub:
            continue
        amount = round(float(txn._amount), 2)
        cents = int(round(abs(amount) * 100))
        key = (sub, cents)
        if amount < 0:
            negatives[key].append(txn)
        elif amount > 0:
            positives[key].append(txn)

    matched_ids: set[str] = set()
    matched_total = 0.0
    for key, pos_items in positives.items():
        neg_items = negatives.get(key)
        if not neg_items:
            continue
        pos_sorted = sorted(pos_items, key=lambda t: (_effective_date(t), t.id))
        remaining_negs = sorted(neg_items, key=lambda t: (_effective_date(t), t.id))
        for pos_txn in pos_sorted:
            match_index = None
            for idx, neg_txn in enumerate(remaining_negs):
                if neg_txn.source == pos_txn.source:
                    continue
                if abs((_effective_date(pos_txn) - _effective_date(neg_txn)).days) > INTERNAL_TRANSFER_WINDOW_DAYS:
                    continue
                match_index = idx
                break
            if match_index is None:
                continue
            neg_txn = remaining_negs.pop(match_index)
            matched_ids.add(pos_txn.id)
            matched_ids.add(neg_txn.id)
            matched_total += key[1] / 100

    return matched_ids, round(matched_total, 2)


def _classify_balance_source(source: str, current_balance: float) -> str:
    credit_card_labels = get_credit_card_sources()
    if source in credit_card_labels:
        return "credit_cards"
    # Check via registry key lookup as well
    source_key = classify_source(source)
    plugin = get_all_sources().get(source_key)
    if plugin and plugin.is_credit_card:
        return "credit_cards"
    if current_balance < 0:
        return "credit_cards"
    return "bank_accounts"


def _classify_snapshot_group(source: str, account_group: str | None) -> str:
    # Try registry-based classification first
    source_key = classify_source(source)
    plugin = get_all_sources().get(source_key)

    if account_group == "retirement" or (plugin and plugin.domain == "retirement"):
        return "retirement"
    if account_group == "credit_card":
        return "credit_cards"
    if account_group == "investment" or (plugin and plugin.domain == "investments"):
        return "brokerage"
    if account_group == "bank_account":
        return "bank_accounts"
    return "bank_accounts"


def _normalize_history_group(source: str, account_group: str | None) -> str:
    if account_group in {"bank_accounts", "credit_cards", "cash_like", "brokerage", "retirement"}:
        return account_group
    return _classify_snapshot_group(source, account_group)


def _same_source_label(left: str, right: str) -> bool:
    a = (left or "").strip().lower()
    b = (right or "").strip().lower()
    return bool(a and b and (a == b or a in b or b in a))


def _get_converted_amount(db: Session, txn: Transaction, lr_subq, target_currency: str) -> float:
    from_currency = getattr(txn, "currency", None) or "USD"
    if "rates" not in db.info:
        db.info["rates"] = {}
    cache_key = (from_currency, target_currency)
    if cache_key in db.info["rates"]:
        return float(txn._amount) * db.info["rates"][cache_key]
    rate_row = (
        db.query(lr_subq.c.rate)
        .filter(
            lr_subq.c.from_currency == from_currency,
            lr_subq.c.to_currency == target_currency,
        )
        .first()
    )
    rate = float(rate_row[0]) if rate_row else 0.0
    db.info["rates"][cache_key] = rate
    return float(txn._amount) * rate


@router.get("/summary", response_model=SummaryResponse)
def get_summary(
    db: Session = Depends(get_db),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    source: Optional[str] = None,
    category: Optional[str] = None,
    currency: str = Query(...),
):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    start, end = _parse_dates(start_date, end_date)

    effective_date = _effective_date_expr()
    base_query = db.query(Transaction).filter(effective_date >= start, effective_date <= end)
    base_query = _apply_source_filter(base_query, source)
    matched_transfer_ids, matched_transfer_total = _match_internal_transfer_ids(base_query.all())

    query = db.query(Transaction).filter(effective_date >= start, effective_date <= end)
    query = _apply_source_filter(query, source)
    query = _apply_category_filter(query, category)
    txns = [t for t in query.all() if t.id not in matched_transfer_ids]

    def _amt(txn: Transaction) -> float:
        return _get_converted_amount(db, txn, lr, currency)

    income = sum(_amt(t) for t in txns if float(t._amount) > 0)
    spending = sum(abs(_amt(t)) for t in txns if float(t._amount) < 0)
    core_expense_txns = [t for t in txns if _core_expense_match(t.category, float(t._amount), include_rent=True)]
    core_spending = sum(-_amt(t) for t in core_expense_txns)
    core_spending_excluding_rent = sum(
        -_amt(t)
        for t in core_expense_txns
        if _top_category(t.category) != "Rent"
    )
    net_flow = income - spending

    spend_income_ratio = (spending / income * 100) if income > 0 else 0

    # Total balance across all accounts converted to target currency
    account_balances = (
        db.query(
            Transaction.source,
            func.sum(Transaction._amount * lr.c.rate).label("bal"),
        )
        .join(lr, and_(
            lr.c.from_currency == Transaction.currency,
            lr.c.to_currency == literal(currency),
        ))
        .group_by(Transaction.source)
        .all()
    )
    total_balance = round(sum(float(r.bal) for r in account_balances if r.bal), 2)

    result = {
        "income": income,
        "spending": spending,
        "core_spending": round(core_spending, 2),
        "core_spending_excluding_rent": round(core_spending_excluding_rent, 2),
        "transfers": matched_transfer_total,
        "spend_income_ratio": round(spend_income_ratio, 1),
        "net_flow": net_flow,
        "total_balance": total_balance,
        "subscriptions_monthly": get_monthly_subscription_total(db, rate_map={row[0]: float(row[1]) for row in db.query(lr.c.from_currency, lr.c.rate).filter(lr.c.to_currency == currency).all()}),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "currency": currency,
    }
    return result


@router.get("/by-category", response_model=list[CategoryBreakdownItem])
def get_by_category(
    db: Session = Depends(get_db),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    source: Optional[str] = None,
    core_expenses_only: bool = False,
    include_rent: bool = True,
    currency: str = Query(...),
):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    start, end = _parse_dates(start_date, end_date)
    effective_date = _effective_date_expr()

    if core_expenses_only:
        base_query = db.query(Transaction).filter(
            effective_date >= start,
            effective_date <= end,
        )
        base_query = _apply_source_filter(base_query, source)
        matched_transfer_ids, _ = _match_internal_transfer_ids(base_query.all())

        txns = [
            txn for txn in base_query.all()
            if txn.id not in matched_transfer_ids and _core_expense_match(txn.category, float(txn._amount), include_rent)
        ]
        categories = {}
        for txn in txns:
            cat = txn.category or "Uncategorized"
            parts = cat.split("/", 1)
            top_level = "Salary" if parts[0] == "Income" else parts[0]
            sub = parts[1] if len(parts) > 1 else None
            amount = _get_converted_amount(db, txn, lr, currency)
            if top_level not in categories:
                categories[top_level] = {"category": top_level, "total": 0, "subcategories": []}
            categories[top_level]["total"] += amount
            if sub:
                categories[top_level]["subcategories"].append({"name": sub, "total": amount})
        for value in categories.values():
            if value["subcategories"]:
                merged: dict[str, float] = defaultdict(float)
                for item in value["subcategories"]:
                    merged[item["name"]] += item["total"]
                value["subcategories"] = [
                    {"name": name, "total": total}
                    for name, total in sorted(merged.items(), key=lambda entry: abs(entry[1]), reverse=True)
                ]
        return sorted(categories.values(), key=lambda x: abs(x["total"]), reverse=True)

    # Per-transaction conversion path (handles all cases with SQL rate lookup)
    query = db.query(Transaction).filter(
        effective_date >= start,
        effective_date <= end,
    )
    query = _apply_source_filter(query, source)
    txns = query.all()
    categories = {}
    for txn in txns:
        cat = txn.category or "Uncategorized"
        parts = cat.split("/", 1)
        top_level = "Salary" if parts[0] == "Income" else parts[0]
        sub = parts[1] if len(parts) > 1 else None
        amount = _get_converted_amount(db, txn, lr, currency)
        if top_level not in categories:
            categories[top_level] = {"category": top_level, "total": 0, "subcategories": []}
        categories[top_level]["total"] += amount
        if sub:
            categories[top_level]["subcategories"].append({"name": sub, "total": amount})
    for value in categories.values():
        if value["subcategories"]:
            merged_subs: dict[str, float] = defaultdict(float)
            for item in value["subcategories"]:
                merged_subs[item["name"]] += item["total"]
            value["subcategories"] = [
                {"name": name, "total": total}
                for name, total in sorted(merged_subs.items(), key=lambda entry: abs(entry[1]), reverse=True)
            ]
    return sorted(categories.values(), key=lambda x: abs(x["total"]), reverse=True)


@router.get("/by-merchant", response_model=list[MerchantBreakdownItem])
def get_by_merchant(
    db: Session = Depends(get_db),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    source: Optional[str] = None,
    category: Optional[str] = None,
    core_expenses_only: bool = False,
    include_rent: bool = True,
    limit: int = 15,
    currency: str = Query(...),
):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    start, end = _parse_dates(start_date, end_date)
    effective_date = _effective_date_expr()

    # SQL aggregation path (no core_expenses_only filtering needed)
    if not core_expenses_only:
        merchant_expr = func.coalesce(Transaction.merchant_clean, Transaction.merchant_raw, "Unknown")
        query = db.query(
            merchant_expr.label("merchant"),
            func.sum(-(Transaction._amount * lr.c.rate)).label("total"),
        ).join(lr, and_(
            lr.c.from_currency == Transaction.currency,
            lr.c.to_currency == literal(currency),
        )).filter(
            effective_date >= start,
            effective_date <= end,
            Transaction._amount < 0,
        )
        query = _apply_source_filter(query, source)
        query = _apply_category_filter(query, category)
        rows = query.group_by(merchant_expr).order_by(func.sum(-(Transaction._amount * lr.c.rate)).desc()).limit(limit).all()
        return [{"merchant": row.merchant, "total": round(float(row.total), 2)} for row in rows]

    # Slow path: need per-transaction processing for core_expenses_only
    query = db.query(Transaction).filter(
        effective_date >= start,
        effective_date <= end,
        Transaction._amount < 0,
    )
    query = _apply_source_filter(query, source)
    query = _apply_category_filter(query, category)
    rows = query.all()

    base_query = db.query(Transaction).filter(
        effective_date >= start,
        effective_date <= end,
    )
    base_query = _apply_source_filter(base_query, source)
    matched_transfer_ids, _ = _match_internal_transfer_ids(base_query.all())
    rows = [
        row for row in rows
        if row.id not in matched_transfer_ids and _core_expense_match(row.category, float(row._amount), include_rent)
    ]

    grouped: dict[str, float] = defaultdict(float)
    for row in rows:
        merchant = row.merchant_clean or row.merchant_raw or "Unknown"
        grouped[merchant] += -_get_converted_amount(db, row, lr, currency)
    results = [{"merchant": merchant, "total": round(total, 2)} for merchant, total in grouped.items()]
    results.sort(key=lambda item: item["total"], reverse=True)
    return results[:limit]


@router.get("/trends")
def get_trends(
    db: Session = Depends(get_db),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    granularity: str = "monthly",
    source: Optional[str] = None,
    category: Optional[str] = None,
    core_expenses_only: bool = False,
    include_rent: bool = True,
    currency: str = Query(...),
):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    start, end = _parse_dates(start_date, end_date)
    effective_date = _effective_date_expr()

    if granularity == "daily" or granularity == "weekly":
        group_fmt = "%Y-%m-%d"
    else:
        group_fmt = "%Y-%m"

    base_query = db.query(Transaction).filter(
        effective_date >= start,
        effective_date <= end,
    )
    base_query = _apply_source_filter(base_query, source)
    matched_transfer_ids, _ = _match_internal_transfer_ids(base_query.all())

    # Need full Transaction objects for per-transaction currency conversion
    query = db.query(Transaction).filter(
        effective_date >= start,
        effective_date <= end,
    )
    query = _apply_source_filter(query, source)
    query = _apply_category_filter(query, category)
    txn_rows = query.all()

    periods: dict = {}
    for txn in txn_rows:
        if txn.id in matched_transfer_ids:
            continue
        raw_amount = float(txn._amount)
        converted = _get_converted_amount(db, txn, lr, currency)
        txn_date = txn.authorized_date or txn.date
        period = txn_date.strftime(group_fmt)
        if core_expenses_only:
            if raw_amount > 0:
                cat = "Income"
                amt = converted
            else:
                if not _core_expense_match(txn.category, raw_amount, include_rent):
                    continue
                full_cat = txn.category or "Uncategorized"
                top_cat = full_cat.split("/")[0]
                amt = -converted
                cat = top_cat
            if period not in periods:
                periods[period] = {"period": period, "total": 0}
            periods[period][cat] = periods[period].get(cat, 0) + amt
            periods[period]["total"] += amt
            continue
        full_cat = txn.category or "Uncategorized"
        top_cat = full_cat.split("/")[0]
        amt = abs(converted)
        if raw_amount > 0:
            cat = "Income"
        elif top_cat == "Credit Card":
            cat = full_cat
        else:
            cat = top_cat
        if period not in periods:
            periods[period] = {"period": period, "total": 0}
        periods[period][cat] = periods[period].get(cat, 0) + amt
        periods[period]["total"] += amt

    return sorted(periods.values(), key=lambda x: x["period"])


@router.get("/subscriptions")
def get_subscriptions(db: Session = Depends(get_db), currency: str = Query(...)):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    rate_rows = db.query(lr.c.from_currency, lr.c.rate).filter(lr.c.to_currency == currency).all()
    rate_map = {row[0]: float(row[1]) for row in rate_rows}
    return detect_subscriptions(db, rate_map=rate_map)


@router.get("/account-balances", response_model=list[AccountBalanceItem])
def get_account_balances(
    db: Session = Depends(get_db),
    currency: str = Query(...),
):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    results = (
        db.query(
            Transaction.source,
            func.sum(Transaction._amount * lr.c.rate).label("balance"),
        )
        .join(lr, and_(
            lr.c.from_currency == Transaction.currency,
            lr.c.to_currency == literal(currency),
        ))
        .group_by(Transaction.source)
        .all()
    )
    return [{"source": r.source, "balance": round(float(r.balance), 2) if r.balance else 0, "currency": currency} for r in results]


@router.get("/net-worth/tracked-history", response_model=NetWorthResponse)
def get_net_worth_tracked_history(
    db: Session = Depends(get_db),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    currency: str = Query(...),
):
    return _compute_net_worth(db, start_date=start_date, end_date=end_date, target_currency=currency)


def _compute_net_worth(
    db: Session,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    target_currency: str = "USD",
):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    start, end = _parse_dates(start_date, end_date)

    rate_map: dict[str, float] = {}
    def _convert(amount: float, from_currency: str) -> float:
        if not target_currency or from_currency == target_currency:
            return amount
        if from_currency not in rate_map:
            rate_row = (
                db.query(lr.c.rate)
                .filter(lr.c.from_currency == from_currency, lr.c.to_currency == target_currency)
                .first()
            )
            rate_map[from_currency] = float(rate_row[0]) if rate_row else 1.0
        return amount * rate_map[from_currency]

    history_rows = (
        db.query(SourceBalanceHistory)
        .filter(SourceBalanceHistory.date <= end)
        .order_by(SourceBalanceHistory.source.asc(), SourceBalanceHistory.date.asc(), SourceBalanceHistory.created_at.asc())
        .all()
    )

    source_events: dict[str, list[tuple[date, float, str, str]]] = defaultdict(list)
    source_info: dict[str, dict] = {}

    for row in history_rows:
        raw_key = row.source_key or row.source
        source_key = classify_source(raw_key)
        event_day = row.date
        group = _normalize_history_group(row.source, row.account_group)
        row_currency = getattr(row, "currency", "USD") or "USD"
        value = round(float(row._value or 0), 2)
        if source_events[source_key] and source_events[source_key][-1][0] == event_day:
            source_events[source_key][-1] = (event_day, value, group, row_currency)
        else:
            source_events[source_key].append((event_day, value, group, row_currency))
        source_info[source_key] = {
            "key": source_key,
            "label": get_source_label(row.source),
            "group": group,
            "current": value,
            "currency": row_currency,
            "history_mode": "historical" if len(source_events[source_key]) > 1 else "latest_only",
        }

    snapshot_rows = (
        db.query(AccountSnapshot)
        .filter(AccountSnapshot.synced_at <= end)
        .order_by(AccountSnapshot.source.asc(), AccountSnapshot.synced_at.asc())
        .all()
    )

    for row in snapshot_rows:
        source_key = classify_source(row.source) or row.source
        event_day = row.synced_at.date()
        group = _classify_snapshot_group(row.source, row.account_group)
        if source_key in source_info or any(
            info["group"] == group and _same_source_label(info["label"], row.source)
            for info in source_info.values()
        ):
            continue
        row_currency = getattr(row, "currency", "USD") or "USD"
        value = round(float(row._current_value or 0), 2)
        if source_events[source_key] and source_events[source_key][-1][0] == event_day:
            source_events[source_key][-1] = (event_day, value, group, row_currency)
        else:
            source_events[source_key].append((event_day, value, group, row_currency))
        source_info[source_key] = {
            "key": source_key,
            "label": get_source_label(row.source),
            "group": group,
            "current": value,
            "currency": row_currency,
            "history_mode": "historical" if len(source_events[source_key]) > 1 else "latest_only",
        }

    if not source_info:
        return {
            "currency": target_currency,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "points": [],
            "latest_sources": [],
            "groups": [],
        }

    # Build time series by carrying forward stored facts only.
    running: dict[str, tuple[float, str, str]] = {}
    event_idx = {s: 0 for s in source_events}

    for source, events in source_events.items():
        for ev_day, ev_val, ev_group, ev_cur in events:
            if ev_day < start:
                running[source] = (ev_val, ev_group, ev_cur)

    points = []
    day = start
    while day <= end:
        group_totals = {"bank_accounts": 0.0, "credit_cards": 0.0, "cash_like": 0.0, "brokerage": 0.0, "retirement": 0.0}

        for source, events in source_events.items():
            idx = event_idx[source]
            while idx < len(events) and events[idx][0] <= day:
                running[source] = (events[idx][1], events[idx][2], events[idx][3])
                idx += 1
            event_idx[source] = idx
            if source in running:
                value, group, cur = running[source]
                if group in group_totals:
                    group_totals[group] += _convert(value, cur)

        points.append({
            "date": day.isoformat(),
            "bank_accounts": round(group_totals["bank_accounts"], 2),
            "credit_cards": round(group_totals["credit_cards"], 2),
            "cash_like": round(group_totals["cash_like"], 2),
            "brokerage": round(group_totals["brokerage"], 2),
            "retirement": round(group_totals["retirement"], 2),
            "tracked_total": round(group_totals["bank_accounts"] + group_totals["credit_cards"], 2),
            "total": round(sum(group_totals.values()), 2),
        })
        day += timedelta(days=1)

    latest_sources = sorted(
        [
            {
                "key": info.get("key", source),
                "label": info["label"],
                "group": info["group"],
                "current": round(_convert(info["current"], info.get("currency", "USD")), 2),
                "currency": target_currency,
                "history_mode": info["history_mode"],
            }
            for source, info in source_info.items()
        ],
        key=lambda item: abs(item["current"]),
        reverse=True,
    )

    return {
        "currency": target_currency,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "points": points,
        "latest_sources": latest_sources,
        "groups": [
            {"key": "bank_accounts", "label": "Bank Accounts", "history_mode": "historical"},
            {"key": "credit_cards", "label": "Credit Cards", "history_mode": "historical"},
            {
                "key": "cash_like",
                "label": "Cash-Like",
                "history_mode": "historical" if any(item["group"] == "cash_like" and item["history_mode"] == "historical" for item in latest_sources) else "latest_only",
            },
            {
                "key": "brokerage",
                "label": "Brokerage",
                "history_mode": "historical" if any(item["group"] == "brokerage" and item["history_mode"] == "historical" for item in latest_sources) else "latest_only",
            },
            {
                "key": "retirement",
                "label": "Retirement",
                "history_mode": "historical" if any(item["group"] == "retirement" and item["history_mode"] == "historical" for item in latest_sources) else "latest_only",
            },
        ],
    }


@router.get("/cc-diagnostic")
def get_cc_diagnostic(db: Session = Depends(get_db)):
    results = db.query(
        Transaction.category,
        func.sum(Transaction._amount).label("total"),
        func.count().label("count")
    ).filter(Transaction.category.like("Credit Card%")).group_by(Transaction.category).all()
    items = [{"category": r.category, "total": round(float(r.total), 2), "count": r.count} for r in results]
    cc_net = round(sum(i["total"] for i in items), 2)
    return {"cc_net": cc_net, "balanced": abs(cc_net) < 0.01, "categories": items}
