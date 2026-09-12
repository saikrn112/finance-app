"""Splitwise connect, contact linking, and staged commits.

Entirely optional: with no credentials configured every endpoint reports
`configured: false` and nothing else in the app changes.
"""
from __future__ import annotations

from datetime import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.integrations import splitwise
from src.models import Contact, ContactSplitwiseLink, Project, SplitwiseCommit, SyncLog, get_db
from src.services import job_lock
from src.services import splitwise_commit as commit_service
from src.services import splitwise_credentials as creds_store

router = APIRouter()
logger = logging.getLogger(__name__)

_SOURCE = "splitwise"


def _connection(db: Session) -> SyncLog | None:
    return (
        db.query(SyncLog)
        .filter(SyncLog.source == _SOURCE, SyncLog.sync_type == _SOURCE, SyncLog.status == "connected")
        .order_by(SyncLog.created_at.desc())
        .first()
    )


def _access(db: Session) -> tuple[SyncLog, str]:
    creds = creds_store.resolve(db)
    log = _connection(db)
    if creds.has_api_key:
        # A personal API key needs no connect step; synthesise a connection row so the rest
        # of the flow (links, commits) works identically.
        if not log:
            log = SyncLog(source=_SOURCE, sync_type=_SOURCE, status="connected",
                          extra_data={"provider": "splitwise", "auth_mode": "api_key"})
            db.add(log)
            db.commit()
        return log, creds.api_key
    if not log:
        raise HTTPException(status_code=400, detail="Connect Splitwise first")
    token, updated = splitwise.ensure_fresh_access_token(dict(log.extra_data or {}), creds)
    if updated != (log.extra_data or {}):
        log.extra_data = updated
        db.commit()
    return log, token


@router.get("/status")
def status(db: Session = Depends(get_db)):
    log = _connection(db)
    extra = (log.extra_data or {}) if log else {}
    me = commit_service.self_contact(db)
    credentials = creds_store.describe(db)
    return {
        "configured": credentials["usable"],
        "credentials": credentials,
        "connected": bool(log) or credentials["auth_mode"] == "api_key",
        "account_name": extra.get("name"),
        "account_email": extra.get("email"),
        "self_contact": {"id": me.id, "name": me.name} if me else None,
        "batch_size": commit_service.settings.splitwise.commit_batch_size,
    }


class CredentialsBody(BaseModel):
    client_id: str | None = None
    client_secret: str | None = None
    redirect_uri: str | None = None
    api_key: str | None = None


@router.put("/credentials")
def save_credentials(body: CredentialsBody, db: Session = Depends(get_db)):
    """Store app credentials entered in the UI. Secrets are never read back out."""
    creds_store.save(
        db,
        client_id=body.client_id,
        client_secret=body.client_secret,
        redirect_uri=body.redirect_uri,
        api_key=body.api_key,
    )
    return creds_store.describe(db)


@router.delete("/credentials")
def clear_credentials(db: Session = Depends(get_db)):
    creds_store.clear(db)
    return creds_store.describe(db)


@router.get("/start")
def start(db: Session = Depends(get_db)):
    """Redirect into Splitwise's consent screen."""
    return RedirectResponse(splitwise.create_auth_url(creds_store.resolve(db)))


@router.get("/callback")
def callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    if error:
        return HTMLResponse(_close_page(f"Splitwise sign-in failed: {error}"), status_code=400)
    if not code or not state:
        return HTMLResponse(_close_page("Splitwise sign-in was cancelled."), status_code=400)

    splitwise.validate_state(state)
    try:
        token_response = splitwise.exchange_code(code, creds_store.resolve(db))
        user = splitwise.get_current_user(token_response["access_token"])
    finally:
        splitwise.delete_pending_state(state)

    extra = splitwise.token_payload_to_extra(token_response, user)
    existing = _connection(db)
    if existing:
        existing.extra_data = {**(existing.extra_data or {}), **extra}
    else:
        db.add(SyncLog(source=_SOURCE, sync_type=_SOURCE, status="connected", extra_data=extra))
    db.commit()
    return HTMLResponse(_close_page("Splitwise connected. You can close this window."))


@router.post("/disconnect")
def disconnect(db: Session = Depends(get_db)):
    """Forget the token only.

    Credentials, contact links, and commit history all survive, so reconnecting resumes
    rather than starting over. Use DELETE /credentials to remove the app credentials.
    """
    for log in db.query(SyncLog).filter(
        SyncLog.source == _SOURCE, SyncLog.sync_type == _SOURCE
    ).all():
        db.delete(log)
    db.commit()
    return {"ok": True}


