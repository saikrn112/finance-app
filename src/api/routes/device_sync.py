"""Explicit device-sync controls, distinct from refreshing connected banks."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from src.models import get_db
from src.models.transaction import SyncLog, SyncDevice
from src.services import job_lock, device_sync_health
from src.sync.runner import convergence_problem, sync_enabled, sync_once

router = APIRouter()

@router.get("/status")
def status(db: Session = Depends(get_db)):
    enabled = sync_enabled()
    google = db.query(SyncLog).filter(SyncLog.source == "vault_google", SyncLog.sync_type == "google_drive", SyncLog.status == "connected").order_by(SyncLog.created_at.desc()).first()
    configured = bool(google and (google.extra_data or {}).get("refresh_token"))
    state = device_sync_health.read(db)
    return {"enabled": enabled, "configured": configured,
            "reason": "Device sync is paused." if not enabled else
                      ("Connect Google Drive in Settings to sync devices." if not configured else "Google Drive is configured."),
            "last_success": state.get("last_success"), "last_attempt": state.get("last_attempt"),
            "last_error": state.get("last_error"),
            "peer_count": db.query(SyncDevice).count(), "busy": job_lock.current_holder() is not None}

@router.post("/run")
def run(db: Session = Depends(get_db)):
    if not sync_enabled():
        raise HTTPException(409, "Device sync is paused. Enable it first.")
    with job_lock.try_acquire("device sync") as acquired:
        if not acquired:
            raise HTTPException(409, "Another job is running. Try again shortly.")
        try:
            result = sync_once(db)
        except Exception:
            db.rollback()
            device_sync_health.record(db, successful=False)
            raise HTTPException(502, "Device sync could not complete. Check Google Drive in Settings.")
    return {"ran": result.get("ran", False), "completed": convergence_problem(result) is None,
            "peer_count": len(result.get("peers_seen", []))}
