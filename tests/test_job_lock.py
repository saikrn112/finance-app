"""The single-writer invariant for long-running database jobs.

SQLite has one writer. Sync, vault backup, and vault restore each run on their own thread
with their own session and hold write transactions across slow network I/O, so any overlap
produces "(sqlite3.OperationalError) database is locked". These tests pin the guarantee that
they cannot overlap.
"""
import threading

from src.services import job_lock


def test_second_job_is_refused_while_first_holds_lock():
    with job_lock.try_acquire("plaid sync") as first:
        assert first is True
        with job_lock.try_acquire("vault backup") as second:
            assert second is False, "a backup must not run while a sync holds the lock"


def test_lock_is_released_after_the_job_finishes():
    with job_lock.try_acquire("plaid sync") as acquired:
        assert acquired is True
    with job_lock.try_acquire("vault backup") as acquired:
        assert acquired is True


def test_lock_is_released_when_the_job_raises():
    try:
        with job_lock.try_acquire("plaid sync") as acquired:
            assert acquired is True
            raise RuntimeError("sync blew up")
    except RuntimeError:
        pass
    with job_lock.try_acquire("vault backup") as acquired:
        assert acquired is True, "a crashed job must not wedge the lock forever"


def test_holder_is_reported_so_callers_can_explain_the_refusal():
    with job_lock.try_acquire("plaid sync"):
        assert job_lock.current_holder() == "plaid sync"
    assert job_lock.current_holder() is None


def test_lock_excludes_across_threads():
    """The real collision is cross-thread: auto-sync vs the backup thread."""
    held = threading.Event()
    release = threading.Event()
    other_result = {}

    def hold_it():
        with job_lock.try_acquire("plaid sync") as acquired:
            other_result["first"] = acquired
            held.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold_it)
    t.start()
    assert held.wait(timeout=5)
    with job_lock.try_acquire("vault backup") as acquired:
        assert acquired is False
    release.set()
    t.join(timeout=5)
    assert other_result["first"] is True
