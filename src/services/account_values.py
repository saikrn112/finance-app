from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import Session

from src.models import SourceBalanceHistory
from src.plugins.registry import classify_source


def canonical_source_key(source: str) -> str:
    return classify_source(source) or source


def record_account_value(
    db: Session,
    *,
    source: str,
    account_group: str,
    value: float,
    observed_at: date | datetime,
    currency: str = "USD",
    provenance: str,
) -> SourceBalanceHistory:
    """Upsert one observed account value. No dates are synthesized or carried forward here."""
    observed_day = observed_at.date() if isinstance(observed_at, datetime) else observed_at
    row = (
        db.query(SourceBalanceHistory)
        .filter(SourceBalanceHistory.source == source, SourceBalanceHistory.date == observed_day)
        .first()
    )
    created_at = observed_at if isinstance(observed_at, datetime) else datetime.combine(observed_day, datetime.min.time())
    if row is None:
        row = SourceBalanceHistory(source=source, date=observed_day)
        db.add(row)
    row.source_key = canonical_source_key(source)
    row.account_group = account_group
    row.value = round(value, 2)
    row.currency = currency
    row.provenance = provenance
    row.created_at = created_at
    return row


def latest_account_values(db: Session) -> dict[str, SourceBalanceHistory]:
    """Return the latest canonical valuation per display source."""
    rows = (
        db.query(SourceBalanceHistory)
        .order_by(SourceBalanceHistory.source.asc(), SourceBalanceHistory.date.desc(), SourceBalanceHistory.created_at.desc())
        .all()
    )
    latest: dict[str, SourceBalanceHistory] = {}
    for row in rows:
        latest.setdefault(row.source, row)
    return latest
