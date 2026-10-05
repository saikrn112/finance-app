"""A sync round touches only what changed.

Payloads are full state, so before this every round re-downloaded each peer's ~6 MB file and merged
it against every row, only to report "nothing new". At a five-minute poll that is ~1.7 GB a day per
device for no information. Now an unchanged peer is recognised from the Drive listing alone.

Skipping is only safe if it can never skip something not yet absorbed. Most of these tests are about
those cases: consent arriving later, a schema upgrade, a restore, and a peer's real edit.
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base
from src.models.transaction import Transaction
from src.sync import engine as sync_engine
from src.sync.engine import forget_merged_versions, grant_merge_consent, run_sync
from tests.sync_fakes import FakeTransport

PEER = "macos-1"
LOCAL = "container-1"


def _session(tmp_path, name):
    engine = create_engine(f"sqlite:///{tmp_path}/{name}.db")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _add(db, source_id, category="Dining"):
    txn = Transaction(
        source="Example Card", source_id=source_id, origin="plaid", date=date(2026, 9, 10),
        merchant_raw=source_id.upper(), merchant_clean=source_id, category=category,
    )
    txn.amount = -10
    db.add(txn)
    db.commit()
    return txn


@pytest.fixture
def pair(tmp_path):
    local, peer = _session(tmp_path, "local"), _session(tmp_path, "peer")
    grant_merge_consent(local)
    grant_merge_consent(peer)
    yield local, peer, FakeTransport()
    local.close()
    peer.close()


def _txns(db):
    return {t.source_id: t.category for t in db.query(Transaction).all()}


class TestQuietRoundsAreCheap:
    def test_an_unchanged_peer_is_downloaded_once(self, pair):
        local, peer, transport = pair
        _add(peer, "coffee")
        run_sync(peer, transport, device_id=PEER)

        for _ in range(5):
            run_sync(local, transport, device_id=LOCAL)

        assert transport.downloads[PEER] == 1, "four quiet rounds should not re-read the payload"
        assert _txns(local) == {"coffee": "Dining"}

    def test_a_quiet_round_reports_the_peer_as_unchanged_not_missing(self, pair):
        local, peer, transport = pair
        _add(peer, "coffee")
        run_sync(peer, transport, device_id=PEER)
        run_sync(local, transport, device_id=LOCAL)

        result = run_sync(local, transport, device_id=LOCAL)

        assert result.peers_seen == [PEER], "a skipped peer is still a present peer"
        assert result.merges[PEER] == {"unchanged": True}

    def test_an_idle_device_does_not_reupload(self, pair):
        local, peer, transport = pair
        _add(local, "coffee")
        run_sync(local, transport, device_id=LOCAL)
        uploads = transport.put_count

        for _ in range(3):
            run_sync(local, transport, device_id=LOCAL)

        assert transport.put_count == uploads


class TestNothingIsSkippedThatWasNotAbsorbed:
    def test_a_peer_edit_is_downloaded_and_applied(self, pair):
        local, peer, transport = pair
        txn = _add(peer, "coffee")
        run_sync(peer, transport, device_id=PEER)
        run_sync(local, transport, device_id=LOCAL)

        txn.category = "Groceries"
        txn.category_source = "user"
        peer.commit()
        run_sync(peer, transport, device_id=PEER)
        run_sync(local, transport, device_id=LOCAL)

        assert transport.downloads[PEER] == 2
        assert _txns(local) == {"coffee": "Groceries"}

    def test_a_payload_seen_only_in_the_consent_preview_is_merged_once_consent_arrives(self, tmp_path):
        """Recording the version during the preview would skip it forever after consent."""
        local, peer = _session(tmp_path, "local"), _session(tmp_path, "peer")
        transport = FakeTransport()
        try:
            grant_merge_consent(peer)
            _add(peer, "coffee")
            run_sync(peer, transport, device_id=PEER)

            run_sync(local, transport, device_id=LOCAL)  # first contact: preview only
            assert _txns(local) == {}

            grant_merge_consent(local)
            run_sync(local, transport, device_id=LOCAL)

            assert _txns(local) == {"coffee": "Dining"}, "the unchanged file must still be merged"
        finally:
            local.close()
            peer.close()

    def test_a_schema_upgrade_makes_every_peer_look_new(self, pair, monkeypatch):
        """After an upgrade that syncs more tables, an unchanged peer file holds unread rows."""
        local, peer, transport = pair
        _add(peer, "coffee")
        run_sync(peer, transport, device_id=PEER)
        run_sync(local, transport, device_id=LOCAL)

        monkeypatch.setattr(sync_engine, "_schema_tag", lambda: "f99-t99")
        run_sync(local, transport, device_id=LOCAL)

        assert transport.downloads[PEER] == 2

    def test_a_restore_forgets_what_was_merged(self, pair):
        """The restored database may predate a peer's file, so it must be read again."""
        local, peer, transport = pair
        _add(peer, "coffee")
        run_sync(peer, transport, device_id=PEER)
        run_sync(local, transport, device_id=LOCAL)

        assert forget_merged_versions(local) == 1
        run_sync(local, transport, device_id=LOCAL)

        assert transport.downloads[PEER] == 2

    def test_a_full_round_ignores_the_skip(self, pair):
        local, peer, transport = pair
        _add(peer, "coffee")
        run_sync(peer, transport, device_id=PEER)
        run_sync(local, transport, device_id=LOCAL)

        run_sync(local, transport, device_id=LOCAL, full=True)

        assert transport.downloads[PEER] == 2

    def test_a_failed_merge_is_retried_next_round(self, pair, monkeypatch):
        """The version is stored in the merge's own commit, so a crash leaves it unrecorded."""
        local, peer, transport = pair
        _add(peer, "coffee")
        run_sync(peer, transport, device_id=PEER)

        def boom(db, payload, **kwargs):
            raise RuntimeError("merge failed")

        monkeypatch.setattr(sync_engine, "merge_payload", boom)
        run_sync(local, transport, device_id=LOCAL)
        monkeypatch.undo()
        grant_merge_consent(local)
        run_sync(local, transport, device_id=LOCAL)

        assert _txns(local) == {"coffee": "Dining"}


