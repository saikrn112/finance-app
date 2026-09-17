"""End-to-end sync between devices.

`FakeTransport` (tests/sync_fakes.py) exercises the real publish/fetch/merge path with no network and
no credentials, round-tripping payloads through JSON so a non-serialisable value still fails here.
`DriveTransport` differs only in where the bytes live, and is **not** covered by automated tests -- it
has been exercised only by hand. That gap is recorded in docs/multi_device_sync.md.
"""
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.models import AppMetadata, Base, Project, SyncDevice, Transaction
from src.sync import device as device_mod
from src.sync.engine import (
    PLAID_REPORT_TRUST_WINDOW,
    grant_merge_consent,
    last_plaid_pull_anywhere,
    plaid_pull_is_needed,
    record_plaid_pull,
    run_sync,
)
from src.sync.transport import payload_name
from tests.sync_fakes import FakeTransport
from src.sync.tracking import resume_tombstones

T0 = datetime(2026, 1, 1, 0, 0, 0)


@pytest.fixture(autouse=True)
def _tombstones_on():
    resume_tombstones()
    yield
    resume_tombstones()


class Device:
    """A database plus a device id -- one simulated device."""

    def __init__(self, tmp_path, name, *, merge_consent: bool = True):
        self.name = name
        engine = create_engine(f"sqlite:///{tmp_path}/{name}.db")
        Base.metadata.create_all(bind=engine)
        self.factory = sessionmaker(bind=engine)
        # A device that has already accepted syncing with a peer, which is what these tests are
        # about. First contact is gated on purpose -- see TestFirstContactGate -- and a device
        # without consent publishes but never merges.
        if merge_consent:
            session = self.factory()
            try:
                grant_merge_consent(session)
            finally:
                session.close()

    def session(self):
        return self.factory()

    def sync(self, transport, **kwargs):
        session = self.session()
        try:
            return run_sync(session, transport, device_id=self.name, device_label=self.name.upper(),
                            **kwargs)
        finally:
            session.close()

    def add_transaction(self, source_id="s1", category=None, amount="-42.50", updated_at=T0):
        session = self.session()
        try:
            session.add(
                Transaction(
                    id=f"{self.name}-{source_id}", source="example_bank", source_id=source_id,
                    date=date(2026, 3, 3), amount=Decimal(amount), merchant_raw="EXAMPLE CAFE",
                    category=category, currency="USD",
                )
            )
            session.commit()
            session.execute(text("UPDATE transactions SET updated_at = :t WHERE source_id = :s"),
                            {"t": updated_at, "s": source_id})
            session.commit()
        finally:
            session.close()

    def add_project(self, name="Japan Trip", uid=None, updated_at=T0):
        session = self.session()
        try:
            session.add(Project(id=f"{self.name}-{name}", name=name, uid=uid or f"uid-{name}"))
            session.commit()
            session.execute(text("UPDATE projects SET updated_at = :t WHERE name = :n"),
                            {"t": updated_at, "n": name})
            session.commit()
        finally:
            session.close()

    def snapshot(self):
        session = self.session()
        try:
            return (
                sorted((t.source, t.source_id, str(t._amount), t.category)
                       for t in session.query(Transaction).all()),
                sorted((p.name, p.uid) for p in session.query(Project).all()),
            )
        finally:
            session.close()


@pytest.fixture
def transport(tmp_path):
    return FakeTransport()


@pytest.fixture
def mac(tmp_path):
    return Device(tmp_path, "mac")


@pytest.fixture
def phone(tmp_path):
    return Device(tmp_path, "phone")


