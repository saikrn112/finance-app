"""One long-running database job at a time.

SQLite allows a single writer. Sync, vault backup, and vault restore all run on their own
background threads with their own sessions, and each holds write transactions across slow
network I/O (Plaid calls, Google Drive uploads). Any overlap between them — or between one
of them and an ordinary request — exceeds the busy timeout and surfaces as
"(sqlite3.OperationalError) database is locked".

Guarding each job individually is not enough: the collision is *between* jobs, so they must
share one lock.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_holder: str | None = None
_held_since: float | None = None


def current_holder() -> str | None:
    """Name of the job currently holding the lock, if any."""
    return _holder


def held_for_seconds() -> float | None:
    """How long the current holder has held the lock. A job stuck on a dead connection held it for
    22 hours once, and "busy" was all anyone could see; this makes "stuck" distinguishable."""
    return None if _held_since is None else time.monotonic() - _held_since


@contextmanager
def try_acquire(job_name: str):
    """Yield True if this job may run, False if another long job already holds the lock.

    Non-blocking on purpose: queueing a second sync behind a slow one is rarely what the
    user wants, and blocking a request thread on a multi-minute backup is worse.
    """
    global _holder
    acquired = _lock.acquire(blocking=False)
    if not acquired:
        logger.info("%s skipped: %s already running", job_name, _holder)
        yield False
        return
    global _held_since
    _holder = job_name
    _held_since = time.monotonic()
    try:
        yield True
    finally:
        _holder = None
        _held_since = None
        _lock.release()
