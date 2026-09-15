from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
import re
from typing import Any

from sqlalchemy.orm import Session

from src.models import Transaction

LOOKBACK_DAYS = 1100
EXCLUDED_TOP_CATEGORIES = {"Income", "Remittance", "Credit Card", "Investment", "Transfer", "Loan", "Tax"}
ALLOWED_TOP_CATEGORIES = {"Subscriptions", "Rent", "Utilities", "Insurance", "Phone", "Health", "Internet"}
EXCLUDED_MERCHANT_PATTERNS = [
    r"ANNUAL FEE",
    r"\bPAYMENT\b",
    r"\bTRANSFER\b",
]
CANONICAL_NAME_PATTERNS: list[tuple[str, str]] = []
FREQUENCY_RULES: dict[str, dict[str, int | tuple[int, int]]] = {
    "weekly": {"days": 7, "min_occurrences": 4, "range": (5, 9)},
    "biweekly": {"days": 14, "min_occurrences": 3, "range": (11, 18)},
    "monthly": {"days": 30, "min_occurrences": 2, "range": (24, 35)},
    "quarterly": {"days": 90, "min_occurrences": 2, "range": (80, 100)},
    "yearly": {"days": 365, "min_occurrences": 2, "range": (330, 390)},
}
GRACE_BY_FREQUENCY = {"weekly": 3, "biweekly": 5, "monthly": 10, "quarterly": 20, "yearly": 35}
MONTHLY_EQ_MULTIPLIER = {
    "weekly": 52 / 12,
    "biweekly": 26 / 12,
    "monthly": 1.0,
    "quarterly": 1 / 3,
    "yearly": 1 / 12,
}


@dataclass
class Charge:
    transaction_id: str
    date: date
    amount: float
    merchant_raw: str
    merchant: str
    canonical_name: str
    display_name: str
    category: str
    top_category: str
    source: str
    account_last4: str | None
    currency: str = "USD"


@dataclass
class RecurringEntity:
    id: str
    canonical_name: str
    display_name: str
    kind: str
    category: str
    top_category: str
    frequency: str
    charges: list[Charge]
    detection_mode: str = "auto"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "recurring"


def _stable_id(canonical_name: str, first_seen: date, frequency: str, amount_hint: float) -> str:
    payload = f"{canonical_name}|{first_seen.isoformat()}|{frequency}|{amount_hint:.2f}"
    digest = hashlib.md5(payload.encode("utf-8")).hexdigest()[:10]
    return f"recurring-{_slug(canonical_name)}-{digest}"


def _merchant_text(txn: Transaction) -> str:
    return (txn.merchant_clean or txn.merchant_raw or "").strip()


def _top_category(category: str | None) -> str:
    return (category or "Uncategorized").split("/", 1)[0]


def _titleize(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9+& ]+", " ", raw).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.title() if cleaned else "Recurring Item"


def _normalize_service_name(txn: Transaction) -> tuple[str, str]:
    merchant = _merchant_text(txn)
    haystack = merchant.upper()
    for pattern, label in CANONICAL_NAME_PATTERNS:
        if re.search(pattern, haystack):
            return label.lower(), label
    if txn.merchant_clean and len(txn.merchant_clean) <= 36 and not re.search(r"\d{4,}", txn.merchant_clean):
        return txn.merchant_clean.strip().lower(), txn.merchant_clean.strip()
    shortened = re.sub(r"\b(?:CA|NJ|NY|MA|DE|USA|SAN FRANCISCO|CUPERTINO|BURLINGAME)\b", " ", merchant, flags=re.IGNORECASE)
    shortened = re.sub(r"\b\d{3,}\b", " ", shortened)
    shortened = re.sub(r"\s+", " ", shortened).strip()
    display = _titleize(shortened[:40] or merchant[:40])
    return display.lower(), display


def _include_transaction(txn: Transaction, raw_amount: float | None = None) -> bool:
    amount = raw_amount if raw_amount is not None else float(txn._amount or 0)
    if amount >= 0:
        return False
    top_category = _top_category(txn.category)
    merchant = _merchant_text(txn).upper()
    if top_category in EXCLUDED_TOP_CATEGORIES:
        return False
    if any(re.search(pattern, merchant) for pattern in EXCLUDED_MERCHANT_PATTERNS):
        return False
    if top_category in ALLOWED_TOP_CATEGORIES:
        return True
    return any(re.search(pattern, merchant) for pattern, _ in CANONICAL_NAME_PATTERNS)