class TestTwoDevices:
    def test_data_flows_both_ways_and_both_devices_converge(self, transport, mac, phone):
        mac.add_transaction("mac-1", category="Food/Coffee")
        mac.add_project("Japan Trip")
        phone.add_transaction("phone-1", category="Travel/Taxi")
        phone.add_project("Iceland")

        # Each publishes, then each picks up the other. Two rounds, as in real life.
        mac.sync(transport)
        phone.sync(transport)
        mac.sync(transport)

        assert mac.snapshot() == phone.snapshot()

    def test_a_device_does_not_read_its_own_payload(self, transport, mac):
        mac.add_transaction()
        result = mac.sync(transport)
        assert result.published
        assert result.peers_seen == []

    def test_a_second_sync_with_nothing_new_changes_nothing(self, transport, mac, phone):
        mac.add_transaction()
        mac.sync(transport)
        phone.sync(transport)

        again = phone.sync(transport)
        merges = again.merges.get("mac", {})
        assert not merges.get("inserted") and not merges.get("updated"), merges

    def test_a_third_device_learns_everything_from_one_peer(self, transport, mac, phone, tmp_path):
        """Publishing *after* merging is what makes this work: the Mac's file already contains what
        it learned from the phone, so a tablet gets the whole picture without meeting the phone."""
        mac.add_transaction("mac-1")
        phone.add_transaction("phone-1")

        phone.sync(transport)
        mac.sync(transport)  # merges the phone, then republishes including it

        tablet = Device(tmp_path, "tablet")
        # Only the Mac's file is visible to the tablet.
        transport.delete("phone")
        tablet.sync(transport)

        session = tablet.session()
        try:
            ids = sorted(t.source_id for t in session.query(Transaction).all())
        finally:
            session.close()
        assert ids == ["mac-1", "phone-1"]


class TestRobustness:
    def test_an_unreadable_peer_file_does_not_stop_the_others(self, transport, mac, phone, tmp_path):
        mac.add_transaction("mac-1")
        mac.sync(transport)

        transport.corrupt("corrupt")

        result = phone.sync(transport)
        session = phone.session()
        try:
            assert session.query(Transaction).count() == 1
        finally:
            session.close()
        assert "corrupt" not in result.peers_seen

    def test_a_failing_peer_merge_is_reported_and_the_publish_still_happens(
        self, transport, mac, phone
    ):
        # A payload from a format this build refuses to guess at.
        transport.files["future"] = '{"format_version": 999, "records": {}, "tombstones": []}'

        result = phone.sync(transport)
        assert "future" in result.peers_failed
        assert result.published


class TestDeviceRegistry:
    def test_peers_are_remembered_with_their_labels(self, transport, mac, phone):
        mac.add_transaction()
        mac.sync(transport)
        phone.sync(transport)

        session = phone.session()
        try:
            rows = {d.device_id: d for d in session.query(SyncDevice).all()}
        finally:
            session.close()

        assert set(rows) == {"mac", "phone"}, "this device should appear alongside its peers"
        assert rows["mac"].label == "MAC"

    def test_last_seen_only_moves_forward(self, transport, mac, phone):
        """Payloads can arrive out of order; a device must not appear to get older."""
        mac.add_transaction()
        mac.sync(transport)
        phone.sync(transport)

        session = phone.session()
        try:
            first = session.get(SyncDevice, "mac").last_seen_at
            device_mod.remember_device(session, "mac", last_seen_at=T0 - timedelta(days=5))
            session.commit()
            assert session.get(SyncDevice, "mac").last_seen_at == first
        finally:
            session.close()

    def test_a_label_is_not_erased_by_a_payload_that_omits_it(self, transport, phone):
        session = phone.session()
        try:
            device_mod.remember_device(session, "mac", label="Studio Mac")
            session.commit()
            device_mod.remember_device(session, "mac", label=None)
            session.commit()
            assert session.get(SyncDevice, "mac").label == "Studio Mac"
        finally:
            session.close()


