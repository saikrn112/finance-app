"""Locking: what turned a background Plaid pull into Internal Server Errors in the macOS app.

Two independent defects, both found from the bundle's backend log:

* Plaid telemetry opens a second session with a 50 ms busy timeout, so it can give up quickly while
  the caller holds the write lock. That PRAGMA was set on a *pooled* connection and never reset, so
  the next request to draw that connection failed with "database is locked" almost instantly.
* In rollback-journal mode a writer blocks readers. The pull holds its write transaction across
  network calls, so every page load during a sync waited out the busy timeout and then 500'd.
  WAL lets readers proceed; it is enabled only where the database is on a local disk.
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from src.models import PlaidApiUsage
from src.models import database as db_module
from src.models.database import Base, SQLITE_BUSY_TIMEOUT_MS


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """A file-backed engine with the app's own connect hook, and a small pool to make reuse certain."""
    eng = create_engine(f"sqlite:///{tmp_path}/lock.db", pool_size=1, max_overflow=0)
    event.listen(eng, "connect", db_module._set_sqlite_pragmas)
    Base.metadata.create_all(bind=eng)
    monkeypatch.setattr(
        "src.ingestion.plaid_usage.ensure_vault_metadata",
        lambda: {"device_id": "synthetic-device", "device_label": "Test"},
    )
    yield eng
    eng.dispose()


def _busy_timeout(eng) -> int:
    with eng.connect() as conn:
        return conn.exec_driver_sql("PRAGMA busy_timeout").scalar()


class TestTelemetryDoesNotPoisonThePool:
    def test_the_normal_timeout_is_restored_after_recording(self, engine):
        from src.ingestion.plaid_usage import record_plaid_usage

        db = sessionmaker(bind=engine)()
        try:
            record_plaid_usage(db, endpoint="accounts_balance_get")
        finally:
            db.close()

        assert _busy_timeout(engine) == SQLITE_BUSY_TIMEOUT_MS, (
            "a 50 ms connection back in the pool fails the next request on any overlapping write"
        )

    def test_the_timeout_is_restored_after_finishing_too(self, engine):
        from src.ingestion.plaid_usage import finish_plaid_usage, record_plaid_usage

        db = sessionmaker(bind=engine)()
        try:
            usage = record_plaid_usage(db, endpoint="accounts_balance_get")
            finish_plaid_usage(db, usage, success=True)
        finally:
            db.close()

        assert _busy_timeout(engine) == SQLITE_BUSY_TIMEOUT_MS

    def test_the_audit_row_is_actually_committed(self, engine):
        """Binding to an explicit connection risks a session that joins, and never commits, the
        transaction the PRAGMA opened. The row must survive the caller rolling back."""
        from src.ingestion.plaid_usage import record_plaid_usage

        db = sessionmaker(bind=engine)()
        try:
            record_plaid_usage(db, endpoint="accounts_balance_get")
            db.rollback()
        finally:
            db.close()

        check = sessionmaker(bind=engine)()
        try:
            assert check.query(PlaidApiUsage).count() == 1
        finally:
            check.close()


class TestJournalMode:
    def _mode_after_hook(self, tmp_path, monkeypatch, use_wal: bool) -> str:
        monkeypatch.setattr(db_module, "USE_WAL", use_wal)
        raw = sqlite3.connect(tmp_path / "mode.db")
        try:
            db_module._set_sqlite_pragmas(raw, None)
            return raw.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            raw.close()

    def test_the_macos_bundle_runs_in_wal(self, tmp_path, monkeypatch):
        assert self._mode_after_hook(tmp_path, monkeypatch, True) == "wal"

    def test_everything_else_stays_in_rollback_journal_mode(self, tmp_path, monkeypatch):
        """The container's database is on a bind mount, where WAL produces disk I/O errors."""
        assert self._mode_after_hook(tmp_path, monkeypatch, False) == "delete"

    def test_wal_is_keyed_on_the_bundle_marker(self, monkeypatch):

        monkeypatch.delenv("FINANCE_APP_LOCAL_TOKEN", raising=False)
        assert bool(__import__("os").environ.get("FINANCE_APP_LOCAL_TOKEN")) is False
        assert db_module.USE_WAL is False, "tests and the container must not run in WAL"

    def test_in_wal_a_reader_is_not_blocked_by_an_open_write(self, tmp_path):
        """The behaviour the macOS app needed: a page loads while a sync holds the write lock."""
        path = tmp_path / "wal.db"
        writer = sqlite3.connect(path, timeout=0.05)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE t (x)")
        writer.execute("INSERT INTO t VALUES (1)")
        writer.commit()

        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO t VALUES (2)")
        reader = sqlite3.connect(path, timeout=0.05)
        try:
            assert reader.execute("SELECT count(*) FROM t").fetchone()[0] == 1
        finally:
            reader.close()
            writer.rollback()
            writer.close()


class TestTelemetryCannotBreakTheSync:
    """Found as "Sync incomplete" on the macOS app: Marcus failed at its final commit.

    Another writer held the lock, so the usage row's fallback flush on the *caller's* session timed
    out. That left the session "rolled back due to a previous exception", and the bank pull's own
    commit then raised PendingRollbackError. Telemetry must never be able to do that.
    """

    def test_the_callers_commit_survives_a_failed_usage_write(self, engine, monkeypatch):
        from src.ingestion.plaid_usage import record_plaid_usage
        from src.models.transaction import Transaction

        # Short timeouts so the contention resolves in milliseconds rather than 15 s.
        monkeypatch.setattr(db_module, "SQLITE_BUSY_TIMEOUT_MS", 100)
        engine.dispose()
        path = engine.url.database

        holder = sqlite3.connect(path, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")  # the other writer

        db = sessionmaker(bind=engine)()
        try:
            assert record_plaid_usage(db, endpoint="transactions_sync") is None, (
                "the usage row cannot be written while another connection holds the lock"
            )
            holder.execute("ROLLBACK")  # the other writer finishes
            holder.close()

            txn = Transaction(source="Example Card", source_id="after-contention", origin="plaid",
                              date=__import__("datetime").date(2026, 10, 8), merchant_raw="X",
                              merchant_clean="X", category="Dining")
            txn.amount = -5
            db.add(txn)
            db.commit()  # PendingRollbackError here is the bug
        finally:
            db.close()

        check = sessionmaker(bind=engine)()
        try:
            assert check.query(Transaction).filter_by(source_id="after-contention").count() == 1
        finally:
            check.close()


class TestDeviceSyncWaitsItsTurn:
    def test_a_device_sync_round_is_deferred_while_a_plaid_pull_holds_the_job_lock(self, monkeypatch):
        from src.services import auto_tasks, job_lock

        ran = []
        monkeypatch.setattr("src.sync.runner.sync_once", lambda db: ran.append(1) or {"ran": True})

        with job_lock.try_acquire("plaid sync") as acquired:
            assert acquired
            auto_tasks._run_device_sync(None, trigger="test")

        assert ran == [], "a merge must not write alongside a Plaid pull"

        auto_tasks._run_device_sync(None, trigger="test")
        assert ran == [1], "and it runs once the lock is free"
