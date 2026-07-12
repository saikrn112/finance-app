from datetime import datetime
import json
import logging
import threading
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models import (
    get_db,
    Transaction,
    Subscription,
    Rule,
    SyncLog,
    Balance,
    AccountSnapshot,
    InvestmentHoldingSnapshot,
    PlaidApiUsage,
    Project,
    TransactionProject,
    SessionLocal,
    PayslipLineItem,
    Payslip,
    RetirementTransaction,
    RetirementStatement,
)
from src.config import settings
from src.models.database import engine, pause_dirty_tracking, resume_dirty_tracking
from src.ingestion.plaid_usage import plaid_usage_summary
from pathlib import Path
import tempfile

from src.vault.backup import (
    BACKUP_MANIFEST_NAME,
    build_backup_bundle,
    clear_local_vault_state,
    ensure_vault_metadata,
    load_vault_metadata,
    manifest_filename,
    mark_backup_succeeded,
    restore_backup_bundle,
)
from src.vault.google_drive import (
    create_google_auth_url,
    delete_google_pending_state,
    download_file_bytes,
    download_file_to_path,
    ensure_fresh_google_access_token,
    ensure_child_folder,
    ensure_visible_app_folder,
    exchange_google_code,
    list_drive_files,
    token_payload_to_extra,
    upload_file_resumable,
    upload_multipart_file,
    validate_google_state,
)

router = APIRouter()
logger = logging.getLogger(__name__)
_backup_jobs: dict[str, dict] = {}
_backup_jobs_lock = threading.Lock()
_restore_jobs: dict[str, dict] = {}
_restore_jobs_lock = threading.Lock()


def _plaid_institution_key(log: SyncLog) -> str:
    institution = ((log.extra_data or {}).get("institution_name") or "").strip()
    if institution:
        return institution.casefold()
    return (log.plaid_item_id or log.id or "unknown").casefold()


def _connected_institutions(db: Session) -> list[SyncLog]:
    logs = (
        db.query(SyncLog)
        .filter(SyncLog.source == "plaid", SyncLog.status == "connected")
        .order_by(SyncLog.created_at.desc())
        .all()
    )

    grouped: dict[str, SyncLog] = {}
    for log in logs:
        key = _plaid_institution_key(log)
        grouped.setdefault(key, log)
    return list(grouped.values())


def _google_drive_log(db: Session) -> SyncLog | None:
    return (
        db.query(SyncLog)
        .filter(SyncLog.source == "vault_google", SyncLog.sync_type == "google_drive", SyncLog.status == "connected")
        .order_by(SyncLog.created_at.desc())
        .first()
    )


def _vault_status(db: Session) -> dict:
    metadata = load_vault_metadata()
    log = _google_drive_log(db)
    extra = dict(log.extra_data or {}) if log else {}
    return {
        "vault_id": metadata.get("vault_id"),
        "provider": metadata.get("provider"),
        "provider_email": metadata.get("provider_email") or extra.get("email"),
        "connected": bool(log),
        "last_backup_at": metadata.get("last_backup_at"),
        "last_backup_file_id": metadata.get("last_backup_file_id"),
        "drive_folder_name": extra.get("drive_folder_name"),
        "drive_folder_id": extra.get("drive_folder_id"),
        "last_backup_id": metadata.get("last_backup_id"),
        "last_restore_at": metadata.get("last_restore_at"),
        "google_drive_ready": bool(log and extra.get("access_token")),
    }


def _google_access(db: Session) -> tuple[SyncLog, str, dict]:
    log = _google_drive_log(db)
    if not log:
        raise HTTPException(status_code=400, detail="Connect Google Drive first")
    access_token, updated_extra = ensure_fresh_google_access_token(dict(log.extra_data or {}))
    log.extra_data = updated_extra
    return log, access_token, updated_extra


def _google_access_ephemeral(db: Session) -> tuple[SyncLog, str, dict]:
    log = _google_drive_log(db)
    if not log:
        raise HTTPException(status_code=400, detail="Connect Google Drive first")
    access_token, updated_extra = ensure_fresh_google_access_token(dict(log.extra_data or {}))
    return log, access_token, updated_extra