class TestPublishesFullState:
    """`run_sync` publishes everything, every time. That is a correctness requirement, not laziness.

    The obvious optimisation -- publish only rows newer than the last publish -- is wrong, and an
    earlier version of this file asserted the broken behaviour. A row merged from a peer carries
    *that peer's* `updated_at`, and peer clocks are independent, so a freshly-learned row often has a
    timestamp older than this device's own high-water mark. It is then never republished and never
    relayed, so a third device silently never receives it. The randomised soak test found this as
    devices holding different *sets* of transactions.

    Fixing it properly needs a local monotonic marker bumped by merges as well as edits, which
    `updated_at` cannot be because it deliberately carries the originating device's time.
    """

    def test_every_publish_carries_the_whole_history(self, transport, mac):
        import json

        mac.add_transaction("old", updated_at=T0)
        mac.sync(transport)
        mac.add_transaction("new", updated_at=T0 + timedelta(days=1))
        mac.sync(transport)

        payload = transport.raw("mac")
        assert sorted(r["source_id"] for r in payload["records"]["transactions"]) == ["new", "old"]

    def test_a_row_learned_from_a_peer_is_relayed_onward(self, transport, mac, phone, tmp_path):
        """The exact failure the watermark caused. The phone's row is timestamped *older* than
        anything the Mac has published, which is what made the Mac drop it from its own payload."""
        mac.add_transaction("mac-new", updated_at=T0 + timedelta(days=10))
        mac.sync(transport)

        phone.add_transaction("phone-old", updated_at=T0)
        phone.sync(transport)

        mac.sync(transport)  # learns phone-old, and must republish it

        payload = transport.raw("mac")
        assert sorted(r["source_id"] for r in payload["records"]["transactions"]) == [
            "mac-new",
            "phone-old",
        ]

        # And a device that can only see the Mac's file still gets everything.
        tablet = Device(tmp_path, "tablet")
        transport.delete("phone")
        tablet.sync(transport)
        session = tablet.session()
        try:
            assert sorted(t.source_id for t in session.query(Transaction).all()) == [
                "mac-new",
                "phone-old",
            ]
        finally:
            session.close()

    def test_build_payload_still_supports_since_for_callers_that_want_it(self, transport, mac):
        """The primitive is fine; using it for publishing is what was wrong."""
        from src.sync.payload import build_payload

        mac.add_transaction("old", updated_at=T0)
        mac.add_transaction("new", updated_at=T0 + timedelta(days=1))
        session = mac.session()
        try:
            payload = build_payload(
                session, device_id="mac", since=T0 + timedelta(hours=1)
            )
        finally:
            session.close()
        assert [r["source_id"] for r in payload["records"]["transactions"]] == ["new"]


class TestPlaidEconomy:
    """Sharing "who already pulled" is what stops each extra device costing another Plaid bill."""

    def test_a_fresh_pull_by_this_device_means_no_pull_is_needed(self, mac):
        session = mac.session()
        try:
            record_plaid_pull(session, datetime.utcnow() - timedelta(hours=1))
            needed, reason = plaid_pull_is_needed(session, interval=timedelta(hours=24))
        finally:
            session.close()
        assert needed is False
        assert "another device" in reason or "ago" in reason

    def test_a_peers_recent_pull_suppresses_this_devices_pull(self, transport, mac, phone):
        """The money-saving case: the Mac pulled, so the phone must not pull again."""
        session = mac.session()
        try:
            record_plaid_pull(session, datetime.utcnow() - timedelta(hours=2))
        finally:
            session.close()
        mac.add_transaction()
        mac.sync(transport)
        phone.sync(transport)

        session = phone.session()
        try:
            needed, reason = plaid_pull_is_needed(session, interval=timedelta(hours=24))
        finally:
            session.close()
        assert needed is False, reason
        assert "duplicate Plaid call" in reason

    def test_nothing_recorded_anywhere_means_a_pull_is_needed(self, mac):
        session = mac.session()
        try:
            needed, reason = plaid_pull_is_needed(session, interval=timedelta(hours=24))
        finally:
            session.close()
        assert needed is True
        assert "no device" in reason

    def test_an_old_pull_means_a_pull_is_needed(self, mac):
        session = mac.session()
        try:
            record_plaid_pull(session, datetime.utcnow() - timedelta(hours=30))
            needed, _ = plaid_pull_is_needed(session, interval=timedelta(hours=24))
        finally:
            session.close()
        assert needed is True

    def test_an_ancient_peer_report_is_not_trusted(self, transport, mac, phone):
        """A device offline for a week must not keep a live device from refreshing."""
        session = phone.session()
        try:
            session.add(
                AppMetadata(
                    key="plaid_peer_pull_at:ghost",
                    value=(datetime.utcnow() - PLAID_REPORT_TRUST_WINDOW - timedelta(days=1))
                    .replace(microsecond=0)
                    .isoformat()
                    + "Z",
                )
            )
            session.commit()
            needed, reason = plaid_pull_is_needed(session, interval=timedelta(hours=24))
        finally:
            session.close()
        assert needed is True
        assert "trust window" in reason

    def test_the_payload_reports_a_timestamp_and_never_a_cursor_or_token(self, transport, mac):
        """The whole reason this is a timestamp: cursors and tokens live in sync_log and must not
        leave the device."""
        import json

        session = mac.session()
        try:
            record_plaid_pull(session)
        finally:
            session.close()
        mac.sync(transport)

        # The serialised form, not the dict: a secret leaking as a nested value would still be in
        # the bytes that reach the transport.
        raw = transport.files["mac"]
        payload = json.loads(raw)
        assert payload["plaid"]["last_pull_at"]
        assert set(payload["plaid"]) == {"last_pull_at"}
        for forbidden in ("access_token", "plaid_cursor", "client_secret"):
            assert forbidden not in raw.lower()

    def test_duplicate_pulls_cannot_duplicate_a_transaction(self, transport, mac, phone):
        """Why a lease is enough and a lock is not needed: the natural key makes a double pull
        wasteful, not corrupting."""
        mac.add_transaction("shared-1", category="Food/Coffee")
        phone.add_transaction("shared-1", category="Food/Coffee")

        mac.sync(transport)
        phone.sync(transport)

        session = phone.session()
        try:
            assert session.query(Transaction).count() == 1
        finally:
            session.close()


