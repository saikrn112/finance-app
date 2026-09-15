"""The merge engine.

The properties here are what make sync work without a server or a leader, so they are tested as
properties rather than as examples:

* **idempotent** -- merging a payload twice changes nothing the second time
* **order-independent** -- A then B reaches the same state as B then A
* **money is never overwritten** by a peer; only the annotations on it are

Two databases stand in for two devices. Each has its own `id` values for the same logical rows, which
is the whole difficulty and exactly what production looks like.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.models import (
    Base,
    Contact,
    Project,
    ProjectMember,
    Tombstone,
    Transaction,
    TransactionProject,
    TransactionSplit,
)
from src.sync import refs
from src.sync.merge import merge_payload
from src.sync.payload import build_payload
from src.sync.tracking import resume_tombstones

T0 = datetime(2026, 1, 1, 0, 0, 0)


@pytest.fixture(autouse=True)
def _tombstones_on():
    resume_tombstones()
    yield
    resume_tombstones()


def _make_device(tmp_path, name):
    """An independent database, standing in for one device."""
    engine = create_engine(f"sqlite:///{tmp_path}/{name}.db")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def device_a(tmp_path):
    return _make_device(tmp_path, "a")


@pytest.fixture
def device_b(tmp_path):
    return _make_device(tmp_path, "b")


def _add_transaction(db, *, source_id="s1", source="example_bank", amount="-42.50",
                     merchant="EXAMPLE CAFE", category=None, updated_at=T0, local_id=None):
    txn = Transaction(
        id=local_id or f"local-{source_id}-{id(db)}",
        source=source, source_id=source_id, date=date(2026, 3, 3),
        amount=Decimal(amount), merchant_raw=merchant, category=category, currency="USD",
    )
    db.add(txn)
    db.commit()
    db.execute(
        text("UPDATE transactions SET updated_at = :ts WHERE source_id = :sid"),
        {"ts": updated_at, "sid": source_id},
    )
    db.commit()
    db.expire_all()
    return txn


def _add_project(db, name="Japan Trip", uid="uid-japan", color="#111111", updated_at=T0):
    project = Project(id=f"local-{name}-{id(db)}", name=name, uid=uid, color=color)
    db.add(project)
    db.commit()
    db.execute(text("UPDATE projects SET updated_at = :ts WHERE name = :n"),
               {"ts": updated_at, "n": name})
    db.commit()
    db.expire_all()
    return project


def _sync(source_factory, target_factory, *, device_id="dev-a"):
    """Publish from one device and merge into the other. Returns the merge report."""
    source = source_factory()
    try:
        payload = build_payload(source, device_id=device_id)
    finally:
        source.close()
    target = target_factory()
    try:
        return merge_payload(target, payload), payload
    finally:
        target.close()


class TestFactsTravel:
    def test_a_transaction_reaches_the_other_device(self, device_a, device_b):
        a = device_a()
        _add_transaction(a, category="Food/Coffee")
        a.close()

        report, _ = _sync(device_a, device_b)

        b = device_b()
        try:
            txn = b.query(Transaction).one()
            assert txn.source_id == "s1"
            assert txn._amount == Decimal("-42.50")
            assert txn.category == "Food/Coffee"
            # The local id is minted locally and must NOT be copied from the peer.
            assert txn.id != f"local-s1-{id(a)}"
        finally:
            b.close()
        assert report.inserted["transactions"] == 1

    def test_the_amount_survives_the_round_trip_exactly(self, device_a, device_b):
        """Cents must not go through a float."""
        a = device_a()
        _add_transaction(a, amount="-1234.56")
        a.close()
        _sync(device_a, device_b)

        b = device_b()
        try:
            assert b.query(Transaction).one()._amount == Decimal("-1234.56")
        finally:
            b.close()

    def test_merging_twice_is_a_no_op(self, device_a, device_b):
        a = device_a()
        _add_transaction(a)
        a.close()

        first, payload = _sync(device_a, device_b)
        b = device_b()
        try:
            second = merge_payload(b, payload)
        finally:
            b.close()

        assert first.inserted.get("transactions") == 1
        assert not second.changed, f"second merge changed something: {second.as_dict()}"

    def test_a_peer_never_overwrites_the_amount(self, device_a, device_b):
        """The single most important rule: money is a provider fact, not a last-write-wins field."""
        a = device_a()
        _add_transaction(a, amount="-42.50", updated_at=T0)
        a.close()
        _sync(device_a, device_b)

        # B's copy is tampered with, and B then claims a much newer edit.
        b = device_b()
        try:
            b.execute(text("UPDATE transactions SET amount = '-999.00', updated_at = :ts"),
                      {"ts": T0 + timedelta(days=5)})
            b.commit()
        finally:
            b.close()

        _sync(device_b, device_a, device_id="dev-b")

        a = device_a()
        try:
            assert a.query(Transaction).one()._amount == Decimal("-42.50")
        finally:
            a.close()

    def test_annotations_do_follow_last_write_wins(self, device_a, device_b):
        a = device_a()
        _add_transaction(a, category="Food/Coffee", updated_at=T0)
        a.close()
        _sync(device_a, device_b)

        b = device_b()
        try:
            txn = b.query(Transaction).one()
            txn.category = "Food/Groceries"
            txn.updated_at = T0 + timedelta(days=1)
            b.commit()
        finally:
            b.close()

        _sync(device_b, device_a, device_id="dev-b")

        a = device_a()
        try:
            assert a.query(Transaction).one().category == "Food/Groceries"
        finally:
            a.close()

    def test_an_older_edit_does_not_win(self, device_a, device_b):
        a = device_a()
        _add_transaction(a, category="Food/Coffee", updated_at=T0 + timedelta(days=10))
        a.close()
        _sync(device_a, device_b)

        b = device_b()
        try:
            b.execute(text("UPDATE transactions SET category = 'Stale', updated_at = :ts"),
                      {"ts": T0})
            b.commit()
        finally:
            b.close()

        _sync(device_b, device_a, device_id="dev-b")

        a = device_a()
        try:
            assert a.query(Transaction).one().category == "Food/Coffee"
        finally:
            a.close()


class TestOwnedRows:
    def test_a_project_travels_with_its_uid(self, device_a, device_b):
        a = device_a()
        _add_project(a)
        a.close()
        _sync(device_a, device_b)

        b = device_b()
        try:
            project = b.query(Project).one()
            assert (project.name, project.uid) == ("Japan Trip", "uid-japan")
        finally:
            b.close()

    @pytest.mark.parametrize(
        "uid_on_a, uid_on_b",
        [
            ("uid-bbb", "uid-aaa"),  # the incoming uid is larger
            ("uid-aaa", "uid-bbb"),  # the incoming uid is smaller -- the case that must adopt it
        ],
    )
    def test_the_same_project_created_on_both_devices_converges_instead_of_duplicating(
        self, device_a, device_b, uid_on_a, uid_on_b
    ):
        """Independently created, so different uids -- but `name` is unique, so one row must result.

        Both directions are exercised on purpose: with only the "incoming uid is larger" case this
        test passes even with uid convergence disabled, because the receiver's own uid already
        happened to be the winner. Verified by mutation.
        """
        a = device_a()
        _add_project(a, uid=uid_on_a)
        a.close()
        b = device_b()
        _add_project(b, uid=uid_on_b)
        b.close()

        _sync(device_a, device_b)

        b = device_b()
        try:
            projects = b.query(Project).all()
            assert len(projects) == 1
            # Lexicographically smaller uid wins: arbitrary, but both devices compute it alike.
            assert projects[0].uid == min(uid_on_a, uid_on_b)
        finally:
            b.close()

    def test_both_devices_end_on_the_same_uid_after_a_round_trip(self, device_a, device_b):
        """The actual convergence property: not just "one row", but the *same* row everywhere."""
        a = device_a()
        _add_project(a, uid="uid-aaa")
        a.close()
        b = device_b()
        _add_project(b, uid="uid-zzz")
        b.close()

        _sync(device_a, device_b, device_id="dev-a")
        _sync(device_b, device_a, device_id="dev-b")

        def uid_of(factory):
            session = factory()
            try:
                return session.query(Project).one().uid
            finally:
                session.close()

        assert uid_of(device_a) == uid_of(device_b) == "uid-aaa"

    def test_a_rename_propagates(self, device_a, device_b):
        a = device_a()
        _add_project(a, name="Japan Trip", updated_at=T0)
        a.close()
        _sync(device_a, device_b)

        a = device_a()
        try:
            project = a.query(Project).one()
            project.name = "Japan 2026"
            project.updated_at = T0 + timedelta(days=1)
            a.commit()
        finally:
            a.close()

        _sync(device_a, device_b)

        b = device_b()
        try:
            assert [p.name for p in b.query(Project).all()] == ["Japan 2026"]
        finally:
            b.close()

    def test_a_rename_onto_an_existing_name_is_refused_rather_than_aborting_the_merge(
        self, device_a, device_b
    ):
        """`name` is unique, so applying this would raise and stop the device syncing anything."""
        a = device_a()
        _add_project(a, name="Japan Trip", uid="uid-japan", updated_at=T0)
        a.close()
        b = device_b()
        _add_project(b, name="Japan Trip", uid="uid-japan", updated_at=T0)
        _add_project(b, name="Iceland", uid="uid-iceland", updated_at=T0)
        b.close()

        a = device_a()
        try:
            project = a.query(Project).filter_by(name="Japan Trip").one()
            project.name = "Iceland"  # collides on B
            project.updated_at = T0 + timedelta(days=1)
            a.commit()
        finally:
            a.close()

        report, _ = _sync(device_a, device_b)

        b = device_b()
        try:
            names = sorted(p.name for p in b.query(Project).all())
            assert names == ["Iceland", "Japan Trip"], "the merge must not have collapsed the rows"
        finally:
            b.close()
        assert report.rename_conflicts == 1


class TestLinks:
    def test_a_link_is_rebuilt_against_local_ids(self, device_a, device_b):
        a = device_a()
        _add_transaction(a)
        _add_project(a)
        txn = a.query(Transaction).one()
        project = a.query(Project).one()
        a.add(TransactionProject(transaction_id=txn.id, project_id=project.id,
                                 description="dinner"))
        a.commit()
        a.close()

        _sync(device_a, device_b)

        b = device_b()
        try:
            link = b.query(TransactionProject).one()
            local_txn = b.query(Transaction).one()
            local_project = b.query(Project).one()
            assert link.transaction_id == local_txn.id
            assert link.project_id == local_project.id
            assert link.description == "dinner"
        finally:
            b.close()

    def test_a_split_amount_travels_exactly(self, device_a, device_b):
        a = device_a()
        _add_transaction(a)
        a.add(Contact(id="ca", name="Sam", uid="uid-sam"))
        a.commit()
        txn = a.query(Transaction).one()
        split = TransactionSplit(transaction_id=txn.id, contact_id="ca")
        split.share_amount = Decimal("21.25")
        a.add(split)
        a.commit()
        a.close()

        _sync(device_a, device_b)

        b = device_b()
        try:
            assert b.query(TransactionSplit).one()._share_amount == Decimal("21.25")
        finally:
            b.close()

    def test_a_link_whose_parent_is_missing_is_reported_not_crashed(self, device_a, device_b):
        """Self-heals: payloads are full state, so the next merge retries once the parent lands."""
        a = device_a()
        _add_transaction(a)
        _add_project(a)
        txn = a.query(Transaction).one()
        project = a.query(Project).one()
        a.add(TransactionProject(transaction_id=txn.id, project_id=project.id))
        a.commit()
        payload = build_payload(a, device_id="dev-a")
        a.close()

        # Deliver only the link, without the transaction or project it needs.
        payload["records"].pop("transactions", None)
        payload["records"].pop("projects", None)

        b = device_b()
        try:
            report = merge_payload(b, payload)
            assert report.unresolved.get("transaction_projects") == 1
            assert b.query(TransactionProject).count() == 0
        finally:
            b.close()

    def test_the_link_arrives_once_its_parents_do(self, device_a, device_b):
        a = device_a()
        _add_transaction(a)
        _add_project(a)
        txn = a.query(Transaction).one()
        project = a.query(Project).one()
        a.add(TransactionProject(transaction_id=txn.id, project_id=project.id))
        a.commit()
        a.close()

        _sync(device_a, device_b)  # full payload, parents included

        b = device_b()
        try:
            assert b.query(TransactionProject).count() == 1
        finally:
            b.close()


class TestDeletions:
    def test_a_delete_propagates(self, device_a, device_b):
        a = device_a()
        _add_project(a)
        a.close()
        _sync(device_a, device_b)

        a = device_a()
        try:
            a.delete(a.query(Project).one())
            a.commit()
        finally:
            a.close()

        report, _ = _sync(device_a, device_b)

        b = device_b()
        try:
            assert b.query(Project).count() == 0
        finally:
            b.close()
        assert report.deletions_applied == 1

    def test_the_peer_does_not_resurrect_a_deleted_row(self, device_a, device_b):
        """The failure this whole mechanism exists to prevent: B still holds the row and sends it
        back, and without tombstones A re-inserts it."""
        a = device_a()
        _add_project(a)
        a.close()
        _sync(device_a, device_b)

        a = device_a()
        try:
            a.delete(a.query(Project).one())
            a.commit()
        finally:
            a.close()

        # B has not heard about the deletion yet and publishes its copy.
        _sync(device_b, device_a, device_id="dev-b")

        a = device_a()
        try:
            assert a.query(Project).count() == 0, "the deleted project came back"
        finally:
            a.close()

    def test_a_deletion_timestamp_is_not_restamped_on_relay(self, device_a, device_b):
        """The originating time is what orders a deletion against edits, so every device must hold
        the same value for it."""
        a = device_a()
        _add_project(a)
        a.close()
        _sync(device_a, device_b)

        a = device_a()
        try:
            a.delete(a.query(Project).one())
            a.commit()
            original = a.query(Tombstone).one().deleted_at
        finally:
            a.close()

        _sync(device_a, device_b)

        b = device_b()
        try:
            assert b.query(Tombstone).one().deleted_at == original
        finally:
            b.close()

    def test_an_edit_after_the_delete_recreates_the_row(self, device_a, device_b):
        """Otherwise deleting "Japan Trip" makes that name unusable forever."""
        a = device_a()
        _add_project(a, updated_at=T0)
        a.close()
        _sync(device_a, device_b)

        a = device_a()
        try:
            a.delete(a.query(Project).one())
            a.commit()
            deleted_at = a.query(Tombstone).one().deleted_at
        finally:
            a.close()

        b = device_b()
        try:
            project = b.query(Project).one()
            project.color = "#222222"
            project.updated_at = deleted_at + timedelta(hours=1)
            b.commit()
        finally:
            b.close()

        _sync(device_b, device_a, device_id="dev-b")

        a = device_a()
        try:
            assert a.query(Project).count() == 1
        finally:
            a.close()


class TestTiedTimestamps:
    """Equal `updated_at` with different values.

    Not an edge case here: `backfill-sync-identity` derives timestamps, so every pre-sync row on both
    devices carries the *same* value. "Keep the local value on a tie" would mean each device keeps its
    own and they never agree.
    """

    def test_a_tie_resolves_the_same_way_on_both_devices(self, device_a, device_b):
        a = device_a()
        _add_transaction(a, category="Food/Coffee", updated_at=T0)
        a.close()
        b = device_b()
        _add_transaction(b, category="Travel/Taxi", updated_at=T0)
        b.close()

        _sync(device_a, device_b, device_id="dev-a")
        _sync(device_b, device_a, device_id="dev-b")

        def category_of(factory):
            session = factory()
            try:
                return session.query(Transaction).one().category
            finally:
                session.close()

        assert category_of(device_a) == category_of(device_b), (
            "the two devices disagreed on a tie, so they will never converge"
        )

    def test_a_tie_is_broken_by_content_not_by_which_device_asked(self, device_a, device_b):
        """Both directions must pick the same winner, or syncing twice flip-flops forever."""
        a = device_a()
        _add_transaction(a, category="AAA", updated_at=T0)
        a.close()
        b = device_b()
        _add_transaction(b, category="ZZZ", updated_at=T0)
        b.close()

        _sync(device_a, device_b, device_id="dev-a")
        b = device_b()
        try:
            after_a_into_b = b.query(Transaction).one().category
        finally:
            b.close()

        _sync(device_b, device_a, device_id="dev-b")
        a = device_a()
        try:
            after_b_into_a = a.query(Transaction).one().category
        finally:
            a.close()

        assert after_a_into_b == after_b_into_a == "ZZZ"


class TestConvergence:
    def test_order_does_not_matter(self, tmp_path):
        """A then B must reach the same state as B then A -- the property that lets devices converge
        with no coordination and no leader."""
        payloads = []
        for index, (project_uid, category) in enumerate(
            [("uid-aaa", "Food/Coffee"), ("uid-bbb", "Food/Groceries")]
        ):
            factory = _make_device(tmp_path, f"src{index}")
            session = factory()
            _add_transaction(session, source_id=f"s{index}", category=category,
                             updated_at=T0 + timedelta(days=index))
            _add_project(session, name=f"Trip {index}", uid=project_uid,
                         updated_at=T0 + timedelta(days=index))
            payloads.append(build_payload(session, device_id=f"dev-{index}"))
            session.close()

        states = []
        for order in ([0, 1], [1, 0]):
            factory = _make_device(tmp_path, f"target-{order[0]}{order[1]}")
            session = factory()
            for index in order:
                merge_payload(session, payloads[index])
            states.append(
                (
                    sorted((t.source_id, str(t._amount), t.category)
                           for t in session.query(Transaction).all()),
                    sorted((p.name, p.uid, p.color) for p in session.query(Project).all()),
                )
            )
            session.close()

        assert states[0] == states[1]

    def test_a_full_round_trip_leaves_both_devices_identical(self, device_a, device_b):
        a = device_a()
        _add_transaction(a, source_id="s1", category="Food/Coffee")
        _add_project(a, name="Japan Trip", uid="uid-japan")
        a.close()
        b = device_b()
        _add_transaction(b, source_id="s2", category="Travel/Flights")
        _add_project(b, name="Iceland", uid="uid-iceland")
        b.close()

        _sync(device_a, device_b, device_id="dev-a")
        _sync(device_b, device_a, device_id="dev-b")
        _sync(device_a, device_b, device_id="dev-a")

        def snapshot(factory):
            session = factory()
            try:
                return (
                    sorted((t.source, t.source_id, str(t._amount), t.category)
                           for t in session.query(Transaction).all()),
                    sorted((p.name, p.uid) for p in session.query(Project).all()),
                )
            finally:
                session.close()

        assert snapshot(device_a) == snapshot(device_b)


class TestPayloadSafety:
    def test_a_payload_carries_no_excluded_table(self, device_a):
        from src.sync.payload import FORBIDDEN_TABLES

        a = device_a()
        try:
            _add_transaction(a)
            payload = build_payload(a, device_id="dev-a")
        finally:
            a.close()

        assert not (set(payload["records"]) & set(FORBIDDEN_TABLES))

    def test_a_payload_containing_a_secret_is_refused(self, device_a):
        """The guard is what stops a future field publishing a Plaid token to cloud storage."""
        from src.sync.payload import assert_no_secrets

        with pytest.raises(AssertionError, match="secret"):
            assert_no_secrets({"records": {"transactions": [{"access_token": "abc"}]}})

    def test_the_watermark_reflects_the_newest_row_not_the_clock(self, device_a):
        a = device_a()
        try:
            _add_transaction(a, source_id="old", updated_at=T0)
            _add_transaction(a, source_id="new", updated_at=T0 + timedelta(days=3))
            payload = build_payload(a, device_id="dev-a")
        finally:
            a.close()

        from src.sync.coding import datetime_from_wire

        assert datetime_from_wire(payload["watermark"]) == T0 + timedelta(days=3)

    def test_since_limits_the_payload(self, device_a):
        a = device_a()
        try:
            _add_transaction(a, source_id="old", updated_at=T0)
            _add_transaction(a, source_id="new", updated_at=T0 + timedelta(days=3))
            payload = build_payload(a, device_id="dev-a", since=T0 + timedelta(days=1))
        finally:
            a.close()

        assert [r["source_id"] for r in payload["records"]["transactions"]] == ["new"]

    def test_a_newer_payload_format_is_refused_rather_than_guessed_at(self, device_a):
        a = device_a()
        try:
            with pytest.raises(ValueError, match="newer"):
                merge_payload(a, {"format_version": 999, "records": {}, "tombstones": []})
        finally:
            a.close()

class TestUnsyncableRows:
    """Rows that cannot be named. Found by running against a real database, not by unit tests --
    61 link rows there pointed at transactions that no longer existed."""

    def test_an_orphaned_link_is_reported_rather_than_silently_dropped(self, device_a):
        from src.sync.payload import find_unsyncable

        a = device_a()
        try:
            _add_transaction(a)
            _add_project(a)
            project = a.query(Project).one()
            # A link to a transaction that does not exist. SQLite permits this: foreign keys are not
            # enforced unless explicitly switched on, which is how the real rows got there.
            a.add(TransactionProject(transaction_id="gone", project_id=project.id))
            a.commit()

            assert find_unsyncable(a) == {"transaction_projects": 1}
        finally:
            a.close()

    def test_an_orphaned_link_is_left_out_of_the_payload(self, device_a):
        a = device_a()
        try:
            _add_transaction(a)
            _add_project(a)
            project = a.query(Project).one()
            a.add(TransactionProject(transaction_id="gone", project_id=project.id))
            a.commit()
            payload = build_payload(a, device_id="dev-a")
        finally:
            a.close()

        assert "transaction_projects" not in payload["records"]

    def test_a_healthy_database_reports_nothing_unsyncable(self, device_a):
        from src.sync.payload import find_unsyncable

        a = device_a()
        try:
            _add_transaction(a)
            _add_project(a)
            txn = a.query(Transaction).one()
            project = a.query(Project).one()
            a.add(TransactionProject(transaction_id=txn.id, project_id=project.id))
            a.commit()

            assert find_unsyncable(a) == {}
        finally:
            a.close()


class TestUnknownAge:
    """A row with no `updated_at` must not be stamped with the receiving device's clock.

    Found on real data, where 2427 transactions had a NULL updated_at. The models declare
    `default=datetime.utcnow`, so inserting such a row without an explicit stamp claimed it had just
    been edited -- and that fabricated timestamp then beats a peer's real, older value, silently
    overwriting a genuine annotation with a stale one.
    """

    def test_an_undated_row_is_inserted_as_unknown_age_not_as_now(self, device_a, device_b):
        from src.sync.merge import UNKNOWN_AGE

        a = device_a()
        try:
            _add_transaction(a)
            a.execute(text("UPDATE transactions SET updated_at = NULL"))
            a.commit()
        finally:
            a.close()

        _sync(device_a, device_b)

        b = device_b()
        try:
            assert b.query(Transaction).one().updated_at == UNKNOWN_AGE
        finally:
            b.close()

    def test_a_real_edit_elsewhere_beats_an_undated_row(self, device_a, device_b):
        """The consequence that matters: the fabricated timestamp used to win this."""
        a = device_a()
        try:
            _add_transaction(a, category="Real annotation", updated_at=T0)
            a.close()
        except Exception:
            a.close()
            raise

        # B receives it as undated, as an old database would hold it.
        b = device_b()
        try:
            _add_transaction(b, category="Stale")
            b.execute(text("UPDATE transactions SET updated_at = NULL"))
            b.commit()
        finally:
            b.close()

        _sync(device_b, device_a, device_id="dev-b")

        a = device_a()
        try:
            assert a.query(Transaction).one().category == "Real annotation"
        finally:
            a.close()

    def test_both_devices_agree_on_the_age_of_an_undated_row(self, device_a, device_b):
        """Deterministic, so neither device wins spuriously."""
        a = device_a()
        try:
            _add_transaction(a)
            a.execute(text("UPDATE transactions SET updated_at = NULL"))
            a.commit()
        finally:
            a.close()

        _sync(device_a, device_b, device_id="dev-a")
        _sync(device_b, device_a, device_id="dev-b")

        def stamp(factory):
            session = factory()
            try:
                return session.query(Transaction).one().updated_at
            finally:
                session.close()

        assert stamp(device_a) == stamp(device_b)