def _vault_folder_hierarchy(access_token: str, vault_id: str) -> dict:
    root_folder = ensure_visible_app_folder(access_token)
    vault_folder = ensure_child_folder(access_token, parent_id=root_folder["id"], name=vault_id)
    backups_folder = ensure_child_folder(access_token, parent_id=vault_folder["id"], name="backups")
    return {"root": root_folder, "vault": vault_folder, "backups": backups_folder}


def _list_google_backups(access_token: str, vault_id: str) -> list[dict]:
    folders = _vault_folder_hierarchy(access_token, vault_id)
    backup_folders = list_drive_files(access_token, parent_id=folders["backups"]["id"], mime_type="application/vnd.google-apps.folder")
    backups: list[dict] = []
    for backup_folder in backup_folders:
        manifest_file = next(
            iter(list_drive_files(access_token, parent_id=backup_folder["id"], name=BACKUP_MANIFEST_NAME)),
            None,
        )
        archive_file = next(
            iter(list_drive_files(access_token, parent_id=backup_folder["id"])),
            None,
        )
        if not manifest_file:
            continue
        try:
            manifest = json.loads(download_file_bytes(access_token, manifest_file["id"]).decode())
        except Exception:
            continue
        archive_candidates = list_drive_files(access_token, parent_id=backup_folder["id"], name=manifest.get("archive_name"))
        archive = archive_candidates[0] if archive_candidates else archive_file
        backups.append(
            {
                "backup_id": manifest.get("backup_id"),
                "parent_backup_id": manifest.get("parent_backup_id"),
                "created_at": manifest.get("created_at"),
                "device_id": manifest.get("device_id"),
                "device_label": manifest.get("device_label"),
                "archive_name": manifest.get("archive_name"),
                "archive_file_id": archive.get("id") if archive else None,
                "archive_size": int(archive.get("size") or 0) if archive else 0,
                "manifest_file_id": manifest_file["id"],
                "archive_sha256": manifest.get("archive_sha256"),
                "manifest": manifest,
            }
        )
    backups.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return backups


def _discover_google_vaults(access_token: str) -> list[dict]:
    root_folder = ensure_visible_app_folder(access_token)
    vault_folders = list_drive_files(access_token, parent_id=root_folder["id"], mime_type="application/vnd.google-apps.folder")
    vaults: list[dict] = []
    for vault_folder in vault_folders:
        latest_candidates = list_drive_files(access_token, parent_id=vault_folder["id"])
        latest_manifest_file = next(
            (item for item in latest_candidates if item.get("name", "").endswith("-latest.manifest.json")),
            None,
        )
        latest_manifest = None
        if latest_manifest_file:
            try:
                latest_manifest = json.loads(download_file_bytes(access_token, latest_manifest_file["id"]).decode())
            except Exception:
                latest_manifest = None
        vaults.append(
            {
                "vault_id": vault_folder["name"],
                "folder_id": vault_folder["id"],
                "latest_backup_id": (latest_manifest or {}).get("latest_backup_id"),
                "latest_backup_created_at": (latest_manifest or {}).get("latest_backup_created_at"),
                "primary": bool((latest_manifest or {}).get("primary")),
            }
        )
    vaults.sort(key=lambda v: v.get("latest_backup_created_at") or "", reverse=True)
    primary = [v for v in vaults if v.get("primary")]
    others = [v for v in vaults if not v.get("primary")]
    return primary + others


def _pick_restore_target_vault(access_token: str, requested_vault_id: str | None) -> str:
    if requested_vault_id:
        requested_backups = _list_google_backups(access_token, requested_vault_id)
        if requested_backups:
            return requested_vault_id
    discovered = _discover_google_vaults(access_token)
    for vault in discovered:
        if vault.get("latest_backup_id"):
            return vault["vault_id"]
    if requested_vault_id:
        return requested_vault_id
    raise HTTPException(status_code=404, detail="No Google Drive backups found for any discovered vault")


def _backup_job_snapshot(job_id: str) -> dict | None:
    with _backup_jobs_lock:
        job = _backup_jobs.get(job_id)
        return dict(job) if job else None


def _set_backup_job(job_id: str, **updates) -> dict:
    with _backup_jobs_lock:
        current = dict(_backup_jobs.get(job_id) or {})
        current["job_id"] = job_id
        current.update(updates)
        _backup_jobs[job_id] = current
        return dict(current)


