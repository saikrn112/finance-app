from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.models import PlaidApiUsage


PLAID_ENDPOINT_PRICING = {
    "accounts_balance_get": Decimal("0.10"),
    "investments_holdings_get": Decimal("0.18"),
    "transactions_refresh": Decimal("0.12"),
    "transactions_sync": Decimal("0.00"),
    "link_token_create": Decimal("0.00"),
    "item_public_token_exchange": Decimal("0.00"),
}


def record_plaid_usage(
    db: Session,
    *,
    endpoint: str,
    institution: str | None = None,
    plaid_item_id: str | None = None,
    units: int | float = 1,
    metadata: dict | None = None,
) -> None:
    unit_cost = PLAID_ENDPOINT_PRICING.get(endpoint, Decimal("0.00"))
    usage = PlaidApiUsage(
        endpoint=endpoint,
        institution=institution,
        plaid_item_id=plaid_item_id,
        units=Decimal(str(units)),
        estimated_cost=unit_cost * Decimal(str(units)),
        metadata_json=metadata or {},
    )
    db.add(usage)


def month_bounds_utc(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = now or datetime.now(timezone.utc)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def plaid_usage_summary(db: Session) -> dict:
    month_start, month_end = month_bounds_utc()
    rows = (
        db.query(
            PlaidApiUsage.endpoint,
            func.count(PlaidApiUsage.id),
            func.sum(PlaidApiUsage.units),
            func.sum(PlaidApiUsage.estimated_cost),
        )
        .filter(PlaidApiUsage.created_at >= month_start, PlaidApiUsage.created_at < month_end)
        .group_by(PlaidApiUsage.endpoint)
        .order_by(PlaidApiUsage.endpoint.asc())
        .all()
    )
    return {
        "month_start": month_start.date().isoformat(),
        "month_end_exclusive": month_end.date().isoformat(),
        "total_estimated_cost": round(sum(float(row[3] or 0) for row in rows), 2),
        "endpoints": [
            {
                "endpoint": row[0],
                "call_count": int(row[1] or 0),
                "units": float(row[2] or 0),
                "estimated_cost": round(float(row[3] or 0), 2),
            }
            for row in rows
        ],
    }
