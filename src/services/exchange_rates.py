from datetime import date, datetime, timezone
from sqlalchemy import and_, func, literal, exists, select
from sqlalchemy.orm import Session
from src.models.transaction import ExchangeRate, generate_uuid

_last_refresh: datetime | None = None
_REFRESH_INTERVAL_SECONDS = 3600  # 1 hour

IDENTITY_CURRENCIES = ["USD", "INR", "EUR", "GBP", "CAD", "AUD", "JPY"]


def seed_identity_rates(db: Session) -> None:
    """Ensure identity rates exist for common currencies (X->X = 1.0).
    These are permanent and never refreshed — the SQL JOIN always does amount * rate unconditionally.
    """
    today = date.today()
    for currency in IDENTITY_CURRENCIES:
        existing = db.query(ExchangeRate).filter(
            ExchangeRate.from_currency == currency,
            ExchangeRate.to_currency == currency,
        ).first()
        if not existing:
            db.add(ExchangeRate(
                id=generate_uuid(),
                date=today,
                from_currency=currency,
                to_currency=currency,
                rate=1.0,
            ))
    db.commit()


def latest_rate_subquery(db: Session):
    """Build a SQLAlchemy subquery returning the latest rate per (from_currency, to_currency).

    Usage:
        lr = latest_rate_subquery(db)
        query = (
            db.query(func.sum(Transaction._amount * lr.c.rate))
            .join(lr, and_(
                lr.c.from_currency == Transaction.currency,
                lr.c.to_currency == literal(currency),
            ))
        )
    """
    # Step 1: Get the max date per currency pair
    max_dates = (
        db.query(
            ExchangeRate.from_currency,
            ExchangeRate.to_currency,
            func.max(ExchangeRate.date).label("max_date"),
        )
        .group_by(ExchangeRate.from_currency, ExchangeRate.to_currency)
        .subquery("max_dates")
    )

    # Step 2: Join back to get the rate at that max date
    direct_latest_query = (
        db.query(
            ExchangeRate.from_currency.label("from_currency"),
            ExchangeRate.to_currency.label("to_currency"),
            ExchangeRate.rate.label("rate"),
        )
        .join(
            max_dates,
            and_(
                ExchangeRate.from_currency == max_dates.c.from_currency,
                ExchangeRate.to_currency == max_dates.c.to_currency,
                ExchangeRate.date == max_dates.c.max_date,
            )
        )
    )
    direct_latest = direct_latest_query.subquery("direct_latest_rates")

    inverse_latest = (
        db.query(
            direct_latest.c.to_currency.label("from_currency"),
            direct_latest.c.from_currency.label("to_currency"),
            (literal(1.0) / direct_latest.c.rate).label("rate"),
        )
        .filter(direct_latest.c.from_currency != direct_latest.c.to_currency)
        .subquery("inverse_candidates")
    )

    inverse_missing = (
        db.query(
            inverse_latest.c.from_currency,
            inverse_latest.c.to_currency,
            inverse_latest.c.rate,
        )
        .filter(
            ~exists(
                select(literal(1)).select_from(direct_latest).where(
                    and_(
                        direct_latest.c.from_currency == inverse_latest.c.from_currency,
                        direct_latest.c.to_currency == inverse_latest.c.to_currency,
                    )
                )
            )
        )
        .subquery("inverse_latest_rates")
    )

    inverse_missing_query = db.query(
        inverse_missing.c.from_currency,
        inverse_missing.c.to_currency,
        inverse_missing.c.rate,
    )

    return direct_latest_query.union_all(inverse_missing_query).subquery("latest_rates")


def ensure_rates_fresh(db: Session) -> None:
    """Refresh rates if stale (older than 1 hour). Also seeds identity rates if missing."""
    global _last_refresh
    now = datetime.now(timezone.utc)
    if _last_refresh and (now - _last_refresh).total_seconds() < _REFRESH_INTERVAL_SECONDS:
        return
    # Seed identity rates if any are missing
    _ensure_identity_rates(db)
    today = date.today()
    has_real_today_rates = db.query(ExchangeRate.id).filter(
        ExchangeRate.date == today,
        ExchangeRate.from_currency == "USD",
        ExchangeRate.to_currency != "USD",
    ).first()
    if has_real_today_rates:
        _last_refresh = now
        return
    count = refresh_rates(db)
    if count > 0:
        _last_refresh = now
    _last_refresh = now


def _ensure_identity_rates(db: Session) -> None:
    """Quick check + seed of identity rates (idempotent)."""
    for currency in IDENTITY_CURRENCIES:
        existing = db.query(ExchangeRate).filter(
            ExchangeRate.from_currency == currency,
            ExchangeRate.to_currency == currency,
        ).first()
        if not existing:
            db.add(ExchangeRate(
                id=generate_uuid(),
                date=date.today(),
                from_currency=currency,
                to_currency=currency,
                rate=1.0,
            ))
    try:
        db.commit()
    except Exception:
        db.rollback()


def refresh_rates(db: Session, base_currency: str = "USD") -> int:
    """Fetch latest rates from a free API and store them."""
    import urllib.request
    import json
    today = date.today()
    try:
        url = f"https://open.er-api.com/v6/latest/{base_currency}"
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read())
        if data.get("result") != "success":
            return 0
        rates = data.get("rates", {})
        count = 0
        # Batch fetch existing rates for today
        existing_forward = {
            r.to_currency: r for r in db.query(ExchangeRate).filter(
                ExchangeRate.date == today,
                ExchangeRate.from_currency == base_currency,
            ).all()
        }
        existing_inverse = {
            r.from_currency: r for r in db.query(ExchangeRate).filter(
                ExchangeRate.date == today,
                ExchangeRate.to_currency == base_currency,
            ).all()
        }

        for currency, rate_value in rates.items():
            if not rate_value or rate_value == 0:
                continue
            # Forward rate
            existing = existing_forward.get(currency)
            if existing:
                existing.rate = rate_value
            else:
                db.add(ExchangeRate(id=generate_uuid(), date=today, from_currency=base_currency, to_currency=currency, rate=rate_value))
            # Inverse rate
            inverse_rate = round(1.0 / rate_value, 10)
            existing_inv = existing_inverse.get(currency)
            if existing_inv:
                existing_inv.rate = inverse_rate
            else:
                db.add(ExchangeRate(id=generate_uuid(), date=today, from_currency=currency, to_currency=base_currency, rate=inverse_rate))
            count += 1
        try:
            db.commit()
        except Exception:
            db.rollback()
        return count
    except Exception:
        db.rollback()
        return 0