def _active_backup_job() -> dict | None:
    with _backup_jobs_lock:
        for job in _backup_jobs.values():
            if job.get("status") == "running":
                return dict(job)
    return None


def _clear_backup_jobs() -> None:
    with _backup_jobs_lock:
        _backup_jobs.clear()


def _set_restore_job(job_id: str, **updates) -> dict:
    with _restore_jobs_lock:
        current = dict(_restore_jobs.get(job_id) or {})
        current["job_id"] = job_id
        current.update(updates)
        _restore_jobs[job_id] = current
        return dict(current)


def _restore_job_snapshot(job_id: str) -> dict | None:
    with _restore_jobs_lock:
        job = _restore_jobs.get(job_id)
        return dict(job) if job else None


def _clear_data_directory() -> None:
    import shutil
    data_dir = Path(settings.app.data_dir)
    runtime_dir = Path(settings.app.runtime_dir)
    for child in sorted(data_dir.rglob("*"), reverse=True):
        if child == runtime_dir or runtime_dir in child.parents:
            if child.suffix == ".db" or child.name in ("vault_metadata.json", "restore_receipt.json"):
                child.unlink(missing_ok=True)
            continue
        if child.is_file():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            try:
                child.rmdir()
            except OSError:
                pass


def _wipe_local_database(db: Session) -> None:
    for model in (
        PayslipLineItem,
        Payslip,
        RetirementTransaction,
        RetirementStatement,
        TransactionProject,
        Project,
        PlaidApiUsage,
        InvestmentHoldingSnapshot,
        AccountSnapshot,
        Balance,
        SyncLog,
        Rule,
        Subscription,
        Transaction,
    ):
        db.query(model).delete(synchronize_session=False)
    db.commit()


@router.get("/")
def get_settings(db: Session = Depends(get_db)):
    """Get app settings and stats."""
    txn_count = db.query(func.count(Transaction.id)).scalar()
    accounts = _connected_institutions(db)

    return {
        "stats": {
            "total_transactions": txn_count,
            "connected_accounts": len(accounts),
        },
        "display_currency": settings.app.display_currency,
        "vault": _vault_status(db),
        "plaid_usage": plaid_usage_summary(db),
        "accounts": [
            {
                "id": account.id,
                "plaid_item_id": account.plaid_item_id,
                "source": (account.extra_data or {}).get("institution_name", "Unknown"),
                "sync_type": account.sync_type,
                "status": account.status,
                "last_sync": ((account.extra_data or {}).get("last_sync_at") or (account.created_at.isoformat() if account.created_at else None)),
                "records_synced": int(account.record_count) if account.record_count else 0,
                "institution_name": (account.extra_data or {}).get("institution_name", ""),
                "last_sync_error": (account.extra_data or {}).get("last_sync_error"),
            }
            for account in accounts
        ],
    }


@router.delete("/accounts/{account_id}")
def disconnect_account(account_id: str, db: Session = Depends(get_db)):
    """Disconnect a linked account."""
    log = db.query(SyncLog).filter(SyncLog.id == account_id).first()
    if log:
        if log.source == "plaid" and log.plaid_item_id:
            institution_key = _plaid_institution_key(log)
            linked_logs = db.query(SyncLog).filter(
                SyncLog.source == "plaid",
                SyncLog.status == "connected",
            ).all()
            delete_ids = [
                item.id for item in linked_logs
                if _plaid_institution_key(item) == institution_key
            ]
            if delete_ids:
                db.query(SyncLog).filter(SyncLog.id.in_(delete_ids)).delete(synchronize_session=False)
            else:
                db.query(SyncLog).filter(SyncLog.plaid_item_id == log.plaid_item_id).delete()
        else:
            db.delete(log)
        db.commit()
        return {"status": "disconnected"}
    return {"status": "not_found"}


@router.delete("/transactions")
def clear_transactions(source: str = None, db: Session = Depends(get_db)):
    """Clear transactions, optionally by source."""
    query = db.query(Transaction)
    if source:
        query = query.filter(Transaction.source == source)
    count = query.delete()
    db.commit()
    return {"deleted": count}


