from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import and_, func, literal
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel
from typing import Optional
from datetime import date
from decimal import Decimal

from src.models import get_db, Project, TransactionProject, Transaction, ExchangeRate, Contact, ProjectMember, TransactionSplit
from src.services.exchange_rates import latest_rate_subquery, ensure_rates_fresh
from src.api.schemas import ProjectItem, ProjectDetailResponse
from src.sync.tracking import bulk_delete

router = APIRouter()


class ProjectCreate(BaseModel):
    name: str
    color: str = "#6366f1"
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    budget: Optional[float] = None
    notes: Optional[str] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    budget: Optional[float] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class ProjectTransactionsUpdate(BaseModel):
    transaction_ids: list[str]


def _serialize_project(p: Project, spent: float = 0, txn_count: int = 0) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "color": p.color,
        "start_date": p.start_date.isoformat() if p.start_date else None,
        "end_date": p.end_date.isoformat() if p.end_date else None,
        "budget": float(p.budget) if p.budget else None,
        "status": p.status,
        "notes": p.notes,
        "spent": spent,
        "txn_count": txn_count,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _effective_date(txn: Transaction):
    return txn.authorized_date or txn.date


def _serialize_transaction(
    txn: Transaction,
    projects: list[dict] | None = None,
    rate_map: dict[str, float] | None = None,
    description: str | None = None,
    splits: list[dict] | None = None,
) -> dict:
    raw_amount = float(txn._amount) if txn._amount else 0
    if rate_map is not None:
        txn_currency = getattr(txn, "currency", None) or "USD"
        rate = rate_map.get(txn_currency, 1.0)
        converted_amount = raw_amount * rate
    else:
        converted_amount = raw_amount
    result = {
        "id": txn.id,
        "date": txn.date.isoformat() if txn.date else None,
        "authorized_date": txn.authorized_date.isoformat() if txn.authorized_date else None,
        "effective_date": _effective_date(txn).isoformat() if _effective_date(txn) else None,
        "amount": converted_amount,
        "merchant_raw": txn.merchant_raw,
        "merchant_clean": txn.merchant_clean,
        "original_description": txn.original_description,
        "category": txn.category,
        "source": txn.source,
        "account_last4": txn.account_last4,
        "pending": bool(txn.pending),
        "is_recurring": txn.is_recurring,
        "tags": txn.tags or [],
        "projects": projects or [],
        # Notes belong to the transaction, not to the project. A project is a grouping, so
        # deleting one must not take the note with it — which is exactly what happened when
        # this lived on transaction_projects.description.
        "notes": txn.notes,
    }
    if description is not None:
        result["description"] = description
    if splits is not None:
        # Shares are stored in the transaction's currency, so convert them with the same
        # rate used for `amount` or a multi-currency project reports inconsistent numbers.
        rate = 1.0
        if rate_map is not None:
            rate = rate_map.get(getattr(txn, "currency", None) or "USD", 1.0)
        converted_splits = []
        has_share = False
        for split in splits:
            raw = split.get("_raw_share")
            entry = {k: v for k, v in split.items() if k != "_raw_share"}
            if raw is None:
                entry["share_amount"] = None
            else:
                entry["share_amount"] = round(raw * rate, 2)
                has_share = True
            converted_splits.append(entry)
        result["splits"] = converted_splits
        result["split_mode"] = "unequal" if has_share else "equal"
    return result


