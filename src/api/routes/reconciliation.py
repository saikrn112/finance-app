from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from datetime import date
from decimal import Decimal
from typing import Optional

from src.models import get_db, Balance
from src.processing.overlap_diagnostics import audit_all_sources, audit_source_overlaps
from src.processing.reconcile import reconcile
from src.processing.statement_verification import StatementEvidence, verify_statement
from src.services.exchange_rates import latest_rate_subquery, ensure_rates_fresh

router = APIRouter()


@router.get("/")
def get_reconciliation(db: Session = Depends(get_db), currency: str = Query(..., pattern=r"^[A-Z]{3}$")):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    rate_rows = db.query(lr.c.from_currency, lr.c.rate).filter(lr.c.to_currency == currency).all()
    rate_map = {row[0]: float(row[1]) for row in rate_rows}
    try:
        return reconcile(db, rate_map=rate_map)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/balance")
def add_balance(
    source: str,
    balance_date: date,
    balance: Decimal = Query(..., allow_inf_nan=False),
    balance_type: str = Query("statement", pattern=r"^(opening|closing|statement)$"),
    currency: str = Query(..., pattern=r"^[A-Z]{3}$"),
    account_last4: str | None = Query(None, pattern=r"^\d{4}$"),
    db: Session = Depends(get_db),
):
    b = Balance(source=source, date=balance_date, balance=balance, balance_type=balance_type,
                currency=currency, account_last4=account_last4)
    db.add(b)
    db.commit()
    return {"status": "ok", "id": b.id}


@router.get("/balances")
def list_balances(source: Optional[str] = None, currency: str = Query(..., pattern=r"^[A-Z]{3}$"),
                  db: Session = Depends(get_db)):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    rates = dict(db.query(lr.c.from_currency, lr.c.rate).filter(lr.c.to_currency == currency).all())
    q = db.query(Balance)
    if source:
        q = q.filter(Balance.source == source)
    return [{"id": b.id, "source": b.source, "account_last4": b.account_last4,
             "date": b.date.isoformat(), "currency": currency, "observed_currency": b.currency,
             "balance": float(b.balance * Decimal(str(rates[b.currency]))) if b.currency in rates else None,
             "type": b.balance_type, "status": "converted" if b.currency in rates else
             ("currency_unknown" if b.currency is None else "rate_missing")}
            for b in q.order_by(Balance.date).all()]


@router.get("/overlaps")
def get_overlap_audit(source: Optional[str] = None, window_days: int = Query(3, ge=0, le=31), db: Session = Depends(get_db), currency: str = Query(..., pattern=r"^[A-Z]{3}$")):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)
    rate_rows = db.query(lr.c.from_currency, lr.c.rate).filter(lr.c.to_currency == currency).all()
    rate_map = {row[0]: float(row[1]) for row in rate_rows}
    try:
        if source:
            return audit_source_overlaps(db, source, window_days=window_days, rate_map=rate_map)
        return audit_all_sources(db, window_days=window_days, rate_map=rate_map)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/verify-statement")
def verify_statement_evidence(
    evidence: StatementEvidence,
    currency: str = Query(..., pattern=r"^[A-Z]{3}$"),
    db: Session = Depends(get_db),
):
    """Compare supplied statement evidence without persisting or changing any rows."""
    return verify_statement(db, evidence, currency)
