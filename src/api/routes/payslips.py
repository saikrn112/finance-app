from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, literal
from sqlalchemy.orm import Session

from src.config import settings
from src.demo import get_demo_payslips
from src.models import SyncLog, get_db
from src.models import Payslip, PayslipLineItem
from src.services.exchange_rates import latest_rate_subquery, ensure_rates_fresh
from src.api.schemas import PayslipListResponse

router = APIRouter()


# ---------------------------------------------------------------------------
# Primary path: query normalized Payslip + PayslipLineItem tables
# ---------------------------------------------------------------------------


def _payslips_from_normalized(db: Session, currency: str) -> list[dict] | None:
    """Query Payslip table with rate JOIN. Returns None if table is empty (fallback signal)."""
    count = db.query(Payslip).limit(1).count()
    if count == 0:
        return None  # Signal caller to use SyncLog fallback

    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)

    rows = (
        db.query(
            Payslip,
            (Payslip._gross * lr.c.rate).label("gross"),
            (Payslip._net * lr.c.rate).label("net"),
            (Payslip._total_taxes * lr.c.rate).label("total_taxes"),
            (Payslip._total_deductions * lr.c.rate).label("total_deductions"),
        )
        .join(lr, and_(
            lr.c.from_currency == Payslip.currency,
            lr.c.to_currency == literal(currency),
        ))
        .order_by(Payslip.pay_date.asc())
        .all()
    )

    # Build line items grouped by payslip_id with rate conversion
    payslip_ids = [row.Payslip.id for row in rows]
    line_items_raw = (
        db.query(
            PayslipLineItem,
            (PayslipLineItem._amount * lr.c.rate).label("converted_amount"),
        )
        .join(Payslip, Payslip.id == PayslipLineItem.payslip_id)
        .join(lr, and_(
            lr.c.from_currency == Payslip.currency,
            lr.c.to_currency == literal(currency),
        ))
        .filter(PayslipLineItem.payslip_id.in_(payslip_ids))
        .all()
    ) if payslip_ids else []

    # Group line items by payslip and section
    from collections import defaultdict
    line_items_by_payslip: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for li_row in line_items_raw:
        li = li_row.PayslipLineItem
        line_items_by_payslip[li.payslip_id][li.section].append({
            "label": li.label,
            "amount": round(float(li_row.converted_amount or 0), 2),
        })

    payslips: list[dict] = []
    for row in rows:
        p = row.Payslip
        items_by_section = line_items_by_payslip.get(p.id, {})
        payslip_dict = {
            "employer": p.employer,
            "pay_date": p.pay_date.isoformat() if p.pay_date else None,
            "pay_period_start": p.pay_period_start.isoformat() if getattr(p, "pay_period_start", None) else None,
            "pay_period_end": p.pay_period_end.isoformat() if getattr(p, "pay_period_end", None) else None,
            "gross": round(float(row.gross or 0), 2),
            "net": round(float(row.net or 0), 2),
            "total_taxes": round(float(row.total_taxes or 0), 2),
            "total_deductions": round(float(row.total_deductions or 0), 2),
            "taxes": items_by_section.get("taxes", []),
            "deductions": items_by_section.get("deductions", []),
            "earnings": items_by_section.get("earnings", []),
        }
        payslips.append(payslip_dict)

    return payslips


# ---------------------------------------------------------------------------
# Fallback path: scan SyncLog JSON (pre-migration data)
# ---------------------------------------------------------------------------


def _imported_payslips(db: Session) -> list[dict]:
    logs = (
        db.query(SyncLog)
        .filter(SyncLog.sync_type == "import_payslip_pdf", SyncLog.status == "success")
        .order_by(SyncLog.created_at.asc())
        .all()
    )
    payslips = []
    seen_signatures: set[str] = set()
    for log in logs:
        extra = log.extra_data or {}
        payloads = list(extra.get("payloads") or ([] if not extra.get("payload") else [extra["payload"]]))
        for payload in payloads:
            signature = _payslip_signature(payload)
            if signature and signature in seen_signatures:
                continue
            if signature:
                seen_signatures.add(signature)
            payslips.append(payload)
    return payslips