def _member_totals(members: list, serialized_txns: list[dict]) -> list[dict]:
    """Per-person owed totals, in the response currency.

    Single source of truth for "who owes what": the frontend renders this rather than
    re-deriving it, and the future Splitwise commit reads the same numbers. A split with
    explicit share_amounts uses them; otherwise the transaction divides equally, which is
    the long-standing default.
    """
    totals = {m.id: {"expenditure": 0.0, "income": 0.0} for m in members}
    for txn in serialized_txns:
        splits = txn.get("splits") or []
        if not splits:
            continue
        amount = txn.get("amount") or 0
        equal_share = abs(amount) / len(splits)
        for split in splits:
            bucket = totals.get(split["id"])
            if bucket is None:
                continue  # split with someone no longer on the project
            share = split.get("share_amount")
            share = equal_share if share is None else abs(share)
            if amount < 0:
                bucket["expenditure"] += share
            elif amount > 0:
                bucket["income"] += share
    result = []
    for m in members:
        bucket = totals[m.id]
        result.append({
            "id": m.id,
            "name": m.name,
            "color": m.color,
            "expenditure": round(bucket["expenditure"], 2),
            "income": round(bucket["income"], 2),
            "net": round(bucket["income"] - bucket["expenditure"], 2),
        })
    return result


@router.get("/", response_model=list[ProjectItem])
def list_projects(db: Session = Depends(get_db), status: Optional[str] = None, currency: str = Query(...)):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)

    q = db.query(Project)
    if status:
        q = q.filter(Project.status == status)
    projects = q.order_by(Project.created_at.desc()).all()

    if not projects:
        return []

    # Single query to get stats for all projects at once with currency conversion
    project_ids = [p.id for p in projects]
    stats_rows = (
        db.query(
            TransactionProject.project_id,
            func.sum(Transaction._amount * lr.c.rate).label("total_amount"),
            func.count(Transaction.id).label("txn_count"),
            func.min(Transaction.date).label("earliest_date"),
            func.max(Transaction.date).label("latest_date"),
        )
        .join(Transaction, Transaction.id == TransactionProject.transaction_id)
        .join(lr, and_(
            lr.c.from_currency == Transaction.currency,
            lr.c.to_currency == literal(currency),
        ))
        .filter(TransactionProject.project_id.in_(project_ids))
        .group_by(TransactionProject.project_id)
        .all()
    )
    stats_map = {
        row.project_id: row for row in stats_rows
    }

    result = []
    for p in projects:
        stats = stats_map.get(p.id)
        spent = abs(float(stats.total_amount or 0)) if stats else 0
        count = stats.txn_count if stats else 0
        result.append({
            **_serialize_project(p, spent, count),
            "currency": currency,
            "earliest_transaction_date": stats.earliest_date.isoformat() if stats and stats.earliest_date else None,
            "latest_transaction_date": stats.latest_date.isoformat() if stats and stats.latest_date else None,
        })
    return result


@router.post("/")
def create_project(body: ProjectCreate, db: Session = Depends(get_db)):
    p = Project(**body.model_dump(exclude_none=True))
    db.add(p)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if "projects.name" in str(exc.orig):
            raise HTTPException(409, "A project with that name already exists") from exc
        raise
    db.refresh(p)
    return _serialize_project(p)