def _charge_from_transaction(txn: Transaction, rate: float = 1.0) -> Charge:
    canonical_name, display_name = _normalize_service_name(txn)
    return Charge(
        transaction_id=txn.id,
        date=txn.date,
        amount=abs(round(float(txn._amount) * rate, 2)),
        merchant_raw=txn.merchant_raw or "",
        merchant=_merchant_text(txn),
        canonical_name=canonical_name,
        display_name=display_name,
        category=txn.category or "Uncategorized",
        top_category=_top_category(txn.category),
        source=txn.source,
        account_last4=txn.account_last4,
        currency=getattr(txn, "currency", None) or "USD",
    )


def _detect_frequency(charges: list[Charge]) -> str | None:
    if len(charges) < 2:
        return None
    dates = sorted(charge.date for charge in charges)
    intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    if not intervals:
        return None

    best_name = None
    best_score = -1
    for name, rule in FREQUENCY_RULES.items():
        min_occurrences = int(rule["min_occurrences"])
        lower, upper = rule["range"]
        if len(charges) < min_occurrences:
            continue
        matches = [days for days in intervals if lower <= days <= upper]
        if not matches:
            continue
        score = len(matches)
        if len(intervals) > 1 and score < max(1, int(len(intervals) * 0.6)):
            continue
        if score > best_score:
            best_name = name
            best_score = score
    return best_name


def _monthly_equivalent(amount: float, frequency: str) -> float:
    return round(amount * MONTHLY_EQ_MULTIPLIER.get(frequency, 1.0), 2)


def _expected_interval_days(frequency: str) -> int:
    return int(FREQUENCY_RULES.get(frequency, {"days": 30})["days"])


def _latest_charge_before(charges: list[Charge], as_of: date) -> Charge | None:
    latest = None
    for charge in charges:
        if charge.date > as_of:
            break
        latest = charge
    return latest


def _entity_status_for_day(entity: dict[str, Any], as_of: date) -> str:
    first_seen = date.fromisoformat(entity["first_seen"])
    if as_of < first_seen:
        return "inactive"
    if entity["ended_at"] and as_of >= date.fromisoformat(entity["ended_at"]):
        return "ended"
    latest_charge = _latest_charge_before(entity["_charges"], as_of)
    if latest_charge is None:
        return "inactive"
    grace = GRACE_BY_FREQUENCY.get(entity["frequency"], 10)
    expected = _expected_interval_days(entity["frequency"])
    if (as_of - latest_charge.date).days <= expected + grace:
        return "active"
    return "inactive"


def _classify_kind(top_category: str, frequency: str) -> str:
    if top_category == "Subscriptions":
        return "subscription"
    if top_category in {"Rent", "Utilities", "Insurance", "Phone", "Internet"}:
        return "bill"
    if frequency in {"quarterly", "yearly"}:
        return "renewal"
    return "subscription"


def _build_entity(charges: list[Charge], frequency: str) -> RecurringEntity:
    charges = sorted(charges, key=lambda charge: (charge.date, charge.transaction_id))
    latest = charges[-1]
    top_category = latest.top_category
    return RecurringEntity(
        id=_stable_id(latest.canonical_name, charges[0].date, frequency, latest.amount),
        canonical_name=latest.canonical_name,
        display_name=latest.display_name,
        kind=_classify_kind(top_category, frequency),
        category=latest.category,
        top_category=top_category,
        frequency=frequency,
        charges=charges,
    )


def _should_merge_entities(left: RecurringEntity, right: RecurringEntity) -> bool:
    if left.frequency != right.frequency or left.canonical_name != right.canonical_name:
        return False
    interval = _expected_interval_days(left.frequency)
    left_start, left_end = left.charges[0].date, left.charges[-1].date
    right_start, right_end = right.charges[0].date, right.charges[-1].date
    overlap_days = (min(left_end, right_end) - max(left_start, right_start)).days
    if overlap_days > interval:
        return False
    gap_days = (right_start - left_end).days if right_start >= left_end else (left_start - right_end).days
    return gap_days <= interval * 2 + 15


