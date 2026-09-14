"""End-to-end sync between devices, over a real transport.

`FolderTransport` against a temp directory exercises the actual publish/fetch/merge path with no
network and no credentials, which is the only way to test convergence honestly. `DriveTransport`
differs only in where the bytes live.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.models import AppMetadata, Base, Project, SyncDevice, Transaction
from src.sync import device as device_mod
from src.sync.engine import (
    PLAID_REPORT_TRUST_WINDOW,
    last_plaid_pull_anywhere,
    plaid_pull_is_needed,
    record_plaid_pull,
    run_sync,
)
from src.sync.transport import FolderTransport, payload_name
from src.sync.tracking import resume_tombstones

T0 = datetime(2026, 1, 1, 0, 0, 0)


@pytest.fixture(autouse=True)
def _tombstones_on():
    resume_tombstones()
    yield
    resume_tombstones()


class Device:
    """A database plus a device id -- one simulated device."""

    def __init__(self, tmp_path, name):
        self.name = name
        engine = create_engine(f"sqlite:///{tmp_path}/{name}.db")
        Base.metadata.create_all(bind=engine)
        self.factory = sessionmaker(bind=engine)

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
    return FolderTransport(tmp_path / "shared")


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
        (transport.root / payload_name("phone")).unlink()
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

        transport.root.mkdir(parents=True, exist_ok=True)
        (transport.root / payload_name("corrupt")).write_text("{not json")

        result = phone.sync(transport)
        session = phone.session()
        try:
            assert session.query(Transaction).count() == 1
        finally:
            session.close()
        assert "corrupt" not in result.peers_seen

    def test_a_write_leaves_no_stray_files_behind(self, transport, mac):
        mac.add_transaction()
        mac.sync(transport)
        assert sorted(p.name for p in transport.root.iterdir()) == [payload_name("mac")]

    def test_a_leftover_partial_file_never_becomes_a_peer_or_a_failure(self, transport, mac, phone):
        """A crash mid-write leaves a temp file behind; syncing must carry on unbothered.

        Note what this does *not* prove: it is not evidence that `TEMP_SUFFIX` is what protects the
        reader. It is not -- `tempfile` generates names like `tmpab12cd`, which never match the
        `device-*` prefix, so setting the suffix to `.json` changes nothing here. Mutation confirmed
        that. Even a file deliberately named `device-ghost.json` is only skipped as unreadable, which
        the corrupt-payload test already covers. The prefix is the real protection.
        """
        mac.add_transaction("mac-1")
        mac.sync(transport)

        from src.sync.transport import TEMP_SUFFIX

        (transport.root / f"device-ghost{TEMP_SUFFIX}").write_text('{"records": {"trans')

        result = phone.sync(transport)
        assert result.peers_seen == ["mac"], f"a stray file was read as a peer: {result.peers_seen}"
        assert not result.peers_failed

    def test_a_failing_peer_merge_is_reported_and_the_publish_still_happens(
        self, transport, mac, phone
    ):
        transport.root.mkdir(parents=True, exist_ok=True)
        # A payload from a format this build refuses to guess at.
        (transport.root / payload_name("future")).write_text(
            '{"format_version": 999, "records": {}, "tombstones": []}'
        )

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


class TestWatermarks:
    def test_the_second_publish_only_carries_what_changed(self, transport, mac):
        mac.add_transaction("old", updated_at=T0)
        mac.sync(transport)

        mac.add_transaction("new", updated_at=T0 + timedelta(days=1))
        mac.sync(transport)

        import json

        payload = json.loads((transport.root / payload_name("mac")).read_text())
        assert [r["source_id"] for r in payload["records"]["transactions"]] == ["new"]

    def test_full_republishes_everything(self, transport, mac):
        mac.add_transaction("old", updated_at=T0)
        mac.sync(transport)
        mac.add_transaction("new", updated_at=T0 + timedelta(days=1))
        mac.sync(transport, full=True)

        import json

        payload = json.loads((transport.root / payload_name("mac")).read_text())
        assert sorted(r["source_id"] for r in payload["records"]["transactions"]) == ["new", "old"]


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

        raw = (transport.root / payload_name("mac")).read_text()
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