def _sort_key(payslip: dict) -> tuple[str, str]:
    raw = payslip.get("pay_date") or ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return (datetime.strptime(raw, fmt).date().isoformat(), raw)
        except ValueError:
            continue
    return (raw, raw)


def _payslip_signature(payload: dict | None) -> str | None:
    if not payload:
        return None
    employer = str(payload.get("employer") or "").strip()
    pay_date = payload.get("pay_date") or ""
    if not employer or not pay_date:
        return None
    gross = round(float(payload.get("gross") or 0), 2)
    net = round(float(payload.get("net") or 0), 2)
    taxes = round(float(payload.get("total_taxes") or 0), 2)
    deductions = round(float(payload.get("total_deductions") or 0), 2)
    return f"{employer}|{pay_date}|{gross:.2f}|{net:.2f}|{taxes:.2f}|{deductions:.2f}"


def _convert_payslip(payslip: dict, rate: float) -> dict:
    """Multiply all money fields in a payslip dict by the exchange rate."""
    money_keys = ("gross", "net", "total_taxes", "total_deductions")
    nested_money_sections = ("taxes", "deductions", "earnings")

    converted = dict(payslip)
    for key in money_keys:
        if key in converted and converted[key] is not None:
            converted[key] = round(float(converted[key]) * rate, 2)

    for section in nested_money_sections:
        if section in converted and isinstance(converted[section], dict):
            converted[section] = {
                k: round(float(v) * rate, 2) if isinstance(v, (int, float)) else v
                for k, v in converted[section].items()
            }
        elif section in converted and isinstance(converted[section], list):
            converted[section] = [
                {
                    k: round(float(v) * rate, 2) if k in ("amount", "value", "ytd") and isinstance(v, (int, float)) else v
                    for k, v in item.items()
                }
                if isinstance(item, dict) else item
                for item in converted[section]
            ]

    return converted


@router.get("/", response_model=PayslipListResponse)
def get_payslips(db: Session = Depends(get_db), currency: str = Query(...)):
    if settings.is_demo:
        payslips = get_demo_payslips()
        if not payslips:
            return {"currency": currency, "payslips": [], "latest": None}
        # Demo payslips are plain dicts — apply rate conversion via fallback path
        ensure_rates_fresh(db)
        lr = latest_rate_subquery(db)
        rate_rows = db.query(lr.c.from_currency, lr.c.rate).filter(lr.c.to_currency == currency).all()
        rate_map: dict[str, float] = {row[0]: float(row[1]) for row in rate_rows}

        def _payslip_rate(payslip: dict) -> float:
            payslip_currency = payslip.get("currency") or "USD"
            return rate_map.get(payslip_currency, 1.0)

        ordered_payslips = sorted(payslips, key=_sort_key)
        converted_payslips = [_convert_payslip(p, _payslip_rate(p)) for p in ordered_payslips]
    else:
        converted_payslips = _payslips_from_normalized(db, currency)
        if converted_payslips is None:
            converted_payslips = []

    if not converted_payslips:
        return {"currency": currency, "payslips": [], "latest": None}

    latest = converted_payslips[-1]
    total_gross = sum(p.get("gross", 0) for p in converted_payslips)
    total_net = sum(p.get("net", 0) for p in converted_payslips)
    total_taxes = sum(p.get("total_taxes", 0) for p in converted_payslips)
    total_deductions = sum(p.get("total_deductions", 0) for p in converted_payslips)

    return {
        "currency": currency,
        "payslips": converted_payslips,
        "latest": latest,
        "totals": {
            "gross": total_gross,
            "net": total_net,
            "taxes": total_taxes,
            "deductions": total_deductions,
            "effective_tax_rate": total_taxes / total_gross if total_gross else 0,
        },
    }
