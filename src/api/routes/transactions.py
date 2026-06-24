from collections import Counter, defaultdict
import re
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import and_, desc, func, literal
from datetime import date, timedelta

from src.models import get_db, Transaction, TransactionProject, Project, ExchangeRate
from src.services.exchange_rates import latest_rate_subquery, ensure_rates_fresh
from src.api.schemas import TransactionItem, TransactionListResponse, UncategorizedReviewResponse

router = APIRouter()
CORE_EXPENSE_EXCLUDED_TOP_CATEGORIES = {"Income", "Salary", "Investment", "Remittance", "Credit Card"}
INTERNAL_TRANSFER_WINDOW_DAYS = 10

PREFERRED_CATEGORY_ORDER = [
    "Salary",
    "Investment",
    "Savings",
    "Remittance",
    "Loan",
    "Rent",
    "Credit Card",
    "Dining",
    "Groceries",
    "Transportation",
    "Health",
    "Shopping",
    "Subscriptions",
    "Personal",
    "Government",
    "Tax",
    "Uncategorized",
]


def _category_sort_key(value: str) -> tuple[int, str]:
    try:
        return (PREFERRED_CATEGORY_ORDER.index(value), value.lower())
    except ValueError:
        return (len(PREFERRED_CATEGORY_ORDER), value.lower())