@router.get("/{project_id}", response_model=ProjectDetailResponse)
def get_project(project_id: str, db: Session = Depends(get_db), currency: str = Query(...)):
    ensure_rates_fresh(db)
    lr = latest_rate_subquery(db)

    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(404, "Project not found")

    # Get transactions
    txns = db.query(Transaction).join(
        TransactionProject, Transaction.id == TransactionProject.transaction_id
    ).filter(TransactionProject.project_id == project_id
    ).order_by(Transaction.date.desc()).all()

    # Compute spent via SQL (sum of negative amounts) with currency conversion
    spent_result = (
        db.query(func.sum(Transaction._amount * lr.c.rate))
        .join(TransactionProject, Transaction.id == TransactionProject.transaction_id)
        .join(lr, and_(
            lr.c.from_currency == Transaction.currency,
            lr.c.to_currency == literal(currency),
        ))
        .filter(TransactionProject.project_id == project_id, Transaction._amount < 0)
        .scalar()
    )
    spent = abs(float(spent_result or 0))

    # Build rate map for transaction serialization
    rate_rows = db.query(lr.c.from_currency, lr.c.rate).filter(lr.c.to_currency == currency).all()
    rate_map: dict[str, float] = {row[0]: float(row[1]) for row in rate_rows}

    projects_by_txn: dict[str, list[dict]] = {}
    descriptions_by_txn: dict[str, str] = {}
    splits_by_txn: dict[str, list[dict]] = {}
    if txns:
        txn_ids = [t.id for t in txns]
        links = db.query(TransactionProject.transaction_id, TransactionProject.description, Project.id, Project.name, Project.color).join(
            Project, Project.id == TransactionProject.project_id
        ).filter(TransactionProject.transaction_id.in_(txn_ids)).all()
        for txn_id, desc, proj_id, proj_name, proj_color in links:
            projects_by_txn.setdefault(txn_id, []).append({
                "id": proj_id,
                "name": proj_name,
                "color": proj_color,
            })
            if proj_id == project_id and desc:
                descriptions_by_txn[txn_id] = desc

        split_rows = (
            db.query(
                TransactionSplit.transaction_id,
                Contact.id,
                Contact.name,
                Contact.color,
                TransactionSplit._share_amount,
            )
            .join(Contact, Contact.id == TransactionSplit.contact_id)
            # No project filter: the split is the transaction's, and the same split shows
            # wherever the transaction appears.
            .filter(TransactionSplit.transaction_id.in_(txn_ids))
            .all()
        )
        for txn_id, contact_id, contact_name, contact_color, share in split_rows:
            splits_by_txn.setdefault(txn_id, []).append({
                "id": contact_id,
                "name": contact_name,
                "color": contact_color,
                # Raw, in the transaction's currency. Converted in _serialize_transaction.
                "_raw_share": float(share) if share is not None else None,
            })

    # Category breakdown with top-level + subcategory grouping (converted amounts)
    cats: dict[str, dict] = {}
    for t in txns:
        raw_amount = float(t._amount or 0)
        if raw_amount >= 0:
            continue
        txn_currency = getattr(t, "currency", None) or "USD"
        rate = rate_map.get(txn_currency, 1.0)
        converted = raw_amount * rate
        cat = t.category or "Uncategorized"
        top, _, sub = cat.partition("/")
        top = top or "Uncategorized"
        bucket = cats.setdefault(top, {"category": top, "total": 0.0, "subcategories": {}})
        magnitude = abs(converted)
        bucket["total"] += magnitude
        subkey = sub or "Uncategorized"
        bucket["subcategories"][subkey] = bucket["subcategories"].get(subkey, 0.0) + magnitude

    # Project members
    members = (
        db.query(Contact)
        .join(ProjectMember, ProjectMember.contact_id == Contact.id)
        .filter(ProjectMember.project_id == project_id)
        .order_by(Contact.name)
        .all()
    )

    serialized_txns = [
        _serialize_transaction(
            t,
            projects_by_txn.get(t.id, []),
            rate_map=rate_map,
            description=descriptions_by_txn.get(t.id),
            splits=splits_by_txn.get(t.id, []),
        )
        for t in txns
    ]

    return {
        **_serialize_project(p, spent, len(txns)),
        "currency": currency,
        "earliest_transaction_date": txns[-1].date.isoformat() if txns else None,
        "latest_transaction_date": txns[0].date.isoformat() if txns else None,
        "members": [{"id": m.id, "name": m.name, "color": m.color} for m in members],
        "categories": [
            {
                "category": value["category"],
                "total": round(value["total"], 2),
                "subcategories": [
                    {"name": name, "total": round(total, 2)}
                    for name, total in sorted(value["subcategories"].items(), key=lambda item: item[1], reverse=True)
                ],
            }
            for value in sorted(cats.values(), key=lambda item: item["total"], reverse=True)
        ],
        "transactions": serialized_txns,
        "member_totals": _member_totals(members, serialized_txns),
    }