class TestTheRealTransport:
    def test_drive_does_not_download_a_known_version(self, monkeypatch):
        from src.sync.transport import DriveTransport

        listing = [{"id": "f1", "name": f"device-{PEER}.json", "modifiedTime": "2026-10-05T00:09:19.319Z"}]
        downloads = []
        monkeypatch.setattr("src.vault.google_drive.list_drive_files", lambda *a, **k: listing)
        monkeypatch.setattr(
            "src.vault.google_drive.download_file_bytes",
            lambda token, fid: downloads.append(fid) or json.dumps({"device_id": PEER}).encode(),
        )
        transport = DriveTransport.__new__(DriveTransport)
        transport.access_token = "tok"
        monkeypatch.setattr(DriveTransport, "folder_id", lambda self: "devices")

        first = transport.fetch_others(LOCAL)
        second = transport.fetch_others(LOCAL, known_versions={PEER: first[0].version})

        assert downloads == ["f1"], "the second listing should not download"
        assert second[0].unchanged is True

    def test_a_reupload_changes_the_version(self, monkeypatch):
        from src.sync.transport import DriveTransport

        listing = [{"id": "f1", "name": f"device-{PEER}.json", "modifiedTime": "2026-10-05T00:09:19Z"}]
        monkeypatch.setattr("src.vault.google_drive.list_drive_files", lambda *a, **k: listing)
        monkeypatch.setattr(
            "src.vault.google_drive.download_file_bytes",
            lambda token, fid: json.dumps({"device_id": PEER}).encode(),
        )
        transport = DriveTransport.__new__(DriveTransport)
        transport.access_token = "tok"
        monkeypatch.setattr(DriveTransport, "folder_id", lambda self: "devices")

        old = transport.fetch_others(LOCAL)[0].version
        listing[0]["modifiedTime"] = "2026-10-05T00:14:19Z"
        fresh = transport.fetch_others(LOCAL, known_versions={PEER: old})

        assert fresh[0].unchanged is False and fresh[0].version != old
