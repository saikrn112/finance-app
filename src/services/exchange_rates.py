from datetime import date, datetime, timezone
from sqlalchemy import and_, func, literal, exists, select
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert
from threading import RLock
from weakref import WeakKeyDictionary
import logging
from src.models.transaction import ExchangeRate, generate_uuid

_last_refresh_by_engine = WeakKeyDictionary()
_refresh_lock = RLock()
logger = logging.getLogger(__name__)
_REFRESH_INTERVAL_SECONDS = 3600  # 1 hour

IDENTITY_CURRENCIES = ["USD", "INR", "EUR", "GBP", "CAD", "AUD", "JPY"]


def seed_identity_rates(db: Session) -> None:
    """Ensure identity rates exist for common currencies (X->X = 1.0).
    These are permanent and never refreshed — the SQL JOIN always does amount * rate unconditionally.
    """
    _ensure_identity_rates(db)


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
    """Coordinate cold-start refresh per database, without committing caller work."""
    bind = db.get_bind()
    with _refresh_lock:
        now = datetime.now(timezone.utc)
        last = _last_refresh_by_engine.get(bind)
        if last and (now - last).total_seconds() < _REFRESH_INTERVAL_SECONDS:
            return
        _ensure_identity_rates(db)
        with Session(bind=bind) as rates_db:
            has_today = rates_db.query(ExchangeRate.id).filter(
                ExchangeRate.date == date.today(), ExchangeRate.from_currency == "USD",
                ExchangeRate.to_currency != "USD",
            ).first()
        if has_today or refresh_rates(db) > 0:
            _last_refresh_by_engine[bind] = now
        # A failed provider refresh must remain retryable; identity joins remain available.


def _ensure_identity_rates(db: Session) -> None:
    """Atomic seeding even when another process initializes the same database."""
    rows = [dict(id=generate_uuid(), date=date.today(), from_currency=currency,
                 to_currency=currency, rate=1.0) for currency in IDENTITY_CURRENCIES]
    with _refresh_lock, Session(bind=db.get_bind()) as rates_db:
        rates_db.execute(insert(ExchangeRate).values(rows).on_conflict_do_nothing(
            index_elements=['date', 'from_currency', 'to_currency']))
        rates_db.commit()


def refresh_rates(db: Session, base_currency: str = "USD") -> int:
    """Fetch outside the write transaction and atomically upsert provider observations."""
    import urllib.request
    import json
    with _refresh_lock:
        try:
            with urllib.request.urlopen(f"https://open.er-api.com/v6/latest/{base_currency}", timeout=10) as response:
                data = json.loads(response.read())
            if data.get("result") != "success":
                return 0
            observations = {}
            for currency, raw in data.get("rates", {}).items():
                value = float(raw)
                if value <= 0:
                    continue
                observations[(base_currency, currency)] = value
                observations[(currency, base_currency)] = round(1.0 / value, 10)
            if not observations:
                return 0
            rows = [dict(id=generate_uuid(), date=date.today(), from_currency=source,
                         to_currency=target, rate=rate) for (source, target), rate in observations.items()]
            with Session(bind=db.get_bind()) as rates_db:
                statement = insert(ExchangeRate).values(rows)
                rates_db.execute(statement.on_conflict_do_update(
                    index_elements=['date', 'from_currency', 'to_currency'],
                    set_={'rate': statement.excluded.rate}))
                rates_db.commit()
            return len(data.get("rates", {}))
        except Exception:
            logger.warning("Exchange-rate refresh failed; retaining stored rates", exc_info=True)
            return 0