def _merge_entities(left: RecurringEntity, right: RecurringEntity) -> RecurringEntity:
    charges_by_id = {charge.transaction_id: charge for charge in left.charges}
    charges_by_id.update({charge.transaction_id: charge for charge in right.charges})
    charges = sorted(charges_by_id.values(), key=lambda charge: (charge.date, charge.transaction_id))
    return _build_entity(charges, left.frequency)


def _build_events(entity: RecurringEntity, today: date) -> tuple[list[dict[str, Any]], set[str], str | None]:
    events: list[dict[str, Any]] = []
    flags: set[str] = set()
    previous = None
    for charge in entity.charges:
        if previous is None:
            events.append(
                {
                    "event_type": "started",
                    "effective_date": charge.date.isoformat(),
                    "amount": charge.amount,
                    "frequency": entity.frequency,
                    "source": charge.source,
                    "account_last4": charge.account_last4,
                    "transaction_id": charge.transaction_id,
                }
            )
            previous = charge
            continue
        tolerance = max(1.0, previous.amount * 0.08)
        if abs(charge.amount - previous.amount) >= tolerance:
            flags.add("price_changed")
            events.append(
                {
                    "event_type": "price_changed",
                    "effective_date": charge.date.isoformat(),
                    "amount": charge.amount,
                    "frequency": entity.frequency,
                    "source": charge.source,
                    "account_last4": charge.account_last4,
                    "transaction_id": charge.transaction_id,
                    "metadata": {"previous_amount": previous.amount},
                }
            )
        account_changed = (
            previous.account_last4 is not None
            and charge.account_last4 is not None
            and charge.account_last4 != previous.account_last4
        )
        if charge.source != previous.source or account_changed:
            flags.add("card_changed")
            events.append(
                {
                    "event_type": "card_changed",
                    "effective_date": charge.date.isoformat(),
                    "amount": charge.amount,
                    "frequency": entity.frequency,
                    "source": charge.source,
                    "account_last4": charge.account_last4,
                    "transaction_id": charge.transaction_id,
                    "metadata": {
                        "previous_source": previous.source,
                        "previous_account_last4": previous.account_last4,
                    },
                }
            )
        events.append(
            {
                "event_type": "renewed",
                "effective_date": charge.date.isoformat(),
                "amount": charge.amount,
                "frequency": entity.frequency,
                "source": charge.source,
                "account_last4": charge.account_last4,
                "transaction_id": charge.transaction_id,
            }
        )
        previous = charge

    expected_interval = _expected_interval_days(entity.frequency)
    grace = GRACE_BY_FREQUENCY.get(entity.frequency, 10)
    last_charge = entity.charges[-1]
    ended_at = last_charge.date + timedelta(days=expected_interval)
    if (today - last_charge.date).days > expected_interval + grace:
        events.append(
            {
                "event_type": "ended",
                "effective_date": ended_at.isoformat(),
                "amount": last_charge.amount,
                "frequency": entity.frequency,
                "source": last_charge.source,
                "account_last4": last_charge.account_last4,
                "transaction_id": None,
            }
        )
        return events, flags, ended_at.isoformat()
    return events, flags, None


def _serialize_entity(entity: RecurringEntity, today: date) -> dict[str, Any]:
    charges = sorted(entity.charges, key=lambda charge: (charge.date, charge.transaction_id))
    events, flags, ended_at = _build_events(entity, today)
    latest = charges[-1]
    monthly_equivalent = _monthly_equivalent(latest.amount, entity.frequency)
    next_expected = (latest.date + timedelta(days=_expected_interval_days(entity.frequency))).isoformat()
    status = "ended" if ended_at else "active"
    serialized = {
        "id": entity.id,
        "display_name": entity.display_name,
        "canonical_name": entity.canonical_name,
        "status": status,
        "kind": entity.kind,
        "category": entity.category,
        "top_category": entity.top_category,
        "currency": "USD",
        "current_amount": latest.amount,
        "monthly_equivalent": monthly_equivalent,
        "frequency": entity.frequency,
        "current_source": latest.source,
        "current_account_last4": latest.account_last4,
        "first_seen": charges[0].date.isoformat(),
        "last_seen": latest.date.isoformat(),
        "next_expected": next_expected,
        "ended_at": ended_at,
        "latest_event_type": events[-1]["event_type"] if events else None,
        "flags": sorted(flags),
        "detection_mode": entity.detection_mode,
        "event_count": len(events),
        "_charges": charges,
        "_events": events,
    }
    return serialized