class TestDeviceIdentity:
    def test_the_device_id_is_stable_across_calls(self, tmp_path, monkeypatch):
        from src.config import settings

        monkeypatch.setattr(settings.app, "runtime_dir", str(tmp_path))
        first = device_mod.current_device_id()
        assert device_mod.current_device_id() == first

    def test_the_device_id_is_not_in_the_database(self, tmp_path, monkeypatch):
        """It must not travel: two devices claiming one identity would each ignore the other."""
        from src.config import settings

        monkeypatch.setattr(settings.app, "runtime_dir", str(tmp_path))
        device_mod.current_device_id()
        assert (tmp_path / device_mod.DEVICE_ID_FILENAME).exists()

    def test_the_id_carries_the_platform_so_the_device_list_is_legible(self, tmp_path, monkeypatch):
        from src.config import settings

        monkeypatch.setattr(settings.app, "runtime_dir", str(tmp_path))
        monkeypatch.setenv("FINANCE_APP_PLATFORM", "ios")
        assert device_mod.current_device_id().startswith("ios-")

    def test_a_hex_hostname_is_not_used_as_a_label(self, monkeypatch):
        """A MAC-address-style hostname is worse than useless as a device name."""
        monkeypatch.delenv("FINANCE_APP_DEVICE_LABEL", raising=False)
        monkeypatch.setattr("platform.node", lambda: "a1b2c3d4e5f6")
        assert device_mod.default_device_label() != "a1b2c3d4e5f6"


