from __future__ import annotations

from sqlalchemy.orm import Session

from src.models import AccountActivity, InvestmentPeriodFact, SourceBalanceHistory, SyncLog
from src.ingestion.plaid_usage import upsert_product_enrollments
from src.plugins.registry import classify_source


def preview_backfill(db: Session) -> dict[str, int]:
    snapshots = db.query(SourceBalanceHistory).filter(
        SourceBalanceHistory.account_group.in_(["investment", "brokerage"])
    ).count()
    existing_facts = db.query(InvestmentPeriodFact).count()
    connections = db.query(SyncLog).filter(
        SyncLog.source == "plaid", SyncLog.plaid_item_id.is_not(None)
    ).count()
    return {
        "investment_snapshots": snapshots,
        "existing_period_facts": existing_facts,
        "plaid_connections": connections,
    }


def run_backfill(db: Session) -> dict[str, int]:
    inserted_facts = 0
    snapshots = db.query(SourceBalanceHistory).filter(
        SourceBalanceHistory.account_group.in_(["investment", "brokerage"])
    ).order_by(
        SourceBalanceHistory.source.asc(), SourceBalanceHistory.date.asc(),
        SourceBalanceHistory.created_at.asc(),
    ).all()
    previous_by_source: dict[str, SourceBalanceHistory] = {}
    for snapshot in snapshots:
        previous = previous_by_source.get(snapshot.source)
        existing = db.query(InvestmentPeriodFact).filter(
            InvestmentPeriodFact.source == snapshot.source,
            InvestmentPeriodFact.period_end == snapshot.date,
        ).first()
        if existing is None:
            source_key = snapshot.source_key or classify_source(snapshot.source) or snapshot.source
            activity = db.query(AccountActivity).filter(
                (AccountActivity.source_key == source_key) | (AccountActivity.source == snapshot.source),
                AccountActivity.date <= snapshot.date,
                AccountActivity.pending.is_(False),
                AccountActivity.currency == snapshot.currency,
                AccountActivity.activity_type == "transfer",
            )
            if previous is not None:
                activity = activity.filter(AccountActivity.date > previous.date)
            inflow = round(sum(float(row._amount or 0) for row in activity.all()), 2)
            beginning = round(float(previous._value), 2) if previous is not None else None
            ending = round(float(snapshot._value), 2)
            db.add(InvestmentPeriodFact(
                source_key=source_key, source=snapshot.source,
                period_start=previous.date if previous is not None else None,
                period_end=snapshot.date, _beginning_value=beginning,
                _ending_value=ending, _inflow=inflow,
                _market_gain=round(ending - beginning - inflow, 2) if beginning is not None else 0,
                currency=snapshot.currency, provenance="account_activity_backfill",
            ))
            inserted_facts += 1
        previous_by_source[snapshot.source] = snapshot

    enrolled_items = 0
    for log in db.query(SyncLog).filter(
        SyncLog.source == "plaid", SyncLog.plaid_item_id.is_not(None)
    ).all():
        products = list((log.extra_data or {}).get("plaid_products") or [])
        if products:
            upsert_product_enrollments(db, log.plaid_item_id, products)
            enrolled_items += 1
    db.commit()
    return {"inserted_period_facts": inserted_facts, "enrolled_plaid_items": enrolled_items}