@router.get("/friends")
def friends(db: Session = Depends(get_db)):
    _, token = _access(db)
    links = commit_service.contact_links(db)
    linked_ids = set(links.values())
    return {
        "friends": [
            {
                "id": str(f.get("id")),
                "name": " ".join(filter(None, [f.get("first_name"), f.get("last_name")])) or f.get("email"),
                "email": f.get("email"),
                "already_linked": str(f.get("id")) in linked_ids,
            }
            for f in splitwise.get_friends(token)
        ]
    }


@router.get("/links")
def links(db: Session = Depends(get_db)):
    """Every contact and its Splitwise mapping, so the UI can show gaps at a glance."""
    rows = {r.contact_id: r for r in db.query(ContactSplitwiseLink).all()}
    contacts = db.query(Contact).order_by(Contact.name).all()
    return {
        "contacts": [
            {
                "id": c.id,
                "name": c.name,
                "color": c.color,
                "is_self": bool(c.is_self),
                "splitwise_user_id": rows[c.id].splitwise_user_id if c.id in rows else None,
                "splitwise_name": rows[c.id].display_name if c.id in rows else None,
            }
            for c in contacts
        ]
    }


class LinkBody(BaseModel):
    splitwise_user_id: str
    display_name: str | None = None


@router.put("/links/{contact_id}")
def link_contact(contact_id: str, body: LinkBody, db: Session = Depends(get_db)):
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(404, "Contact not found")
    row = db.query(ContactSplitwiseLink).filter_by(contact_id=contact_id).first()
    if row is None:
        row = ContactSplitwiseLink(contact_id=contact_id)
        db.add(row)
    row.splitwise_user_id = body.splitwise_user_id
    row.display_name = body.display_name
    row.linked_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.delete("/links/{contact_id}")
def unlink_contact(contact_id: str, db: Session = Depends(get_db)):
    db.query(ContactSplitwiseLink).filter_by(contact_id=contact_id).delete()
    db.commit()
    return {"ok": True}


@router.put("/self/{contact_id}")
def set_self_contact(contact_id: str, db: Session = Depends(get_db)):
    """Mark who the account owner is. Splitwise needs a payer for every expense."""
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(404, "Contact not found")
    db.query(Contact).filter(Contact.is_self.is_(True)).update({"is_self": False})
    contact.is_self = True
    db.commit()
    return {"ok": True, "self_contact": {"id": contact.id, "name": contact.name}}


@router.get("/projects/{project_id}/preview")
def preview(project_id: str, db: Session = Depends(get_db)):
    if not db.query(Project).filter(Project.id == project_id).first():
        raise HTTPException(404, "Project not found")
    summary = commit_service.pending_summary(db, project_id)
    summary["connected"] = bool(_connection(db))
    return summary


@router.post("/projects/{project_id}/commit")
def commit(
    project_id: str,
    amend: bool = Query(False, description="Re-push transactions whose shares changed since they were committed"),
    db: Session = Depends(get_db),
):
    """Commit one batch. Call again to drain the rest of the queue."""
    _, token = _access(db)
    # Shares the lock with sync and backup: this holds write transactions across network
    # calls, which is exactly what caused "database is locked" before.
    with job_lock.try_acquire("splitwise commit") as acquired:
        if not acquired:
            raise HTTPException(
                status_code=409,
                detail=f"Busy: {job_lock.current_holder() or 'another job'} is running",
            )
        return commit_service.commit_project(db, project_id, token, amend=amend)


@router.post("/projects/{project_id}/reset")
def reset(project_id: str, db: Session = Depends(get_db)):
    """Forget local commit records so the project can be pushed again.

    Does not delete anything in Splitwise — that has to be done there, on purpose.
    """
    deleted = db.query(SplitwiseCommit).filter_by(project_id=project_id).delete()
    project = db.query(Project).filter(Project.id == project_id).first()
    if project:
        project.splitwise_group_id = None
    db.commit()
    return {"ok": True, "cleared": deleted}


def _close_page(message: str) -> str:
    return (
        "<!doctype html><html><body style=\"font-family:system-ui;padding:2rem\">"
        f"<p>{message}</p>"
        "<script>window.opener&&window.opener.postMessage({type:'splitwise-connected'},'*');"
        "setTimeout(()=>window.close(),1200);</script>"
        "</body></html>"
    )