def _normalize_category(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return "Uncategorized"

    parts = [part.strip() for part in raw.split("/") if part.strip()]
    if not parts:
        return "Uncategorized"
    if len(parts) > 2:
        raise HTTPException(status_code=422, detail="Category must be either 'Category' or 'Category/Subcategory'")
    if parts[0].lower() == "uncategorized":
        return "Uncategorized"
    return "/".join(parts)


def _load_rule_categories() -> dict[str, set[str]]:
    import os
    env_plugins_dir = os.environ.get("FINANCE_PLUGINS_DIR")
    if env_plugins_dir and (Path(env_plugins_dir) / "rules" / "categories.yaml").exists():
        rules_path = Path(env_plugins_dir) / "rules" / "categories.yaml"
    else:
        rules_path = Path(__file__).resolve().parents[3] / "rules" / "categories.yaml"
    categories: dict[str, set[str]] = defaultdict(set)
    if not rules_path.exists():
        return categories

    with rules_path.open() as handle:
        data = yaml.safe_load(handle) or {}

    for rule in data.get("rules", []):
        category = _normalize_category(rule.get("category"))
        top_level, _, subcategory = category.partition("/")
        categories[top_level]
        if subcategory:
            categories[top_level].add(subcategory)

    return categories


def _build_category_options(db: Session) -> list[dict[str, list[str] | str]]:
    categories = _load_rule_categories()
    rows = db.query(Transaction.category).filter(Transaction.category.isnot(None)).distinct().all()
    for (category_value,) in rows:
        category = _normalize_category(category_value)
        top_level, _, subcategory = category.partition("/")
        categories[top_level]
        if subcategory:
            categories[top_level].add(subcategory)

    categories["Uncategorized"]
    return [
        {
            "category": top_level,
            "subcategories": sorted(subcategories, key=lambda item: item.lower()),
        }
        for top_level, subcategories in sorted(categories.items(), key=lambda item: _category_sort_key(item[0]))
    ]


def _merchant_match_value(txn: Transaction) -> str:
    return (txn.merchant_clean or txn.merchant_raw or "").strip()


def _merchant_group_key(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", " ", (value or "").upper()).strip()
    return normalized or "UNKNOWN"


def _transactions_for_uncategorized(query):
    return query.filter(
        (Transaction.category.is_(None)) | (Transaction.category.like("Uncategorized%"))
    )


def _quote_yaml(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _append_rule_to_yaml(category: str, match_text: str, merchant_clean: str | None) -> bool:
    import os
    env_plugins_dir = os.environ.get("FINANCE_PLUGINS_DIR")
    if env_plugins_dir:
        rules_path = Path(env_plugins_dir) / "rules" / "categories.yaml"
    else:
        rules_path = Path(__file__).resolve().parents[3] / "rules" / "categories.yaml"
    existing_data: dict = {}
    if rules_path.exists():
        with rules_path.open() as handle:
            existing_data = yaml.safe_load(handle) or {}
    existing_rules = existing_data.get("rules", [])
    pattern = re.escape(match_text.strip())

    for rule in existing_rules:
        if (
            (rule.get("pattern") or "").strip() == pattern
            and _normalize_category(rule.get("category")) == category
            and (rule.get("merchant_clean") or "").strip() == (merchant_clean or "").strip()
        ):
            return False

    if not rules_path.exists():
        rules_path.parent.mkdir(parents=True, exist_ok=True)
        rules_path.write_text("# Default categorization rules\nrules:\n")

    lines = [
        "",
        "  # Learned from uncategorized review",
        f"  - pattern: {_quote_yaml(pattern)}",
        f"    category: {_quote_yaml(category)}",
    ]
    if merchant_clean:
        lines.append(f"    merchant_clean: {_quote_yaml(merchant_clean.strip())}")

    with rules_path.open("a") as handle:
        handle.write("\n".join(lines) + "\n")
    return True


def _get_date_range(range_str: str) -> tuple[date, date]:
    today = date.today()
    if range_str == "week":
        start = today - timedelta(days=today.weekday())
    elif range_str == "month":
        start = today.replace(day=1)
    elif range_str == "6months":
        start = today - timedelta(days=180)
    elif range_str == "sinceyear":
        start = today - timedelta(days=365)
    elif range_str == "year":
        start = today.replace(month=1, day=1)
    else:
        return None, None
    return start, today


def _top_category(category: str | None) -> str:
    return (category or "Uncategorized").split("/", 1)[0]


def _effective_date_expr():
    return func.coalesce(Transaction.authorized_date, Transaction.date)


def _effective_date(txn: Transaction) -> date | None:
    return txn.authorized_date or txn.date


def _core_expense_match(txn: Transaction, include_rent: bool) -> bool:
    top = _top_category(txn.category)
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


def _match_internal_transfer_ids(txns: list[Transaction]) -> set[str]:
    negatives: dict[tuple[str, int], list[Transaction]] = defaultdict(list)
    positives: dict[tuple[str, int], list[Transaction]] = defaultdict(list)

    for txn in txns:
        sub = _cc_transfer_subcategory(txn)
        if not sub:
            continue
        amount = round(float(txn._amount or 0), 2)
        cents = int(round(abs(amount) * 100))
        key = (sub, cents)
        if amount < 0:
            negatives[key].append(txn)
        elif amount > 0:
            positives[key].append(txn)

    matched_ids: set[str] = set()
    for key, pos_items in positives.items():
        neg_items = negatives.get(key)
        if not neg_items:
            continue
        pos_sorted = sorted(pos_items, key=lambda t: (t.date, t.id))
        remaining_negs = sorted(neg_items, key=lambda t: (t.date, t.id))
        for pos_txn in pos_sorted:
            match_index = None
            for idx, neg_txn in enumerate(remaining_negs):
                if neg_txn.source == pos_txn.source:
                    continue
                if abs((pos_txn.date - neg_txn.date).days) > INTERNAL_TRANSFER_WINDOW_DAYS:
                    continue
                match_index = idx
                break
            if match_index is None:
                continue
            neg_txn = remaining_negs.pop(match_index)
            matched_ids.add(pos_txn.id)
            matched_ids.add(neg_txn.id)

    return matched_ids


@router.get("/", response_model=TransactionListResponse)
def list_transactions(
    db: Session = Depends(get_db),
    offset: int = 0,
    limit: int = 10000,
    category: Optional[str] = None,
    source: Optional[str] = None,
    range: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    search: Optional[str] = None,
    core_expenses_only: bool = False,
    include_rent: bool = True,
    currency: str = Query(...),
):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)

    query = db.query(Transaction)

    if range:
        start, end = _get_date_range(range)
        if start:
            effective_date = _effective_date_expr()
            query = query.filter(effective_date >= start, effective_date <= end)
    else:
        if start_date:
            query = query.filter(_effective_date_expr() >= start_date)
        if end_date:
            query = query.filter(_effective_date_expr() <= end_date)

    if category:
        query = query.filter(Transaction.category.like(f"{category}%"))
    if search:
        query = query.filter(
            (Transaction.merchant_clean.ilike(f"%{search}%")) | (Transaction.merchant_raw.ilike(f"%{search}%"))
        )
    if source:
        sources = [s.strip() for s in source.split(',')]
        query = query.filter(Transaction.source.in_(sources))

    if core_expenses_only:
        rows = query.order_by(desc(_effective_date_expr()), desc(Transaction.date)).all()
        matched_transfer_ids = _match_internal_transfer_ids(rows)
        rows = [txn for txn in rows if txn.id not in matched_transfer_ids and _core_expense_match(txn, include_rent)]
        total = len(rows)
        transactions = rows[offset:offset + limit]
    else:
        # Use SQL COUNT + LIMIT/OFFSET to avoid loading all rows into memory
        total = query.count()
        transactions = query.order_by(desc(_effective_date_expr()), desc(Transaction.date)).offset(offset).limit(limit).all()

    # Build a rate map for batch conversion
    rate_map = _build_rate_map(db, lr, currency)

    # Batch load project assignments
    txn_ids = [t.id for t in transactions]
    proj_map: dict[str, list[dict]] = {}
    if txn_ids:
        links = db.query(TransactionProject, Project).join(
            Project, TransactionProject.project_id == Project.id
        ).filter(TransactionProject.transaction_id.in_(txn_ids)).all()
        for tp, p in links:
            proj_map.setdefault(tp.transaction_id, []).append({"id": p.id, "name": p.name, "color": p.color})

    return {
        "currency": currency,
        "transactions": [_serialize(t, proj_map.get(t.id), rate_map=rate_map) for t in transactions],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/category-options")
def get_category_options(db: Session = Depends(get_db)):
    return {"categories": _build_category_options(db)}


@router.get("/uncategorized/review", response_model=UncategorizedReviewResponse)
def review_uncategorized_transactions(
    db: Session = Depends(get_db),
    search: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=100, ge=1, le=100),
    per_group_limit: int = Query(default=5, ge=1, le=20),
    currency: str = Query(...),
):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    rate_map = _build_rate_map(db, lr, currency)

    query = db.query(Transaction)
    query = _transactions_for_uncategorized(query)
    if search:
        query = query.filter(
            (Transaction.merchant_clean.ilike(f"%{search}%")) | (Transaction.merchant_raw.ilike(f"%{search}%"))
        )

    rows = query.order_by(desc(Transaction.date)).all()
    grouped: dict[str, dict] = {}
    for txn in rows:
        match_value = _merchant_match_value(txn)
        group_key = _merchant_group_key(match_value)
        txn_currency = getattr(txn, "currency", None) or "USD"
        rate = rate_map.get(txn_currency, 1.0)
        entry = grouped.setdefault(
            group_key,
            {
                "merchant_key": group_key,
                "display_name": match_value or "Unknown Merchant",
                "merchant_clean_suggestion": (txn.merchant_clean or match_value or "Unknown Merchant").strip(),
                "rule_pattern_source": match_value or (txn.merchant_raw or "").strip(),
                "transaction_count": 0,
                "total_amount": 0.0,
                "latest_date": txn.date.isoformat() if txn.date else None,
                "_display_names": Counter(),
                "_merchant_clean": Counter(),
                "transactions": [],
            },
        )
        entry["transaction_count"] += 1
        entry["total_amount"] += float(txn._amount or 0) * rate
        if txn.date and (entry["latest_date"] is None or txn.date.isoformat() > entry["latest_date"]):
            entry["latest_date"] = txn.date.isoformat()
        if match_value:
            entry["_display_names"][match_value] += 1
        if txn.merchant_clean:
            entry["_merchant_clean"][txn.merchant_clean.strip()] += 1
        if len(entry["transactions"]) < per_group_limit:
            entry["transactions"].append(_serialize(txn, rate_map=rate_map))

    groups = []
    for entry in grouped.values():
        if entry["_display_names"]:
            entry["display_name"] = entry["_display_names"].most_common(1)[0][0]
        if entry["_merchant_clean"]:
            entry["merchant_clean_suggestion"] = entry["_merchant_clean"].most_common(1)[0][0]
        entry["total_amount"] = round(entry["total_amount"], 2)
        entry.pop("_display_names", None)
        entry.pop("_merchant_clean", None)
        groups.append(entry)

    groups.sort(
        key=lambda item: (
            item["latest_date"] or "",
            item["transaction_count"],
            item["display_name"].lower(),
        ),
        reverse=True,
    )
    total_groups = len(groups)
    start = (page - 1) * limit
    end = start + limit
    return {
        "currency": currency,
        "groups": groups[start:end],
        "total_groups": total_groups,
        "total_transactions": sum(group["transaction_count"] for group in groups),
        "page": page,
        "limit": limit,
        "total_pages": max(1, (total_groups + limit - 1) // limit),
    }


@router.post("/uncategorized/apply")
def apply_uncategorized_review(updates: dict, db: Session = Depends(get_db)):
    category = _normalize_category(updates.get("category"))
    merchant_key = (updates.get("merchant_key") or "").strip()
    txn_ids = [str(txn_id) for txn_id in updates.get("transaction_ids", []) if str(txn_id).strip()]
    apply_to_similar = bool(updates.get("apply_to_similar", True))
    save_rule = bool(updates.get("save_rule", False))
    merchant_clean = (updates.get("merchant_clean") or "").strip() or None
    rule_pattern_source = (updates.get("rule_pattern_source") or "").strip()

    if not merchant_key and not txn_ids:
        raise HTTPException(status_code=422, detail="merchant_key or transaction_ids is required")

    query = db.query(Transaction)
    query = _transactions_for_uncategorized(query)
    if apply_to_similar:
        if not merchant_key:
            raise HTTPException(status_code=422, detail="merchant_key is required when apply_to_similar is true")
        matches = [txn for txn in query.all() if _merchant_group_key(_merchant_match_value(txn)) == merchant_key]
    else:
        if not txn_ids:
            raise HTTPException(status_code=422, detail="transaction_ids is required when apply_to_similar is false")
        matches = query.filter(Transaction.id.in_(txn_ids)).all()

    if not matches:
        raise HTTPException(status_code=404, detail="No matching uncategorized transactions found")

    previous_values = [
        {
            "id": txn.id,
            "category": txn.category,
            "merchant_clean": txn.merchant_clean,
            "category_source": txn.category_source,
        }
        for txn in matches
    ]

    updated_ids: list[str] = []
    for txn in matches:
        txn.category = category
        txn.category_source = "user"
        if merchant_clean:
            txn.merchant_clean = merchant_clean
        updated_ids.append(txn.id)

    rule_saved = False
    if save_rule:
        match_text = rule_pattern_source or _merchant_match_value(matches[0])
        if not match_text:
            raise HTTPException(status_code=422, detail="rule_pattern_source is required to save a rule")
        rule_saved = _append_rule_to_yaml(category, match_text, merchant_clean)

    db.commit()
    return {
        "updated_count": len(updated_ids),
        "updated_ids": updated_ids,
        "category": category,
        "rule_saved": rule_saved,
        "undo_payload": previous_values,
    }


@router.post("/uncategorized/undo")
def undo_uncategorized_review(payload: dict, db: Session = Depends(get_db)):
    previous_values = payload.get("previous_values") or []
    if not isinstance(previous_values, list) or not previous_values:
        raise HTTPException(status_code=422, detail="previous_values is required")

    updated_ids: list[str] = []
    for item in previous_values:
        txn_id = str(item.get("id") or "").strip()
        if not txn_id:
            continue
        txn = db.query(Transaction).filter(Transaction.id == txn_id).first()
        if not txn:
            continue
        txn.category = item.get("category")
        txn.merchant_clean = item.get("merchant_clean")
        txn.category_source = item.get("category_source")
        updated_ids.append(txn.id)

    db.commit()
    return {"updated_count": len(updated_ids), "updated_ids": updated_ids}


@router.get("/{txn_id}", response_model=TransactionItem)
def get_transaction(txn_id: str, db: Session = Depends(get_db), currency: str = Query(...)):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    rate_map = _build_rate_map(db, lr, currency)
    txn = db.query(Transaction).filter(Transaction.id == txn_id).first()
    if not txn:
        raise HTTPException(status_code=404, detail="Not found")
    return _serialize(txn, rate_map=rate_map)


@router.patch("/{txn_id}", response_model=TransactionItem)
def update_transaction(txn_id: str, updates: dict, db: Session = Depends(get_db), currency: str = Query(...)):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    rate_map = _build_rate_map(db, lr, currency)
    txn = db.query(Transaction).filter(Transaction.id == txn_id).first()
    if not txn:
        raise HTTPException(status_code=404, detail="Not found")

    allowed = {"category", "merchant_clean", "tags", "notes", "is_recurring"}
    for key, value in updates.items():
        if key in allowed:
            if key == "category":
                value = _normalize_category(value)
            setattr(txn, key, value)
            if key == "category":
                txn.category_source = "user"

    db.commit()
    return _serialize(txn, rate_map=rate_map)


def _build_rate_map(db: Session, lr_subq, target_currency: str) -> dict[str, float]:
    """Build a {from_currency: rate} lookup for all currencies to target_currency."""
    rows = (
        db.query(lr_subq.c.from_currency, lr_subq.c.rate)
        .filter(lr_subq.c.to_currency == target_currency)
        .all()
    )
    return {row[0]: float(row[1]) for row in rows}


def _serialize(t: Transaction, projects: list[dict] | None = None, rate_map: dict[str, float] | None = None) -> dict:
    raw_amount = float(t._amount) if t._amount else 0
    if rate_map is not None:
        txn_currency = getattr(t, "currency", None) or "USD"
        rate = rate_map.get(txn_currency)
        converted_amount = raw_amount * rate if rate is not None else raw_amount
    else:
        converted_amount = raw_amount
    return {
        "id": t.id,
        "date": t.date.isoformat() if t.date else None,
        "authorized_date": t.authorized_date.isoformat() if t.authorized_date else None,
        "effective_date": (_effective_date(t).isoformat() if _effective_date(t) else None),
        "amount": converted_amount,
        "merchant_raw": t.merchant_raw,
        "merchant_clean": t.merchant_clean,
        "original_description": t.original_description,
        "category": t.category,
        "source": t.source,
        "account_last4": t.account_last4,
        "pending": bool(t.pending),
        "is_recurring": t.is_recurring,
        "tags": t.tags or [],
        "projects": projects or [],
    }
