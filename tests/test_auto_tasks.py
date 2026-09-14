"""Scheduling tests for the background sync/backup worker.

The defect these exist for: the old loop slept 24h and only then backed up, so the countdown
restarted on every launch and a frequently-restarted desktop app never backed up at all. The
regression to protect is therefore not "a backup happens eventually" but "a backup happens on a
process that has only just started".
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.models import AppMetadata
from src.services import auto_tasks


@pytest.fixture
def db(db_session, monkeypatch):
    """A session on the temporary database, with the worker's own SessionLocal pointed at it.

    `_tick` opens its own session rather than being handed one -- it runs on a background thread in
    production -- so the factory has to be redirected too, or the tick would read the real database.
    """
    monkeypatch.setattr(auto_tasks, "SessionLocal", lambda: db_session)
    return db_session


def _iso(when: datetime) -> str:
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class TestTimestampParsing:
    @pytest.mark.parametrize(
        "value",
        [
            "2026-09-12T17:55:15Z",       # what the vault writes
            "2026-09-12T17:55:15+00:00",  # what fromisoformat writes
            "2026-09-12T17:55:15",        # naive, from older rows
        ],
    )
    def test_accepted_formats_all_parse_to_the_same_instant(self, value):
        parsed = auto_tasks._parse_timestamp(value)
        assert parsed == datetime(2026, 9, 12, 17, 55, 15, tzinfo=timezone.utc)

    @pytest.mark.parametrize("value", [None, "", "   ", "not a date", 12345, {}])
    def test_unusable_values_read_as_absent(self, value):
        assert auto_tasks._parse_timestamp(value) is None

    def test_a_naive_timestamp_is_not_treated_as_local_time(self):
        """A naive value read as local time would shift the due calculation by the UTC offset."""
        parsed = auto_tasks._parse_timestamp("2026-09-12T17:55:15")
        assert parsed.tzinfo == timezone.utc
        assert parsed.hour == 17


class TestDueCalculation:
    def test_never_run_is_due(self):
        assert auto_tasks._is_due(None, now=_now()) is True

    def test_just_run_is_not_due(self):
        now = _now()
        assert auto_tasks._is_due(now - timedelta(minutes=5), now=now) is False

    def test_older_than_the_interval_is_due(self):
        now = _now()
        assert auto_tasks._is_due(now - auto_tasks.AUTO_SYNC_INTERVAL, now=now) is True

    def test_just_under_the_interval_is_not_due(self):
        now = _now()
        assert auto_tasks._is_due(
            now - auto_tasks.AUTO_SYNC_INTERVAL + timedelta(minutes=1), now=now
        ) is False

    def test_a_future_timestamp_is_due_rather_than_waiting_it_out(self):
        """A clock change or a database restored from a machine running ahead would otherwise
        suppress backups for an unbounded time."""
        now = _now()
        assert auto_tasks._is_due(now + timedelta(days=400), now=now) is True

    def test_unparseable_stored_value_errs_towards_backing_up(self, db):
        db.add(AppMetadata(key=auto_tasks.LAST_BACKUP_ATTEMPT_KEY, value="garbage"))
        db.commit()
        assert auto_tasks._backup_due(db, now=_now()) is True


class TestPersistence:
    def test_a_written_timestamp_survives_a_new_session(self, temp_db):
        """The point of the change: the schedule outlives the process that wrote it."""
        session_factory, _ = temp_db
        when = _now() - timedelta(hours=1)

        writer = session_factory()
        auto_tasks._write_timestamp(writer, auto_tasks.LAST_SYNC_KEY, when)
        writer.close()

        reader = session_factory()
        try:
            assert auto_tasks._read_timestamp(reader, auto_tasks.LAST_SYNC_KEY) == when.replace(
                microsecond=0
            )
        finally:
            reader.close()

    def test_writing_twice_updates_rather_than_duplicating(self, db):
        auto_tasks._write_timestamp(db, auto_tasks.LAST_SYNC_KEY, _now() - timedelta(days=2))
        auto_tasks._write_timestamp(db, auto_tasks.LAST_SYNC_KEY, _now())
        rows = db.query(AppMetadata).filter(AppMetadata.key == auto_tasks.LAST_SYNC_KEY).all()
        assert len(rows) == 1


class TestBackupScheduling:
    """`_backup_due` combines a success time from the vault with an attempt time from the DB."""

    def test_a_fresh_install_is_due(self, db, monkeypatch):
        _vault(monkeypatch, {})
        assert auto_tasks._backup_due(db, now=_now()) is True

    def test_a_recent_vault_backup_defers_it(self, db, monkeypatch):
        now = _now()
        _vault(monkeypatch, {"last_backup_at": _iso(now - timedelta(hours=2))})
        assert auto_tasks._backup_due(db, now=now) is False

    def test_an_overdue_vault_backup_is_due_on_a_process_that_just_started(self, db, monkeypatch):
        """THE regression test.

        This is the exact state of the real app: the last backup was two days ago, taken by a
        different app, and this process has just started. The old design could not reach a backup
        here -- it would wait 24h of uptime first, and the app is restarted long before that.
        """
        now = _now()
        _vault(monkeypatch, {"last_backup_at": _iso(now - timedelta(days=2))})
        assert auto_tasks._backup_due(db, now=now) is True

    def test_a_recent_failed_attempt_defers_the_retry(self, db, monkeypatch):
        """Otherwise a disconnected vault is retried every poll, forever."""
        now = _now()
        _vault(monkeypatch, {"last_backup_at": _iso(now - timedelta(days=30))})
        auto_tasks._write_timestamp(db, auto_tasks.LAST_BACKUP_ATTEMPT_KEY, now - timedelta(minutes=5))
        assert auto_tasks._backup_due(db, now=now) is False

    def test_an_old_failed_attempt_does_not_defer_it_forever(self, db, monkeypatch):
        now = _now()
        _vault(monkeypatch, {"last_backup_at": _iso(now - timedelta(days=30))})
        auto_tasks._write_timestamp(db, auto_tasks.LAST_BACKUP_ATTEMPT_KEY, now - timedelta(days=2))
        assert auto_tasks._backup_due(db, now=now) is True

    def test_the_attempt_is_recorded_before_the_backup_runs(self, db, monkeypatch):
        """A hang or crash inside the backup must still move the attempt clock, or the next poll
        starts it again immediately."""
        recorded: list[datetime | None] = []

        def exploding_backup(*, db):
            recorded.append(auto_tasks._read_timestamp(db, auto_tasks.LAST_BACKUP_ATTEMPT_KEY))
            raise RuntimeError("network died mid-upload")

        monkeypatch.setattr(
            "src.api.routes.settings.backup_vault_to_google_drive", exploding_backup
        )
        auto_tasks._run_vault_backup(db, trigger="test")

        assert recorded and recorded[0] is not None, "the attempt was not recorded before the call"
        assert auto_tasks._backup_due(db, now=_now()) is False


class TestTick:
    def test_startup_syncs_even_when_a_sync_is_not_due(self, db, monkeypatch):
        """Opening the app should show current data; that behaviour predates this change."""
        calls = _stub_tasks(monkeypatch)
        auto_tasks._write_timestamp(db, auto_tasks.LAST_SYNC_KEY, _now() - timedelta(minutes=1))
        _vault(monkeypatch, {"last_backup_at": _iso(_now())})

        auto_tasks._tick(trigger="startup", force_sync=True)
        assert calls["sync"] == 1
        assert calls["backup"] == 0

    def test_an_interval_tick_with_nothing_due_does_nothing(self, db, monkeypatch):
        calls = _stub_tasks(monkeypatch)
        auto_tasks._write_timestamp(db, auto_tasks.LAST_SYNC_KEY, _now() - timedelta(minutes=1))
        _vault(monkeypatch, {"last_backup_at": _iso(_now())})

        auto_tasks._tick(trigger="interval", force_sync=False)
        assert calls == {"sync": 0, "backup": 0}

    def test_a_failing_sync_does_not_prevent_the_backup(self, db, monkeypatch):
        """These are the two halves of 'the data is safe', and sync talks to a third party."""
        calls = _stub_tasks(monkeypatch, sync_raises=RuntimeError("plaid is down"))
        _vault(monkeypatch, {"last_backup_at": _iso(_now() - timedelta(days=2))})

        auto_tasks._tick(trigger="startup", force_sync=True)
        assert calls["backup"] == 1

    def test_startup_takes_an_overdue_backup(self, db, monkeypatch):
        calls = _stub_tasks(monkeypatch)
        _vault(monkeypatch, {"last_backup_at": _iso(_now() - timedelta(days=2))})

        auto_tasks._tick(trigger="startup", force_sync=True)
        assert calls["backup"] == 1


# --- helpers ---------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _vault(monkeypatch, metadata: dict) -> None:
    monkeypatch.setattr("src.vault.backup.load_vault_metadata", lambda: metadata)


def _stub_tasks(monkeypatch, *, sync_raises: Exception | None = None) -> dict:
    """Replace both real tasks with counters, so scheduling is tested without Plaid or Drive."""
    calls = {"sync": 0, "backup": 0}

    def fake_sync(db, *, trigger):
        calls["sync"] += 1
        if sync_raises:
            raise sync_raises

    def fake_backup(db, *, trigger):
        calls["backup"] += 1

    monkeypatch.setattr(auto_tasks, "_run_plaid_sync", fake_sync)
    monkeypatch.setattr(auto_tasks, "_run_vault_backup", fake_backup)
    return calls