@router.get("/signout/preflight")
def signout_preflight(db: Session = Depends(get_db)):
    metadata = load_vault_metadata()
    last_backup_at = metadata.get("last_backup_at")
    txn_count = db.query(func.count(Transaction.id)).scalar()
    needs_backup = bool(metadata.get("dirty"))
    return {
        "needs_backup": needs_backup,
        "transaction_count": txn_count,
        "last_backup_at": last_backup_at,
    }


@router.post("/signout")
def sign_out_and_reset_local_state(db: Session = Depends(get_db)):
    from src.models.database import init_db

    active = _active_backup_job()
    if active:
        raise HTTPException(status_code=409, detail="Backup is currently running")

    db.close()
    engine.dispose()
    _clear_data_directory()
    clear_local_vault_state()
    _clear_backup_jobs()
    Path(settings.database.path).parent.mkdir(parents=True, exist_ok=True)
    init_db()
    return {"status": "signed_out"}


@router.get("/vault/google/start")
def start_google_drive_connect():
    return RedirectResponse(create_google_auth_url())


@router.get("/vault/google/callback")
def finish_google_drive_connect(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db),
):
    return complete_google_drive_connect(code=code, state=state, db=db)


def complete_google_drive_connect(code: str, state: str, db: Session) -> HTMLResponse:
    try:
        pending_state = validate_google_state(state)
        token_response = exchange_google_code(code, pending_state=pending_state)
        delete_google_pending_state(state)
    except Exception as exc:
        logger.exception("Google Drive OAuth callback failed")
        try:
            delete_google_pending_state(state)
        except Exception:
            pass
        raise
    pause_dirty_tracking()
    extra = token_payload_to_extra(token_response)
    ensure_vault_metadata(provider="google_drive", provider_email=extra.get("email"))

    existing = _google_drive_log(db)
    if existing:
        merged = dict(existing.extra_data or {})
        merged.update({k: v for k, v in extra.items() if v})
        existing.extra_data = merged
        existing.created_at = datetime.utcnow()
    else:
        db.add(
            SyncLog(
                source="vault_google",
                sync_type="google_drive",
                status="connected",
                extra_data=extra,
            )
        )
    db.commit()
    resume_dirty_tracking()
    return HTMLResponse(
        """
        <html>
          <body style="font-family: sans-serif; padding: 24px;">
            <p>Google Drive connected. You can close this window.</p>
            <script>
              try { window.opener && window.opener.postMessage({ type: 'vault-google-connected' }, '*'); } catch (e) {}
              window.close();
            </script>
          </body>
        </html>
        """
    )