def _query_candidate_transactions(db: Session) -> list[Transaction]:
    cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)
    return (
        db.query(Transaction)
        .filter(Transaction.date >= cutoff, Transaction._amount < 0)
        .order_by(Transaction.date.asc(), Transaction.id.asc())
        .all()
    )


def build_recurring_entities(db: Session, rate_map: dict[str, float] | None = None) -> list[dict[str, Any]]:
    if rate_map is None:
        rate_map = {}
    grouped: dict[tuple[str, float], list[Charge]] = defaultdict(list)
    for txn in _query_candidate_transactions(db):
        if not _include_transaction(txn):
            continue
        txn_currency = getattr(txn, "currency", None) or "USD"
        rate = rate_map.get(txn_currency, 1.0)
        charge = _charge_from_transaction(txn, rate=rate)
        grouped[(charge.canonical_name, round(charge.amount, 2))].append(charge)

    provisional: list[RecurringEntity] = []
    for charges in grouped.values():
        charges = sorted(charges, key=lambda charge: (charge.date, charge.transaction_id))
        frequency = _detect_frequency(charges)
        if not frequency:
            continue
        provisional.append(_build_entity(charges, frequency))

    merged: list[RecurringEntity] = []
    by_name: dict[str, list[RecurringEntity]] = defaultdict(list)
    for entity in provisional:
        by_name[entity.canonical_name].append(entity)

    for entities in by_name.values():
        entities = sorted(entities, key=lambda item: (item.charges[0].date, item.charges[-1].date))
        current = None
        for entity in entities:
            if current is None:
                current = entity
                continue
            if _should_merge_entities(current, entity):
                current = _merge_entities(current, entity)
            else:
                merged.append(current)
                current = entity
        if current is not None:
            merged.append(current)

    today = date.today()
    serialized = [_serialize_entity(entity, today) for entity in merged]
    serialized.sort(
        key=lambda item: (
            item["status"] != "active",
            -item["monthly_equivalent"],
            item["display_name"].lower(),
        )
    )
    return serialized


def get_recurring_catalog(db: Session, start_date: date | None = None, end_date: date | None = None, rate_map: dict[str, float] | None = None) -> dict[str, Any]:
    start = start_date or (date.today() - timedelta(days=365))
    end = end_date or date.today()
    items = build_recurring_entities(db, rate_map=rate_map)
    today = date.today()
    upcoming_cutoff = today + timedelta(days=30)

    summary = {
        "current_monthly_total": round(sum(item["monthly_equivalent"] for item in items if item["status"] == "active"), 2),
        "active_count": sum(1 for item in items if item["status"] == "active"),
        "ended_in_range": sum(
            1
            for item in items
            if item["ended_at"] and start <= date.fromisoformat(item["ended_at"]) <= end
        ),
        "price_changes_in_range": sum(
            1
            for item in items
            for event in item["_events"]
            if event["event_type"] == "price_changed" and start <= date.fromisoformat(event["effective_date"]) <= end
        ),
        "renewing_soon": sum(
            1
            for item in items
            if item["status"] == "active" and today <= date.fromisoformat(item["next_expected"]) <= upcoming_cutoff
        ),
    }

    filters = {
        "statuses": sorted({item["status"] for item in items}),
        "kinds": sorted({item["kind"] for item in items}),
        "sources": sorted({item["current_source"] for item in items if item["current_source"]}),
        "categories": sorted({item["top_category"] for item in items if item.get("top_category")}),
        "currencies": sorted({item["currency"] for item in items}),
    }

    catalog_items = []
    for item in items:
        catalog_items.append(
            {
                key: value
                for key, value in item.items()
                if key not in {"_charges", "_events"}
            }
            | {"top_category": item["top_category"]}
        )

    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "summary": summary,
        "items": catalog_items,
        "filters": filters,
    }


def _month_start(day: date) -> date:
    return date(day.year, day.month, 1)