@router.patch("/{project_id}")
def update_project(project_id: str, body: ProjectUpdate, db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(404, "Project not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(p, k, v)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if "projects.name" in str(exc.orig):
            raise HTTPException(409, "A project with that name already exists") from exc
        raise
    db.refresh(p)
    return _serialize_project(p)


@router.delete("/{project_id}")
def delete_project(project_id: str, db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(404, "Project not found")
    # bulk_delete, not query.delete(): a raw DELETE never loads the rows, so the tombstone hook
    # cannot see them and the links would come back on the next merge.
    bulk_delete(db, TransactionProject, project_id=project_id)
    db.delete(p)
    db.commit()
    return {"ok": True}


@router.post("/{project_id}/transactions")
def add_transactions(project_id: str, body: ProjectTransactionsUpdate, db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(404, "Project not found")
    ids = body.transaction_ids
    if not ids:
        return {"added": 0}
    existing_ids = set(
        row[0] for row in
        db.query(TransactionProject.transaction_id)
        .filter(TransactionProject.project_id == project_id, TransactionProject.transaction_id.in_(ids))
        .all()
    )
    member_ids = [
        row[0] for row in
        db.query(ProjectMember.contact_id).filter(ProjectMember.project_id == project_id).all()
    ]
    # Project members are the *default* for a transaction that has no split yet. They must
    # not overwrite an existing split: that split is the transaction's own, and filing the
    # transaction under another project does not change who owes what.
    already_split = {
        row[0] for row in
        db.query(TransactionSplit.transaction_id)
        .filter(TransactionSplit.transaction_id.in_(ids))
        .distinct()
        .all()
    } if ids else set()
    added = 0
    for tid in ids:
        if tid not in existing_ids:
            db.add(TransactionProject(transaction_id=tid, project_id=project_id))
            if tid not in already_split:
                for contact_id in member_ids:
                    db.add(TransactionSplit(transaction_id=tid, contact_id=contact_id))
            added += 1
    db.commit()
    return {"added": added}


@router.delete("/{project_id}/transactions/{txn_id}")
def remove_transaction(project_id: str, txn_id: str, db: Session = Depends(get_db)):
    # Deliberately leaves the split alone. Taking a transaction out of a grouping does not
    # settle the debt, and deleting it here is how split data used to disappear silently.
    r = db.query(TransactionProject).filter_by(transaction_id=txn_id, project_id=project_id).first()
    if r:
        db.delete(r)
        db.commit()
    return {"ok": True}


class TransactionPatchBody(BaseModel):
    description: Optional[str] = None


@router.patch("/{project_id}/transactions/{txn_id}")
def update_transaction_project(project_id: str, txn_id: str, body: TransactionPatchBody, db: Session = Depends(get_db)):
    link = db.query(TransactionProject).filter_by(transaction_id=txn_id, project_id=project_id).first()
    if not link:
        raise HTTPException(404, "Transaction not in this project")
    if body.description is not None:
        link.description = body.description or None
    db.commit()
    return {"ok": True}


class SplitsUpdateBody(BaseModel):
    contact_ids: list[str]
    # contact_id -> share, in the transaction's own currency. Omit entirely for an equal
    # split, which stays the default. Partial maps are rejected rather than silently mixed.
    share_amounts: dict[str, float] | None = None


# Shares are entered by hand, so allow a cent of float/rounding slack against the total.
_SHARE_TOLERANCE = Decimal("0.01")


@router.patch("/{project_id}/transactions/{txn_id}/splits")
def update_transaction_splits(project_id: str, txn_id: str, body: SplitsUpdateBody, db: Session = Depends(get_db)):
    link = db.query(TransactionProject).filter_by(transaction_id=txn_id, project_id=project_id).first()
    if not link:
        raise HTTPException(404, "Transaction not in this project")

    shares = body.share_amounts or None
    if shares:
        contact_ids = set(body.contact_ids)
        unknown = sorted(set(shares) - contact_ids)
        if unknown:
            raise HTTPException(422, f"Share given for contacts not in the split: {', '.join(unknown)}")
        missing = sorted(contact_ids - set(shares))
        if missing:
            raise HTTPException(422, f"Every person in an unequal split needs a share; missing: {', '.join(missing)}")
        if any(value < 0 for value in shares.values()):
            raise HTTPException(422, "Shares cannot be negative")

        txn = db.query(Transaction).filter(Transaction.id == txn_id).first()
        if not txn:
            raise HTTPException(404, "Transaction not found")
        # Compare in the transaction's own currency, which is how shares are stored.
        total = abs(Decimal(str(txn._amount or 0)))
        assigned = sum((Decimal(str(v)) for v in shares.values()), Decimal("0"))
        if abs(assigned - total) > _SHARE_TOLERANCE:
            raise HTTPException(
                422,
                f"Shares must add up to {total}; got {assigned}",
            )

    bulk_delete(db, TransactionSplit, transaction_id=txn_id)
    for contact_id in body.contact_ids:
        row = TransactionSplit(transaction_id=txn_id, contact_id=contact_id)
        if shares:
            row.share_amount = Decimal(str(shares[contact_id]))
        db.add(row)
    db.commit()
    return {"ok": True}


# --- Project Members ---

class MembersUpdateBody(BaseModel):
    contact_ids: list[str]


@router.post("/{project_id}/members")
def add_project_members(project_id: str, body: MembersUpdateBody, db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p:
        raise HTTPException(404, "Project not found")
    existing = set(
        row[0] for row in
        db.query(ProjectMember.contact_id).filter(ProjectMember.project_id == project_id).all()
    )
    added = 0
    for cid in body.contact_ids:
        if cid not in existing:
            db.add(ProjectMember(project_id=project_id, contact_id=cid))
            added += 1
    db.commit()
    return {"added": added}


@router.delete("/{project_id}/members/{contact_id}")
def remove_project_member(project_id: str, contact_id: str, db: Session = Depends(get_db)):
    bulk_delete(db, ProjectMember, project_id=project_id, contact_id=contact_id)
    db.commit()
    return {"ok": True}


# --- Contacts (global) ---

contacts_router = APIRouter()


@contacts_router.get("/")
def list_contacts(db: Session = Depends(get_db)):
    contacts = db.query(Contact).order_by(Contact.name).all()
    return [{"id": c.id, "name": c.name, "color": c.color} for c in contacts]


class ContactCreateBody(BaseModel):
    name: str
    color: str = "#6366f1"


@contacts_router.post("/")
def create_contact(body: ContactCreateBody, db: Session = Depends(get_db)):
    c = Contact(name=body.name.strip(), color=body.color)
    db.add(c)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A contact with that name already exists")
    db.refresh(c)
    return {"id": c.id, "name": c.name, "color": c.color}


class ContactUpdateBody(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None


@contacts_router.patch("/{contact_id}")
def update_contact(contact_id: str, body: ContactUpdateBody, db: Session = Depends(get_db)):
    c = db.query(Contact).filter(Contact.id == contact_id).first()
    if not c:
        raise HTTPException(404, "Contact not found")
    if body.name is not None:
        c.name = body.name.strip()
    if body.color is not None:
        c.color = body.color
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A contact with that name already exists")
    db.refresh(c)
    return {"id": c.id, "name": c.name, "color": c.color}


@contacts_router.get("/{contact_id}/usage")
def contact_usage(contact_id: str, db: Session = Depends(get_db)):
    split_count = db.query(TransactionSplit).filter_by(contact_id=contact_id).count()
    project_count = db.query(ProjectMember).filter_by(contact_id=contact_id).count()
    return {"split_count": split_count, "project_count": project_count}


@contacts_router.delete("/{contact_id}")
def delete_contact(contact_id: str, db: Session = Depends(get_db)):
    # Children first, so the contact's name is still readable when the tombstone hook names them.
    bulk_delete(db, TransactionSplit, contact_id=contact_id)
    bulk_delete(db, ProjectMember, contact_id=contact_id)
    c = db.query(Contact).filter(Contact.id == contact_id).first()
    if c:
        db.delete(c)
    db.commit()
    return {"ok": True}
