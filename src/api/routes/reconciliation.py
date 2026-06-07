from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from datetime import date
from typing import Optional

from src.models import get_db, Balance
from src.processing.overlap_diagnostics import audit_all_sources, audit_source_overlaps
from src.processing.reconcile import reconcile
from src.services.exchange_rates import latest_rate_subquery, ensure_rates_fresh

router = APIRouter()


@router.get("/")
def get_reconciliation(db: Session = Depends(get_db), currency: str = Query(...)):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    rate_rows = db.query(lr.c.from_currency, lr.c.rate).filter(lr.c.to_currency == currency).all()
    rate_map = {row[0]: float(row[1]) for row in rate_rows}
    return reconcile(db, rate_map=rate_map)


@router.post("/balance")
def add_balance(
    source: str,
    balance_date: date,
    balance: float,
    balance_type: str = "statement",
    db: Session = Depends(get_db),
):
    b = Balance(source=source, date=balance_date, balance=balance, balance_type=balance_type)
    db.add(b)
    db.commit()
    return {"status": "ok", "id": b.id}


@router.get("/balances")
def list_balances(source: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(Balance)
    if source:
        q = q.filter(Balance.source == source)
    return [{"id": b.id, "source": b.source, "date": b.date.isoformat(), "balance": float(b.balance), "type": b.balance_type} for b in q.order_by(Balance.date).all()]


@router.get("/overlaps")
def get_overlap_audit(source: Optional[str] = None, window_days: int = 3, db: Session = Depends(get_db), currency: str = Query(...)):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    rate_rows = db.query(lr.c.from_currency, lr.c.rate).filter(lr.c.to_currency == currency).all()
    rate_map = {row[0]: float(row[1]) for row in rate_rows}
    if source:
        return audit_source_overlaps(db, source, window_days=window_days, rate_map=rate_map)
    return audit_all_sources(db, window_days=window_days, rate_map=rate_map)