def _add_month(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def _month_end(day: date) -> date:
    return _add_month(day) - timedelta(days=1)


def _group_key(item: dict[str, Any], group_by: str, charge: Charge | None = None) -> str:
    if group_by == "category":
        return item["top_category"]
    if group_by == "source":
        return charge.source if charge else item["current_source"]
    if group_by == "currency":
        return item["currency"]
    return item["display_name"]


def get_recurring_trends(
    db: Session,
    start_date: date | None = None,
    end_date: date | None = None,
    metric: str = "monthly_equivalent_total",
    group_by: str = "service",
    rate_map: dict[str, float] | None = None,
) -> dict[str, Any]:
    start = _month_start(start_date or (date.today() - timedelta(days=365)))
    end = end_date or date.today()
    items = build_recurring_entities(db, rate_map=rate_map)
    points = []
    groups_total: dict[str, float] = defaultdict(float)

    cursor = start
    while cursor <= end:
        bucket_end = min(_month_end(cursor), end)
        breakdown: dict[str, float] = defaultdict(float)
        for item in items:
            if metric == "charge_total":
                for charge in item["_charges"]:
                    if cursor <= charge.date <= bucket_end:
                        key = _group_key(item, group_by, charge)
                        breakdown[key] += charge.amount
            else:
                status = _entity_status_for_day(item, bucket_end)
                if status != "active":
                    continue
                charge = _latest_charge_before(item["_charges"], bucket_end)
                if charge is None:
                    continue
                key = _group_key(item, group_by, charge)
                if metric == "active_count":
                    breakdown[key] += 1
                else:
                    breakdown[key] += _monthly_equivalent(charge.amount, item["frequency"])

        cleaned_breakdown = {key: round(value, 2) for key, value in breakdown.items() if round(value, 2)}
        total = round(sum(cleaned_breakdown.values()), 2)
        for key, value in cleaned_breakdown.items():
            groups_total[key] += value
        points.append(
            {
                "date": cursor.isoformat(),
                "label": cursor.strftime("%b %Y"),
                "total": total,
                "breakdown": cleaned_breakdown,
            }
        )
        cursor = _add_month(cursor)

    groups = [
        {"key": key, "label": key, "total": round(total, 2)}
        for key, total in sorted(groups_total.items(), key=lambda item: (-item[1], item[0].lower()))
    ]
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "metric": metric,
        "group_by": group_by,
        "groups": groups,
        "points": points,
    }


def get_recurring_detail(db: Session, recurring_id: str, rate_map: dict[str, float] | None = None) -> dict[str, Any] | None:
    items = build_recurring_entities(db, rate_map=rate_map)
    for item in items:
        if item["id"] != recurring_id:
            continue
        charge_history = [
            {
                "transaction_id": charge.transaction_id,
                "date": charge.date.isoformat(),
                "amount": charge.amount,
                "source": charge.source,
                "account_last4": charge.account_last4,
                "merchant": charge.merchant,
                "category": charge.category,
            }
            for charge in sorted(item["_charges"], key=lambda charge: (charge.date, charge.transaction_id), reverse=True)
        ]
        detail = {
            key: value
            for key, value in item.items()
            if key not in {"_charges", "_events"}
        }
        detail["events"] = sorted(item["_events"], key=lambda event: event["effective_date"], reverse=True)
        detail["transactions"] = charge_history
        detail["price_history"] = [
            {"date": charge["date"], "amount": charge["amount"]}
            for charge in charge_history
        ]
        detail["source_history"] = [
            {
                "date": charge["date"],
                "source": charge["source"],
                "account_last4": charge["account_last4"],
            }
            for charge in charge_history
        ]
        return detail
    return None


def detect_subscriptions(db: Session, rate_map: dict[str, float] | None = None) -> list[dict[str, Any]]:
    subscriptions = []
    for item in build_recurring_entities(db, rate_map=rate_map):
        if item["kind"] != "subscription" or item["status"] != "active":
            continue
        subscriptions.append(
            {
                "merchant": item["display_name"],
                "amount": item["current_amount"],
                "frequency": item["frequency"],
                "last_charge": item["last_seen"],
                "occurrences": len(item["_charges"]),
                "status": item["status"],
                "next_expected": item["next_expected"],
                "source": item["current_source"],
                "account_last4": item["current_account_last4"],
                "monthly_equivalent": item["monthly_equivalent"],
                "flags": item["flags"],
            }
        )
    return sorted(subscriptions, key=lambda item: item["monthly_equivalent"], reverse=True)


def get_monthly_subscription_total(db: Session, rate_map: dict[str, float] | None = None) -> float:
    # float() matters: with nothing to sum, round(0, 2) is an int, so the return type
    # changed shape depending on the data.
    return float(round(sum(item["monthly_equivalent"] for item in detect_subscriptions(db, rate_map=rate_map)), 2))