def _perform_google_drive_backup(db: Session, *, job_id: str | None = None):
    def update(stage: str, progress: int, message: str):
        if job_id:
            _set_backup_job(
                job_id,
                status="running",
                stage=stage,
                progress=progress,
                message=message,
                updated_at=datetime.utcnow().isoformat(),
            )

    log, access_token, updated_extra = _google_access(db)
    update("preparing", 10, "Preparing vault bundle")
    archive_path, manifest = build_backup_bundle()
    vault_id = manifest["vault_id"]
    backup_name = manifest["archive_name"]
    manifest_name = manifest_filename(vault_id)
    folders = _vault_folder_hierarchy(access_token, vault_id)
    backup_folder = ensure_child_folder(access_token, parent_id=folders["backups"]["id"], name=manifest["backup_id"])
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode()
    update("uploading", 55, "Uploading vault archive")
    backup_file = upload_file_resumable(
        access_token,
        name=backup_name,
        file_path=archive_path,
        mime_type="application/octet-stream",
        parent_id=backup_folder["id"],
    )
    backup_manifest_file = upload_multipart_file(
        access_token,
        name=BACKUP_MANIFEST_NAME,
        content_bytes=manifest_bytes,
        mime_type="application/json",
        parent_id=backup_folder["id"],
    )
    update("finalizing", 85, "Finalizing backup manifest")
    metadata = load_vault_metadata()
    latest_manifest_bytes = json.dumps(
        {
            "vault_id": vault_id,
            "latest_backup_id": manifest["backup_id"],
            "latest_backup_created_at": manifest["created_at"],
            "latest_backup_archive_name": manifest["archive_name"],
            "latest_backup_archive_sha256": manifest["archive_sha256"],
            "latest_backup_archive_file_id": backup_file.get("id"),
            "latest_backup_archive_size": backup_file.get("size"),
            "latest_backup_folder_id": backup_folder["id"],
            "primary": bool(metadata.get("primary")),
        },
        indent=2,
        sort_keys=True,
    ).encode()
    existing_manifest = next(iter(list_drive_files(access_token, name=manifest_name, parent_id=folders["vault"]["id"])), None)
    latest_manifest_file = upload_multipart_file(
        access_token,
        name=manifest_name,
        content_bytes=latest_manifest_bytes,
        mime_type="application/json",
        parent_id=folders["vault"]["id"],
        file_id=existing_manifest["id"] if existing_manifest else None,
    )

    pause_dirty_tracking()
    metadata = mark_backup_succeeded(
        "google_drive",
        manifest,
        remote_file_id=backup_file.get("id"),
        remote_manifest_file_id=latest_manifest_file.get("id"),
    )
    log.extra_data = {
        **updated_extra,
        "drive_folder_id": folders["root"]["id"],
        "drive_folder_name": folders["root"].get("name"),
        "vault_folder_id": folders["vault"]["id"],
        "last_backup_at": manifest["created_at"],
        "last_backup_id": manifest["backup_id"],
        "last_backup_name": backup_name,
        "last_backup_file_id": backup_file.get("id"),
    }
    db.commit()
    resume_dirty_tracking()
    try:
        archive_path.unlink(missing_ok=True)
    except Exception:
        pass
    result = {
        "status": "success",
        "provider": "google_drive",
        "vault_id": metadata["vault_id"],
        "backup_id": manifest["backup_id"],
        "backup_created_at": manifest["created_at"],
        "backup_name": backup_name,
        "file_id": backup_file.get("id"),
        "manifest_file_id": backup_manifest_file.get("id"),
    }
    update("success", 100, "Backup complete")
    return result


def _run_backup_job(job_id: str):
    db = SessionLocal()
    try:
        result = _perform_google_drive_backup(db, job_id=job_id)
        _set_backup_job(
            job_id,
            status="success",
            stage="success",
            progress=100,
            message="Backup complete",
            result=result,
            finished_at=datetime.utcnow().isoformat(),
            updated_at=datetime.utcnow().isoformat(),
        )
    except Exception as exc:
        _set_backup_job(
            job_id,
            status="error",
            stage="error",
            progress=100,
            message=str(exc),
            error=str(exc),
            finished_at=datetime.utcnow().isoformat(),
            updated_at=datetime.utcnow().isoformat(),
        )
    finally:
        db.close()


@router.post("/vault/google/backup")
def backup_vault_to_google_drive(db: Session = Depends(get_db)):
    return _perform_google_drive_backup(db)


@router.post("/vault/google/backup/start")
def start_google_drive_backup_job(db: Session = Depends(get_db)):
    _google_drive_log(db) or (_ for _ in ()).throw(HTTPException(status_code=400, detail="Connect Google Drive first"))
    active = _active_backup_job()
    if active:
        return active

    job_id = str(uuid.uuid4())
    job = _set_backup_job(
        job_id,
        status="running",
        stage="queued",
        progress=0,
        message="Queued",
        created_at=datetime.utcnow().isoformat(),
        updated_at=datetime.utcnow().isoformat(),
    )
    threading.Thread(target=_run_backup_job, args=(job_id,), name=f"vault-backup-{job_id[:8]}", daemon=True).start()
    return job