class TestPublishSkipping:
    """Payloads are full state, so republishing an unchanged one uploads the whole history for
    nothing -- ~2.5 MB, which at a 15-minute poll is a few hundred MB a day."""

    def test_an_unchanged_device_does_not_republish(self, transport, mac):
        mac.add_transaction()
        first = mac.sync(transport)
        before = transport.put_count

        second = mac.sync(transport)

        assert first.published and not first.skipped_publish
        assert second.skipped_publish and not second.published
        assert transport.put_count == before, "the payload was rewritten with identical content"

    def test_a_local_edit_publishes_again(self, transport, mac):
        mac.add_transaction("a")
        mac.sync(transport)
        mac.add_transaction("b", updated_at=T0 + timedelta(days=1))

        assert mac.sync(transport).published is True

    def test_a_delete_publishes_again(self, transport, mac):
        """A tombstone is the one change that need not move any row's updated_at."""
        mac.add_project("Japan Trip")
        mac.sync(transport)

        session = mac.session()
        try:
            session.delete(session.query(Project).one())
            session.commit()
        finally:
            session.close()

        assert mac.sync(transport).published is True

    def test_a_quiet_round_skips_but_a_new_peer_row_publishes(self, transport, mac, phone):
        phone.add_transaction("from-phone")
        phone.sync(transport)
        mac.add_transaction("from-mac")
        mac.sync(transport)

        assert mac.sync(transport).skipped_publish is True

        phone.add_transaction("phone-second", updated_at=T0 + timedelta(days=2))
        phone.sync(transport)
        assert mac.sync(transport).published is True

    def test_a_merged_update_relays_even_when_the_signature_does_not_move(
        self, transport, mac, phone, tmp_path
    ):
        """The case the `merged_something` guard actually exists for.

        The signature is row count plus `max(updated_at)` per table. A merged **update** can move
        neither: the incoming row is newer than *its own* previous value -- so it wins -- while still
        being older than some other row that sets the maximum. Without the guard the signature looks
        unchanged, publishing is skipped, and a third device reaching only this one never learns the
        change. Skipping this construction is why the first version of this test was vacuous: an
        inserted row moves the count, so the signature path published anyway.
        """
        # `late` sets the maximum and is never touched again.
        mac.add_transaction("late", updated_at=T0 + timedelta(days=10))
        mac.add_transaction("target", category="old", updated_at=T0)
        mac.sync(transport)

        phone.sync(transport)  # learns both
        session = phone.session()
        try:
            row = session.query(Transaction).filter_by(source_id="target").one()
            row.category = "new-from-phone"
            # Newer than the row's own T0, older than `late`'s T0+10.
            row.updated_at = T0 + timedelta(days=1)
            session.commit()
        finally:
            session.close()
        phone.sync(transport)

        result = mac.sync(transport)
        assert result.published is True, (
            "a merged update was not relayed: the signature did not move, so it was skipped"
        )

        tablet = Device(tmp_path, "tablet")
        transport.delete("phone")
        tablet.sync(transport)
        session = tablet.session()
        try:
            got = session.query(Transaction).filter_by(source_id="target").one().category
        finally:
            session.close()
        assert got == "new-from-phone", "a device reading only the Mac never got the change"

    def test_force_publish_overrides_the_skip(self, transport, mac):
        mac.add_transaction()
        mac.sync(transport)
        session = mac.session()
        try:
            from src.sync.engine import run_sync

            result = run_sync(session, transport, device_id="mac", force_publish=True)
        finally:
            session.close()
        assert result.published is True

    def test_uid_convergence_relays_even_though_it_moves_no_timestamp(
        self, transport, mac, phone, tmp_path
    ):
        """Convergence must reach a third device, and this is the change least visible to the
        signature: it rewrites `projects.uid` and sets no timestamp of its own.

        It publishes anyway because `updated_at` carries `onupdate`, so the ORM moves the timestamp
        for any modification -- which is what made an explicit "did the merge change anything?" guard
        redundant and got it removed. This test is what demonstrates that, so keep it if the signature
        is ever changed.
        """
        mac.add_project("Japan Trip", uid="uid-zzz")
        # The Mac must publish FIRST, so it has a stored signature to compare against. Without this
        # the skip cannot engage at all -- a first publish always happens -- and the test passes
        # whether or not the guard exists. That was the third vacuous version of this test.
        assert mac.sync(transport).published is True

        phone.add_project("Japan Trip", uid="uid-aaa")
        phone.sync(transport)

        result = mac.sync(transport)  # converges onto uid-aaa

        assert result.merges["phone"]["uid_converged"] == 1
        assert result.published is True, "a converged uid was not relayed onward"

        tablet = Device(tmp_path, "tablet")
        transport.delete("phone")
        tablet.sync(transport)
        session = tablet.session()
        try:
            assert session.query(Project).one().uid == "uid-aaa"
        finally:
            session.close()
