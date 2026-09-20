"""The backup lease, and the two publish defects it depends on.

The lease answers "has *anybody* backed up recently", not "have I" -- the same question the Plaid
economy asks, for the same reason: every device holds the whole database, so a snapshot taken on one
protects them all, and N devices would otherwise produce N near-identical snapshots a day and shrink
the retention window to a fraction of the days it claims to cover.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.sync import engine
from src.sync.engine import (
    BACKUP_LAST_AT_KEY,
    last_backup_anywhere,
    note_peer_backup_report,
    record_backup,
    run_sync,
)
from tests.sync_fakes import FakeTransport


def _wire(when: datetime) -> str:
    from src.sync import coding

    return coding.datetime_to_wire(when)


class TestLease:
    def test_no_backup_anywhere_reads_as_unknown(self, db_session):
        assert last_backup_anywhere(db_session) is None

    def test_this_devices_own_backup_counts(self, db_session):
        when = datetime(2026, 9, 17, 5, 0, 0)
        record_backup(db_session, when)
        assert last_backup_anywhere(db_session) == when

    def test_a_peers_backup_counts_so_this_device_can_skip_its_own(self, db_session):
        when = datetime(2026, 9, 17, 5, 0, 0)
        note_peer_backup_report(db_session, "macos-1", {"backup": {"last_backup_at": _wire(when)}})
        db_session.commit()
        assert last_backup_anywhere(db_session) == when

    def test_the_newest_across_devices_wins(self, db_session):
        older = datetime(2026, 9, 10, 0, 0, 0)
        newer = datetime(2026, 9, 17, 0, 0, 0)
        record_backup(db_session, older)
        note_peer_backup_report(db_session, "macos-1", {"backup": {"last_backup_at": _wire(newer)}})
        db_session.commit()
        assert last_backup_anywhere(db_session) == newer

    def test_a_peer_report_never_moves_backwards(self, db_session):
        newer = datetime(2026, 9, 17, 0, 0, 0)
        older = datetime(2026, 9, 1, 0, 0, 0)
        note_peer_backup_report(db_session, "macos-1", {"backup": {"last_backup_at": _wire(newer)}})
        note_peer_backup_report(db_session, "macos-1", {"backup": {"last_backup_at": _wire(older)}})
        db_session.commit()
        assert last_backup_anywhere(db_session) == newer

    def test_a_payload_without_a_backup_section_is_ignored(self, db_session):
        note_peer_backup_report(db_session, "macos-1", {})
        db_session.commit()
        assert last_backup_anywhere(db_session) is None

    def test_the_report_carries_a_timestamp_and_nothing_else(self, db_session):
        record_backup(db_session, datetime(2026, 9, 17))
        assert set(engine._backup_report(db_session)) == {"last_backup_at"}


class TestPayloadCarriesTheReport:
    def test_publishing_includes_the_backup_report_so_peers_can_read_it(self, db_session):
        record_backup(db_session, datetime(2026, 9, 17, 5, 0, 0))
        transport = FakeTransport()

        run_sync(db_session, transport, device_id="container-1", force_publish=True)

        import json

        payload = json.loads(transport.files["container-1"])
        assert payload["backup"]["last_backup_at"], "peers cannot skip without this"

    def test_a_peers_report_is_absorbed_on_merge(self, db_session):
        when = datetime(2026, 9, 17, 5, 0, 0)
        transport = FakeTransport()
        transport.files["macos-1"] = __import__("json").dumps(
            {
                "format_version": 1,
                "device_id": "macos-1",
                "records": {},
                "tombstones": [],
                "backup": {"last_backup_at": _wire(when)},
            }
        )

        run_sync(db_session, transport, device_id="container-1")

        assert last_backup_anywhere(db_session) == when


class TestPublishSkipVerifiesThePayloadExists:
    """A local fingerprint records what we last *decided* to publish, not what is on Drive."""

    def test_an_unchanged_device_still_skips_when_its_payload_is_there(self, db_session):
        transport = FakeTransport()
        run_sync(db_session, transport, device_id="container-1")
        first = transport.put_count

        second = run_sync(db_session, transport, device_id="container-1")

        assert second.skipped_publish is True
        assert transport.put_count == first, "unchanged state must not re-upload full history"

    def test_a_missing_payload_is_republished_even_though_nothing_changed(self, db_session):
        transport = FakeTransport()
        run_sync(db_session, transport, device_id="container-1")
        transport.delete("container-1")

        result = run_sync(db_session, transport, device_id="container-1")

        assert result.published is True, (
            "observed live: skipped_publish with no payload on Drive, so sync did nothing at all"
        )
        assert "container-1" in transport.files

    def test_a_transport_that_cannot_answer_is_assumed_to_still_hold_it(self, db_session):
        """Failing closed here would re-upload 2.5 MB on every poll because a listing hiccupped."""
        transport = FakeTransport()
        run_sync(db_session, transport, device_id="container-1")
        before = transport.put_count

        def explode(device_id):
            raise RuntimeError("drive listing failed")

        transport.has_payload = explode
        result = run_sync(db_session, transport, device_id="container-1")

        assert result.skipped_publish is True
        assert transport.put_count == before


class TestForcePublish:
    def test_force_publish_reaches_the_engine_through_sync_once(self, db_session, monkeypatch):
        """Restore relies on this: the restored state must win on peers deliberately."""
        from src.sync import runner

        transport = FakeTransport()
        monkeypatch.setattr(
            runner, "choose_transport",
            lambda db: runner.TransportChoice(runner.MODE_DRIVE, transport, "fake"),
        )

        runner.sync_once(db_session)
        before = transport.put_count
        runner.sync_once(db_session, force_publish=True)

        assert transport.put_count == before + 1, "force_publish did not reach run_sync"