@router.get("/vault/google/backup/jobs/{job_id}")
def get_google_drive_backup_job(job_id: str):
    job = _backup_job_snapshot(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Backup job not found")
    return job


@router.get("/vault/google/backups")
def list_google_backups(vault_id: str | None = Query(default=None), db: Session = Depends(get_db)):
    _, access_token, _ = _google_access(db)
    target_vault_id = vault_id or load_vault_metadata().get("vault_id")
    if not target_vault_id:
        raise HTTPException(status_code=400, detail="No local vault selected and no vault_id was provided")
    backups = _list_google_backups(access_token, target_vault_id)
    return {
        "vault_id": target_vault_id,
        "backups": [
            {k: v for k, v in backup.items() if k != "manifest"}
            for backup in backups
        ],
    }


@router.get("/vault/google/discover")
def discover_google_vaults(db: Session = Depends(get_db)):
    _, access_token, _ = _google_access(db)
    return {"vaults": _discover_google_vaults(access_token)}


@router.post("/vault/google/restore/latest")
def restore_latest_google_backup(vault_id: str | None = Query(default=None), db: Session = Depends(get_db)):
    google_log = _google_drive_log(db)
    preserved_google_extra = dict((google_log.extra_data or {})) if google_log else {}
    _, access_token, _ = _google_access_ephemeral(db)
    target_vault_id = vault_id or load_vault_metadata().get("vault_id")
    if not target_vault_id:
        raise HTTPException(status_code=400, detail="No local vault selected and no vault_id was provided")
    job_id = str(uuid.uuid4())
    job = _set_restore_job(
        job_id,
        status="running",
        stage="discovering",
        progress=0,
        message="Finding latest backup...",
        archive_size=0,
        downloaded=0,
        created_at=datetime.utcnow().isoformat(),
        updated_at=datetime.utcnow().isoformat(),
    )
    db.close()
    threading.Thread(
        target=_run_restore_job,
        args=(job_id, access_token, target_vault_id, preserved_google_extra),
        name=f"vault-restore-{job_id[:8]}",
        daemon=True,
    ).start()
    return job


@router.get("/vault/google/restore/jobs/{job_id}")
def get_restore_job(job_id: str):
    job = _restore_job_snapshot(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Restore job not found")
    return job


def _find_latest_backup_fast(access_token: str, target_vault_id: str) -> dict | None:
    folders = _vault_folder_hierarchy(access_token, target_vault_id)
    manifest_name = manifest_filename(target_vault_id)
    pointer_file = next(iter(list_drive_files(access_token, name=manifest_name, parent_id=folders["vault"]["id"])), None)
    if not pointer_file:
        return None
    pointer = json.loads(download_file_bytes(access_token, pointer_file["id"]).decode())
    archive_file_id = pointer.get("latest_backup_archive_file_id")
    if not archive_file_id:
        return None
    backup_folder_id = pointer.get("latest_backup_folder_id")
    manifest = None
    if backup_folder_id:
        manifest_file = next(iter(list_drive_files(access_token, parent_id=backup_folder_id, name=BACKUP_MANIFEST_NAME)), None)
        if manifest_file:
            try:
                manifest = json.loads(download_file_bytes(access_token, manifest_file["id"]).decode())
            except Exception:
                pass
    return {
        "archive_file_id": archive_file_id,
        "archive_size": int(pointer.get("latest_backup_archive_size") or 0),
        "manifest": manifest,
    }


def _run_restore_job(job_id: str, access_token: str, target_vault_id: str, preserved_google_extra: dict):
    try:
        _set_restore_job(job_id, stage="discovering", progress=2, message="Finding latest backup...", updated_at=datetime.utcnow().isoformat())
        backup = _find_latest_backup_fast(access_token, target_vault_id)
        if not backup:
            _set_restore_job(job_id, progress=3, message="Scanning backup history...", updated_at=datetime.utcnow().isoformat())
            backups = _list_google_backups(access_token, target_vault_id)
            if not backups:
                raise HTTPException(status_code=404, detail="No backups found for this vault")
            backup = backups[0]
        if not backup.get("archive_file_id"):
            raise HTTPException(status_code=400, detail="Latest backup archive is missing")
        archive_size = int(backup.get("archive_size") or 0)

        _set_restore_job(job_id, stage="downloading", progress=5, message="Starting download...", archive_size=archive_size, updated_at=datetime.utcnow().isoformat())
        temp_path = Path(tempfile.gettempdir()) / f"vault-restore-{job_id[:8]}.fvault"

        def on_download_progress(downloaded: int, total: int):
            pct = 5 + int((downloaded / total) * 70) if total else 5
            _set_restore_job(
                job_id,
                progress=pct,
                downloaded=downloaded,
                message=f"Downloading... {downloaded // 1024}KB / {total // 1024}KB",
                updated_at=datetime.utcnow().isoformat(),
            )

        download_file_to_path(
            access_token,
            backup["archive_file_id"],
            temp_path,
            expected_size=archive_size,
            on_progress=on_download_progress,
        )

        _set_restore_job(
            job_id,
            stage="restoring",
            progress=75,
            message="Extracting and restoring...",
            updated_at=datetime.utcnow().isoformat(),
        )
        engine.dispose()
        pause_dirty_tracking()
        result = restore_backup_bundle(temp_path, backup["manifest"])
        engine.dispose()
        from src.models.database import init_db
        init_db()
        _restore_google_drive_state(preserved_google_extra)
        _touch_last_restore_at()
        resume_dirty_tracking()
        _set_restore_job(
            job_id,
            status="success",
            stage="success",
            progress=100,
            message="Restore complete",
            result=result,
            finished_at=datetime.utcnow().isoformat(),
            updated_at=datetime.utcnow().isoformat(),
        )
    except Exception as exc:
        logger.exception("Restore job failed")
        _set_restore_job(
            job_id,
            status="error",
            stage="error",
            progress=100,
            message=str(exc),
            error=str(exc),
            finished_at=datetime.utcnow().isoformat(),
            updated_at=datetime.utcnow().isoformat(),
        )
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _touch_last_restore_at() -> None:
    from src.vault.backup import save_vault_metadata
    metadata = ensure_vault_metadata()
    metadata["last_restore_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    metadata.pop("dirty", None)
    save_vault_metadata(metadata)


def mark_vault_dirty() -> None:
    from src.vault.backup import save_vault_metadata
    metadata = load_vault_metadata()
    if not metadata.get("dirty"):
        metadata["dirty"] = True
        save_vault_metadata(metadata)


@router.post("/rates/refresh")
def refresh_exchange_rates(db: Session = Depends(get_db)):
    """Fetch latest exchange rates from external API."""
    from src.services.exchange_rates import refresh_rates
    count = refresh_rates(db, base_currency=settings.app.display_currency)
    return {"status": "success", "rates_updated": count}


@router.get("/rates")
def list_exchange_rates(db: Session = Depends(get_db)):
    """List stored exchange rates."""
    from src.models import ExchangeRate
    rates = db.query(ExchangeRate).order_by(ExchangeRate.date.desc()).limit(200).all()
    return {
        "rates": [
            {
                "id": r.id,
                "date": r.date.isoformat(),
                "from_currency": r.from_currency,
                "to_currency": r.to_currency,
                "rate": float(r.rate),
            }
            for r in rates
        ]
    }


@router.put("/rates")
def upsert_exchange_rate(
    payload: dict,
    db: Session = Depends(get_db),
):
    """Manual rate override (for offline use)."""
    from src.models import ExchangeRate
    from src.models.transaction import generate_uuid
    from datetime import date as date_type

    rate_date = date_type.fromisoformat(payload["date"])
    from_currency = payload["from_currency"].upper()
    to_currency = payload["to_currency"].upper()
    rate_value = float(payload["rate"])

    existing = db.query(ExchangeRate).filter(
        ExchangeRate.date == rate_date,
        ExchangeRate.from_currency == from_currency,
        ExchangeRate.to_currency == to_currency,
    ).first()
    if existing:
        existing.rate = rate_value
    else:
        db.add(ExchangeRate(
            id=generate_uuid(),
            date=rate_date,
            from_currency=from_currency,
            to_currency=to_currency,
            rate=rate_value,
        ))
    db.commit()
    return {"status": "success", "date": rate_date.isoformat(), "from_currency": from_currency, "to_currency": to_currency, "rate": rate_value}


def _restore_google_drive_state(preserved_google_extra: dict) -> None:
    if not preserved_google_extra:
        return
    ensure_vault_metadata(provider="google_drive", provider_email=preserved_google_extra.get("email"))
    db = SessionLocal()
    try:
        existing = _google_drive_log(db)
        if existing:
            merged = dict(existing.extra_data or {})
            merged.update({k: v for k, v in preserved_google_extra.items() if v is not None})
            existing.extra_data = merged
            existing.status = "connected"
            existing.created_at = datetime.utcnow()
        else:
            db.add(
                SyncLog(
                    source="vault_google",
                    sync_type="google_drive",
                    status="connected",
                    extra_data=preserved_google_extra,
                )
            )
        db.commit()
    finally:
        db.close()
