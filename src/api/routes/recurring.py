from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.models import get_db
from src.processing.subscriptions import get_recurring_catalog, get_recurring_detail, get_recurring_trends
from src.services.exchange_rates import latest_rate_subquery, ensure_rates_fresh
from src.api.schemas import RecurringCatalogResponse, RecurringTrendsResponse, RecurringDetailResponse

router = APIRouter()


def _build_rate_map(db: Session, lr_subq, target_currency: str) -> dict[str, float]:
    rate_rows = db.query(lr_subq.c.from_currency, lr_subq.c.rate).filter(lr_subq.c.to_currency == target_currency).all()
    return {row[0]: float(row[1]) for row in rate_rows}


@router.get("/catalog", response_model=RecurringCatalogResponse)
def recurring_catalog(
    db: Session = Depends(get_db),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    currency: str = Query(...),
):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    rate_map = _build_rate_map(db, lr, currency)

    result = get_recurring_catalog(
        db,
        start_date=None if not start_date else date.fromisoformat(start_date),
        end_date=None if not end_date else date.fromisoformat(end_date),
        rate_map=rate_map,
    )
    result["currency"] = currency
    return result


@router.get("/trends", response_model=RecurringTrendsResponse)
def recurring_trends(
    db: Session = Depends(get_db),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    metric: str = "monthly_equivalent_total",
    group_by: str = "service",
    currency: str = Query(...),
):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    rate_map = _build_rate_map(db, lr, currency)

    result = get_recurring_trends(
        db,
        start_date=None if not start_date else date.fromisoformat(start_date),
        end_date=None if not end_date else date.fromisoformat(end_date),
        metric=metric,
        group_by=group_by,
        rate_map=rate_map,
    )
    result["currency"] = currency
    return result


@router.get("/{recurring_id}", response_model=RecurringDetailResponse)
def recurring_detail(recurring_id: str, db: Session = Depends(get_db), currency: str = Query(...)):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    rate_map = _build_rate_map(db, lr, currency)

    detail = get_recurring_detail(db, recurring_id, rate_map=rate_map)
    if not detail:
        raise HTTPException(status_code=404, detail="Recurring item not found")
    detail["currency"] = currency
    return detail
