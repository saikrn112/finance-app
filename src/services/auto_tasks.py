from __future__ import annotations

import logging
import os
import threading
from datetime import timedelta

from fastapi import HTTPException

from src.config import settings
from src.models import SessionLocal

logger = logging.getLogger(__name__)

AUTO_SYNC_INTERVAL = timedelta(hours=24)
_worker_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None


def _run_sync_and_optional_backup(*, trigger: str, run_backup: bool) -> None:
    from src.api.routes.settings import backup_vault_to_google_drive
    from src.api.routes.sync import sync_plaid

    db = SessionLocal()
    try:
        sync_result = sync_plaid(db=db)
        logger.info("auto-task plaid sync completed", extra={"trigger": trigger, "result": sync_result})
        if not run_backup:
            return
        try:
            backup_result = backup_vault_to_google_drive(db=db)
            logger.info("auto-task vault backup completed", extra={"trigger": trigger, "result": backup_result})
        except HTTPException as exc:
            if exc.status_code == 400:
                logger.info("auto-task vault backup skipped", extra={"trigger": trigger, "detail": exc.detail})
            else:
                logger.exception("auto-task vault backup failed", extra={"trigger": trigger, "detail": exc.detail})
        except Exception:
            logger.exception("auto-task vault backup failed", extra={"trigger": trigger})
    except Exception:
        logger.exception("auto-task plaid sync failed", extra={"trigger": trigger})
    finally:
        db.close()


def _worker_loop(stop_event: threading.Event) -> None:
    try:
        _run_sync_and_optional_backup(trigger="startup", run_backup=False)
    except Exception:
        logger.exception("auto-task startup sync crashed")
    while not stop_event.wait(AUTO_SYNC_INTERVAL.total_seconds()):
        try:
            _run_sync_and_optional_backup(trigger="interval", run_backup=True)
        except Exception:
            logger.exception("auto-task interval run crashed")


def start_auto_tasks() -> None:
    global _worker_thread, _stop_event
    if settings.is_demo:
        return
    if os.environ.get("FINANCE_APP_DISABLE_AUTO_TASKS") == "1":
        # The worker syncs immediately on startup and holds the shared job lock while it
        # does. Under test that races whatever the test itself is asking the API to do, so
        # a manual sync intermittently comes back "already_running". Tests opt out.
        logger.info("auto-tasks disabled by FINANCE_APP_DISABLE_AUTO_TASKS")
        return
    if _worker_thread and _worker_thread.is_alive():
        return
    stop_event = threading.Event()
    worker = threading.Thread(target=_worker_loop, args=(stop_event,), name="finance-auto-sync", daemon=True)
    worker.start()
    _stop_event = stop_event
    _worker_thread = worker


def stop_auto_tasks() -> None:
    global _worker_thread, _stop_event
    if _stop_event:
        _stop_event.set()
    _stop_event = None
    _worker_thread = None
