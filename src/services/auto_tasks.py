from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from src.config import settings
from src.models import AppMetadata, SessionLocal

logger = logging.getLogger(__name__)

AUTO_SYNC_INTERVAL = timedelta(hours=24)

# How often the worker wakes up to *consider* work, as distinct from how often work is due.
#
# These used to be the same value, and that was the defect: the loop slept AUTO_SYNC_INTERVAL and
# only backed up afterwards, so the countdown restarted from zero on every launch. A desktop app
# that is quit and reopened during the day therefore never reached its first backup at all --
# confirmed on the real app, which had never logged one, while the long-running container app had.
# Once the desktop app became the only copy of the data, "never backs up" stopped being a latency
# problem.
#
# So the schedule is decided by comparing persisted timestamps and the poll is short. A wake-up
# with nothing due costs one primary-key lookup.
POLL_INTERVAL = timedelta(minutes=15)

# In app_metadata rather than in memory, which is the whole point.
LAST_SYNC_KEY = "auto_task_last_sync_at"
# Attempts, not successes: a backup that fails every time -- no vault connected, no network --
# must not be retried on every poll. Successes come from the vault metadata instead, because that
# is where the backup itself already records them, and it travels with the vault.
LAST_BACKUP_ATTEMPT_KEY = "auto_task_last_backup_attempt_at"

_worker_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(when: datetime) -> str:
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    """Parse a stored timestamp, tolerating what is already on disk.

    The vault writes `...Z`, and rows written by older code may be naive. Anything unparseable is
    reported as "no timestamp", which makes the task due -- erring towards taking a backup rather
    than towards skipping one.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _read_timestamp(db, key: str) -> datetime | None:
    row = db.get(AppMetadata, key)
    return _parse_timestamp(row.value) if row else None


def _write_timestamp(db, key: str, when: datetime) -> None:
    row = db.get(AppMetadata, key)
    if row:
        row.value = _format_timestamp(when)
    else:
        db.add(AppMetadata(key=key, value=_format_timestamp(when)))
    db.commit()


def _is_due(last: datetime | None, *, now: datetime) -> bool:
    if last is None:
        return True
    # A timestamp in the future means a clock change, or a database restored from a machine whose
    # clock ran ahead. Treat it as due rather than waiting the difference out, which could be
    # arbitrarily long.
    if last > now:
        return True
    return now - last >= AUTO_SYNC_INTERVAL


def _sync_due(db, *, now: datetime) -> bool:
    return _is_due(_read_timestamp(db, LAST_SYNC_KEY), now=now)


def _backup_due(db, *, now: datetime) -> bool:
    from src.vault.backup import load_vault_metadata

    last_success = _parse_timestamp(load_vault_metadata().get("last_backup_at"))
    last_attempt = _read_timestamp(db, LAST_BACKUP_ATTEMPT_KEY)
    candidates = [t for t in (last_success, last_attempt) if t is not None]
    return _is_due(max(candidates) if candidates else None, now=now)


def _run_plaid_sync(db, *, trigger: str, force: bool = False) -> None:
    """Pull from Plaid, unless another device already did it recently.

    Plaid bills per call, and a transaction fetched on the Mac is the same transaction on the phone,
    so the question is "has *anybody* pulled recently", not "have I". That is what keeps a second or
    third device from multiplying the bill.

    Skipping is safe in both directions: if the peer's report turns out to be stale the next poll
    pulls anyway, and if two devices pull simultaneously the `(source, source_id)` unique index makes
    that wasteful rather than wrong.
    """
    from src.api.routes.sync import sync_plaid
    from src.sync.engine import plaid_pull_is_needed, record_plaid_pull

    if not force:
        needed, reason = plaid_pull_is_needed(db, interval=AUTO_SYNC_INTERVAL, now=_utc_now().replace(tzinfo=None))
        if not needed:
            logger.info("auto-task plaid sync skipped", extra={"trigger": trigger, "reason": reason})
            # The local clock still moves, or this device would re-evaluate on every single poll.
            _write_timestamp(db, LAST_SYNC_KEY, _utc_now())
            return

    result = sync_plaid(db=db)
    logger.info("auto-task plaid sync completed", extra={"trigger": trigger, "result": result})
    _write_timestamp(db, LAST_SYNC_KEY, _utc_now())
    # Tell peers, so they can skip their own pull.
    record_plaid_pull(db)


def _run_vault_backup(db, *, trigger: str) -> None:
    from src.api.routes.settings import backup_vault_to_google_drive

    # Recorded before the attempt, so a crash or a hang inside the backup cannot turn into a tight
    # retry loop on the next poll.
    _write_timestamp(db, LAST_BACKUP_ATTEMPT_KEY, _utc_now())
    try:
        result = backup_vault_to_google_drive(db=db)
        logger.info("auto-task vault backup completed", extra={"trigger": trigger, "result": result})
    except HTTPException as exc:
        if exc.status_code == 400:
            # No vault connected yet. Expected, not a failure.
            logger.info("auto-task vault backup skipped", extra={"trigger": trigger, "detail": exc.detail})
        else:
            logger.exception("auto-task vault backup failed", extra={"trigger": trigger, "detail": exc.detail})
    except Exception:
        logger.exception("auto-task vault backup failed", extra={"trigger": trigger})


def _tick(*, trigger: str, force_sync: bool) -> None:
    """One scheduling decision.

    The two tasks are guarded separately: a failing sync must not stop the backup. They are the
    two halves of "the data is safe", and the sync half talks to a third party that returns
    errors of its own.
    """
    now = _utc_now()
    db = SessionLocal()
    try:
        try:
            if force_sync or _sync_due(db, now=now):
                # `force` must be forwarded, not just used to decide whether to call. Without it the
                # startup tick still hit the peer lease -- and since that lease counts this device's
                # own last pull, reopening the app within 24h pulled nothing at all, which is the
                # opposite of what opening the app is for.
                _run_plaid_sync(db, trigger=trigger, force=force_sync)
        except Exception:
            logger.exception("auto-task plaid sync failed", extra={"trigger": trigger})

        try:
            if _backup_due(db, now=now):
                _run_vault_backup(db, trigger=trigger)
        except Exception:
            logger.exception("auto-task vault backup crashed", extra={"trigger": trigger})
    finally:
        db.close()


def _worker_loop(stop_event: threading.Event) -> None:
    # Opening the app still syncs, as it always has: data that is current on screen is the visible
    # half of this worker. The backup is decided on its own timestamp, so an overdue one runs now
    # rather than waiting for an interval this process may never see the end of.
    try:
        _tick(trigger="startup", force_sync=True)
    except Exception:
        logger.exception("auto-task startup tick crashed")
    while not stop_event.wait(POLL_INTERVAL.total_seconds()):
        try:
            _tick(trigger="interval", force_sync=False)
        except Exception:
            logger.exception("auto-task interval tick crashed")


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
