from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import and_, func, literal
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel
from typing import Optional
from datetime import date

from src.models import get_db, Project, TransactionProject, Transaction, ExchangeRate, Contact, ProjectMember, TransactionProjectSplit
from src.services.exchange_rates import latest_rate_subquery, ensure_rates_fresh
from src.api.schemas import ProjectItem, ProjectDetailResponse

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
    }
    if description is not None:
        result["description"] = description
    if splits is not None:
        result["splits"] = splits
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
            db.query(TransactionProjectSplit.transaction_id, Contact.id, Contact.name, Contact.color)
            .join(Contact, Contact.id == TransactionProjectSplit.contact_id)
            .filter(
                TransactionProjectSplit.project_id == project_id,
                TransactionProjectSplit.transaction_id.in_(txn_ids),
            )
            .all()
        )
        for txn_id, contact_id, contact_name, contact_color in split_rows:
            splits_by_txn.setdefault(txn_id, []).append({
                "id": contact_id,
                "name": contact_name,
                "color": contact_color,
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
        "transactions": [
            _serialize_transaction(
                t,
                projects_by_txn.get(t.id, []),
                rate_map=rate_map,
                description=descriptions_by_txn.get(t.id),
                splits=splits_by_txn.get(t.id, []),
            )
            for t in txns
        ],
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
    db.query(TransactionProject).filter(TransactionProject.project_id == project_id).delete()
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
    added = 0
    for tid in ids:
        if tid not in existing_ids:
            db.add(TransactionProject(transaction_id=tid, project_id=project_id))
            for contact_id in member_ids:
                db.add(TransactionProjectSplit(transaction_id=tid, project_id=project_id, contact_id=contact_id))
            added += 1
    db.commit()
    return {"added": added}


@router.delete("/{project_id}/transactions/{txn_id}")
def remove_transaction(project_id: str, txn_id: str, db: Session = Depends(get_db)):
    db.query(TransactionProjectSplit).filter_by(transaction_id=txn_id, project_id=project_id).delete()
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


@router.patch("/{project_id}/transactions/{txn_id}/splits")
def update_transaction_splits(project_id: str, txn_id: str, body: SplitsUpdateBody, db: Session = Depends(get_db)):
    link = db.query(TransactionProject).filter_by(transaction_id=txn_id, project_id=project_id).first()
    if not link:
        raise HTTPException(404, "Transaction not in this project")
    db.query(TransactionProjectSplit).filter_by(transaction_id=txn_id, project_id=project_id).delete()
    for contact_id in body.contact_ids:
        db.add(TransactionProjectSplit(transaction_id=txn_id, project_id=project_id, contact_id=contact_id))
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
    db.query(ProjectMember).filter_by(project_id=project_id, contact_id=contact_id).delete()
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
    split_count = db.query(TransactionProjectSplit).filter_by(contact_id=contact_id).count()
    project_count = db.query(ProjectMember).filter_by(contact_id=contact_id).count()
    return {"split_count": split_count, "project_count": project_count}


@contacts_router.delete("/{contact_id}")
def delete_contact(contact_id: str, db: Session = Depends(get_db)):
    db.query(TransactionProjectSplit).filter_by(contact_id=contact_id).delete()
    db.query(ProjectMember).filter_by(contact_id=contact_id).delete()
    c = db.query(Contact).filter(Contact.id == contact_id).first()
    if c:
        db.delete(c)
    db.commit()
    return {"ok": True}
