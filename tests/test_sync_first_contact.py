"""First contact asks before merging another device's history.

Why this gate exists, rather than merging on sight: tombstones only cover deletions made *after*
sync shipped, so the first merge between two long-lived databases produces their union and
resurrects anything either side deleted beforehand. That is not hypothetical here -- it is exactly
why the macOS app had to be reseeded from the container by hand instead of just being switched on.

Modelled on Timeslice's `pendingFirstMerge`, which publishes but does not merge until answered.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base
from src.models.transaction import Transaction
from src.sync.engine import (
    grant_merge_consent,
    merge_consent_given,
    pending_first_merge,
    run_sync,
)
from tests.sync_fakes import FakeTransport


@pytest.fixture
def fresh_db(tmp_path):
    """A device that has never answered the first-merge question."""
    engine = create_engine(f"sqlite:///{tmp_path}/fresh.db")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def peer_payload(tmp_path):
    """A peer's full-state payload holding two transactions this device has never seen."""
    engine = create_engine(f"sqlite:///{tmp_path}/peer.db")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        for i in (1, 2):
            txn = Transaction(
                source="Example Card", source_id=f"peer-{i}", origin="plaid",
                date=datetime(2026, 9, 10).date(), merchant_raw=f"PEER {i}",
                merchant_clean=f"Peer {i}", category="Dining",
            )
            txn.amount = -10 * i
            session.add(txn)
        session.commit()

        from src.sync.payload import build_payload

        payload = build_payload(session, device_id="macos-1", device_label="MacBook")
        return json.loads(json.dumps(payload, default=str))
    finally:
        session.close()


def _serve(transport: FakeTransport, payload: dict) -> None:
    transport.files[payload["device_id"]] = json.dumps(payload, default=str)


class TestGate:
    def test_consent_is_absent_on_a_fresh_install(self, fresh_db):
        assert merge_consent_given(fresh_db) is False

    def test_a_peers_history_is_not_absorbed_before_it_is_answered(self, fresh_db, peer_payload):
        transport = FakeTransport()
        _serve(transport, peer_payload)

        run_sync(fresh_db, transport, device_id="container-1")

        assert fresh_db.query(Transaction).count() == 0, (
            "a first merge would union two databases and resurrect pre-sync deletions"
        )

    def test_it_still_publishes_while_waiting(self, fresh_db, peer_payload):
        """Otherwise the device sits signed in, invisible to peers and permanently out of date."""
        transport = FakeTransport()
        _serve(transport, peer_payload)

        result = run_sync(fresh_db, transport, device_id="container-1")

        assert result.published is True
        assert "container-1" in transport.files

    def test_the_prompt_carries_real_counts_not_just_a_peer_exists(self, fresh_db, peer_payload):
        transport = FakeTransport()
        _serve(transport, peer_payload)

        run_sync(fresh_db, transport, device_id="container-1")

        pending = pending_first_merge(fresh_db)
        assert pending is not None
        assert pending["device_id"] == "macos-1"
        assert pending["device_label"] == "MacBook"
        assert pending["would_insert"] >= 2, f"expected the two peer transactions, got {pending}"

    def test_the_preview_changes_nothing_it_counts(self, fresh_db, peer_payload):
        transport = FakeTransport()
        _serve(transport, peer_payload)

        run_sync(fresh_db, transport, device_id="container-1")
        run_sync(fresh_db, transport, device_id="container-1")

        assert fresh_db.query(Transaction).count() == 0, "the preview must roll back"

    def test_the_result_says_it_is_waiting_rather_than_reporting_a_clean_merge(self, fresh_db, peer_payload):
        transport = FakeTransport()
        _serve(transport, peer_payload)

        result = run_sync(fresh_db, transport, device_id="container-1")

        assert result.merges["macos-1"]["awaiting_consent"] is True

    def test_a_peer_is_remembered_even_while_waiting(self, fresh_db, peer_payload):
        """The UI cannot offer the choice for a device it does not know exists."""
        from src.models import SyncDevice

        transport = FakeTransport()
        _serve(transport, peer_payload)

        run_sync(fresh_db, transport, device_id="container-1")

        assert fresh_db.query(SyncDevice).filter(SyncDevice.device_id == "macos-1").first()

    def test_lease_timestamps_are_absorbed_even_while_waiting(self, fresh_db, peer_payload):
        """Timestamps are not the user's history, and withholding them would make both devices
        spend Plaid calls and take duplicate backups while the question sits unanswered."""
        from src.sync import coding
        from src.sync.engine import last_backup_anywhere

        when = datetime(2026, 9, 17, 5, 0, 0)
        peer_payload["backup"] = {"last_backup_at": coding.datetime_to_wire(when)}
        transport = FakeTransport()
        _serve(transport, peer_payload)

        run_sync(fresh_db, transport, device_id="container-1")

        assert last_backup_anywhere(fresh_db) == when


class TestAfterConsent:
    def test_granting_consent_lets_the_next_round_merge(self, fresh_db, peer_payload):
        transport = FakeTransport()
        _serve(transport, peer_payload)
        run_sync(fresh_db, transport, device_id="container-1")

        grant_merge_consent(fresh_db)
        run_sync(fresh_db, transport, device_id="container-1")

        assert fresh_db.query(Transaction).count() == 2

    def test_the_prompt_does_not_come_back(self, fresh_db, peer_payload):
        transport = FakeTransport()
        _serve(transport, peer_payload)
        run_sync(fresh_db, transport, device_id="container-1")
        grant_merge_consent(fresh_db)

        run_sync(fresh_db, transport, device_id="container-1")

        assert pending_first_merge(fresh_db) is None
        assert merge_consent_given(fresh_db) is True

    def test_consent_survives_a_new_session(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path}/persist.db")
        Base.metadata.create_all(bind=engine)
        factory = sessionmaker(bind=engine)

        first = factory()
        grant_merge_consent(first)
        first.close()

        second = factory()
        try:
            assert merge_consent_given(second) is True
        finally:
            second.close()
