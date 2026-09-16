"""What a wrong clock does to sync.

Last-write-wins is decided by comparing `updated_at` across devices, so a device whose clock is wrong
does not merely mis-order its own edits -- it wins or loses *every* comparison against its peers. This
was listed as untested; these tests say what actually happens, so the behaviour is known rather than
assumed.

They are deliberately descriptive. A clock-independent design would need Lamport or vector clocks and
a different wire format; the point here is that the failure is bounded and money is never affected.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.models import Base, Project, Transaction
from src.sync.engine import run_sync
from src.sync.tracking import resume_tombstones
from tests.sync_fakes import FakeTransport

NOW = datetime(2026, 6, 1, 12, 0, 0)
FAST = NOW + timedelta(days=30)   # a device a month ahead
SLOW = NOW - timedelta(days=30)   # a month behind


@pytest.fixture(autouse=True)
def _tombstones_on():
    resume_tombstones()
    yield
    resume_tombstones()


class Dev:
    def __init__(self, tmp_path, name):
        self.name = name
        engine = create_engine(f"sqlite:///{tmp_path}/{name}.db")
        Base.metadata.create_all(bind=engine)
        self.factory = sessionmaker(bind=engine)

    def session(self):
        return self.factory()

    def sync(self, transport):
        s = self.session()
        try:
            return run_sync(s, transport, device_id=self.name, device_label=self.name)
        finally:
            s.close()

    def add_txn(self, source_id, category, when, amount="-25.00"):
        s = self.session()
        try:
            s.add(Transaction(
                id=f"{self.name}-{source_id}", source="bank", source_id=source_id,
                date=date(2026, 3, 1), amount=Decimal(amount), merchant_raw="EXAMPLE CAFE",
                category=category, currency="USD",
            ))
            s.commit()
            s.execute(text("UPDATE transactions SET updated_at=:t WHERE source_id=:s"),
                      {"t": when, "s": source_id})
            s.commit()
        finally:
            s.close()

    def edit_txn(self, source_id, category, when):
        s = self.session()
        try:
            row = s.query(Transaction).filter_by(source_id=source_id).one()
            row.category = category
            row.updated_at = when
            s.commit()
        finally:
            s.close()

    def category(self, source_id):
        s = self.session()
        try:
            return s.query(Transaction).filter_by(source_id=source_id).one().category
        finally:
            s.close()

    def amount(self, source_id):
        s = self.session()
        try:
            return s.query(Transaction).filter_by(source_id=source_id).one()._amount
        finally:
            s.close()


def converge(devices, transport, rounds=4):
    for _ in range(rounds):
        for d in devices:
            d.sync(transport)


class TestSkewedClocks:
    def test_a_fast_clock_wins_every_annotation_conflict(self, tmp_path):
        """The headline consequence, and the reason this is a known weakness rather than a bug: a
        device a month ahead beats a correct device's later edit."""
        transport = FakeTransport()
        fast, good = Dev(tmp_path, "fast"), Dev(tmp_path, "good")
        fast.add_txn("t1", "from-fast", FAST)
        good.add_txn("t1", "from-good", NOW)

        converge([fast, good], transport)

        assert fast.category("t1") == "from-fast"
        assert good.category("t1") == "from-fast", "the skewed device did not win, so the rule changed"

    def test_a_slow_clock_loses_even_its_newest_edit(self, tmp_path):
        transport = FakeTransport()
        slow, good = Dev(tmp_path, "slow"), Dev(tmp_path, "good")
        good.add_txn("t1", "from-good", NOW)
        converge([good, slow], transport)

        # The slow device edits *after* seeing the row, but stamps it a month in the past.
        slow.edit_txn("t1", "from-slow", SLOW)
        converge([slow, good], transport)

        assert good.category("t1") == "from-good"

    def test_devices_still_converge_despite_skew(self, tmp_path):
        """Skew changes *which* value wins, not whether the devices agree. Agreement is the property
        that must survive, because a permanent disagreement is unfixable without manual work."""
        transport = FakeTransport()
        fast, good, slow = Dev(tmp_path, "fast"), Dev(tmp_path, "good"), Dev(tmp_path, "slow")
        fast.add_txn("t1", "fast", FAST)
        good.add_txn("t1", "good", NOW)
        slow.add_txn("t1", "slow", SLOW)

        converge([fast, good, slow], transport, rounds=6)

        values = {d.name: d.category("t1") for d in (fast, good, slow)}
        assert len(set(values.values())) == 1, f"devices disagree under skew: {values}"

    def test_money_is_unaffected_by_skew(self, tmp_path):
        """Amounts are immutable facts, not LWW fields, so no clock can rewrite one."""
        transport = FakeTransport()
        fast, good = Dev(tmp_path, "fast"), Dev(tmp_path, "good")
        good.add_txn("t1", "good", NOW, amount="-25.00")
        converge([good, fast], transport)

        # The fast device tampers with the amount and claims a far-future edit.
        s = fast.session()
        try:
            s.execute(text("UPDATE transactions SET amount='-9999.00', updated_at=:t"), {"t": FAST})
            s.commit()
        finally:
            s.close()
        converge([fast, good], transport)

        assert good.amount("t1") == Decimal("-25.00"), "a skewed clock rewrote an amount"

    def test_a_future_stamped_row_does_not_freeze_later_real_edits_forever(self, tmp_path):
        """A row stamped in the future is unbeatable until the real clock passes it. Bounded by the
        size of the skew, which is why this is a weakness and not a deadlock."""
        transport = FakeTransport()
        fast, good = Dev(tmp_path, "fast"), Dev(tmp_path, "good")
        fast.add_txn("t1", "from-fast", FAST)
        converge([fast, good], transport)

        # A real edit made after the skewed timestamp has passed does win.
        good.edit_txn("t1", "eventually", FAST + timedelta(seconds=1))
        converge([good, fast], transport)

        assert fast.category("t1") == "eventually"


class TestConcurrentPublishers:
    def test_two_devices_publishing_before_reading_still_converge(self, tmp_path):
        """The closest this suite gets to concurrency: both publish from an unmerged state, so each
        one's file predates knowing about the other."""
        transport = FakeTransport()
        a, b = Dev(tmp_path, "a"), Dev(tmp_path, "b")
        a.add_txn("only-a", "a-cat", NOW)
        b.add_txn("only-b", "b-cat", NOW)

        # Publish both before either fetches.
        a.sync(transport)
        b.sync(transport)
        converge([a, b], transport)

        for dev in (a, b):
            s = dev.session()
            try:
                assert sorted(t.source_id for t in s.query(Transaction).all()) == ["only-a", "only-b"]
            finally:
                s.close()
