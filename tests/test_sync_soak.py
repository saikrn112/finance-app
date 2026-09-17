"""Randomised multi-device soak: does sync converge from *arbitrary* histories?

The hand-written tests each check one rule. They cannot answer the question that actually matters --
"after any sequence of edits on any devices, in any sync order, do all devices agree?" -- because the
interesting failures come from interactions between rules that no one thought to combine.

So this generates random operations across N devices, syncs them in random order, and then drives
them to quiescence and asserts every device holds identical state. A seed is printed on failure so
any counterexample is reproducible.

What this deliberately measures:

* **Convergence** -- all devices identical at the end.
* **Quiescence** -- syncing stops changing things within a bounded number of rounds. A system that
  converges in value but never stops publishing would burn Drive calls forever.
* **Conservation of money** -- no amount is ever altered by merging; the union of transactions is
  exactly what was created.

What it does not measure: concurrency (every operation here is sequential) and clock skew. Both are
listed as untested in docs/multi_device_sync.md.
"""
from __future__ import annotations

import hashlib
import random
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
    Transaction,
    TransactionProject,
    TransactionSplit,
)
from src.sync.engine import grant_merge_consent, run_sync
from src.sync.tracking import resume_tombstones
from tests.sync_fakes import FakeTransport

T0 = datetime(2026, 1, 1)

CATEGORIES = ["Food/Coffee", "Food/Groceries", "Travel/Taxi", "Home/Rent", None]
PROJECT_NAMES = ["Japan Trip", "Iceland", "House Move", "Wedding"]
CONTACT_NAMES = ["Sam", "Alex", "Jo", "Kit"]


@pytest.fixture(autouse=True)
def _tombstones_on():
    resume_tombstones()
    yield
    resume_tombstones()


def _facts_for(source_id: str) -> tuple[Decimal, str]:
    """The immutable provider facts for a natural key -- identical on every device, as in reality."""
    digest = int(hashlib.sha256(source_id.encode()).hexdigest(), 16)
    return Decimal(f"-{digest % 9000 + 1}.{digest % 90 + 10}"), f"MERCHANT {digest % 9}"


class SoakDevice:
    def __init__(self, tmp_path, name):
        self.name = name
        engine = create_engine(f"sqlite:///{tmp_path}/{name}.db")
        Base.metadata.create_all(bind=engine)
        self.factory = sessionmaker(bind=engine)
        self.clock = T0
        # These devices model an install whose owner has already accepted syncing with a peer.
        # Without consent the engine publishes but never merges -- correct for first contact, and
        # not what convergence is being tested against here.
        session = self.factory()
        try:
            grant_merge_consent(session)
        finally:
            session.close()

    def tick(self) -> datetime:
        """A monotonic per-device clock. Distinct values so most comparisons are decided by time and
        the content tie-break is exercised only where timestamps genuinely coincide."""
        self.clock += timedelta(minutes=1)
        return self.clock

    def session(self):
        return self.factory()

    def sync(self, transport):
        session = self.session()
        try:
            return run_sync(session, transport, device_id=self.name, device_label=self.name)
        finally:
            session.close()

    def state(self):
        """Everything that is supposed to be identical across devices."""
        session = self.session()
        try:
            txns = sorted(
                (t.source, t.source_id, str(t._amount), t.currency, t.category, t.notes)
                for t in session.query(Transaction).all()
            )
            projects = sorted(
                (p.name, p.uid, p.color, p.status) for p in session.query(Project).all()
            )
            contacts = sorted((c.name, c.uid, c.color) for c in session.query(Contact).all())
            # Links compared by the parents' natural names, since local ids differ per device.
            links = sorted(
                (t.source_id, p.name, link.description)
                for link, t, p in session.query(TransactionProject, Transaction, Project)
                .filter(Transaction.id == TransactionProject.transaction_id)
                .filter(Project.id == TransactionProject.project_id)
                .all()
            )
            splits = sorted(
                (t.source_id, c.name, str(s._share_amount))
                for s, t, c in session.query(TransactionSplit, Transaction, Contact)
                .filter(Transaction.id == TransactionSplit.transaction_id)
                .filter(Contact.id == TransactionSplit.contact_id)
                .all()
            )
            members = sorted(
                (p.name, c.name)
                for m, p, c in session.query(ProjectMember, Project, Contact)
                .filter(Project.id == ProjectMember.project_id)
                .filter(Contact.id == ProjectMember.contact_id)
                .all()
            )
            return {
                "transactions": txns, "projects": projects, "contacts": contacts,
                "links": links, "splits": splits, "members": members,
            }
        finally:
            session.close()


# --- random operations -------------------------------------------------------------------------


def _op_add_transaction(rng, device):
    session = device.session()
    try:
        source_id = f"txn-{rng.randrange(1, 40)}"
        if session.query(Transaction).filter_by(source="bank", source_id=source_id).first():
            return
        # The amount and merchant are derived from the natural key, not drawn at random.
        # Two devices holding the same (source, source_id) with *different* amounts is not a
        # divergence the merge could ever resolve -- the key asserts they are the same transaction
        # while the values say otherwise -- and Plaid never does that. Generating it would be testing
        # an impossible input. (This bit the first run of this suite.)
        amount, merchant = _facts_for(source_id)
        txn = Transaction(
            id=f"{device.name}-{source_id}-{rng.random()}", source="bank", source_id=source_id,
            date=date(2026, 3, 1), amount=amount,
            merchant_raw=merchant, currency="USD",
            category=rng.choice(CATEGORIES),
        )
        session.add(txn)
        session.commit()
        session.execute(text("UPDATE transactions SET updated_at=:t WHERE source_id=:s"),
                        {"t": device.tick(), "s": source_id})
        session.commit()
    finally:
        session.close()


def _op_edit_transaction(rng, device):
    session = device.session()
    try:
        rows = session.query(Transaction).all()
        if not rows:
            return
        row = rng.choice(rows)
        row.category = rng.choice(CATEGORIES)
        row.notes = rng.choice([None, "checked", "disputed"])
        row.updated_at = device.tick()
        session.commit()
    finally:
        session.close()


def _op_add_project(rng, device):
    session = device.session()
    try:
        name = rng.choice(PROJECT_NAMES)
        if session.query(Project).filter_by(name=name).first():
            return
        # A deliberately *device-specific* uid, so independent creation of the same name is common and
        # the uid-convergence path gets exercised hard.
        session.add(Project(id=f"{device.name}-{name}", name=name,
                            uid=f"uid-{device.name}-{name}", color="#111111"))
        session.commit()
        session.execute(text("UPDATE projects SET updated_at=:t WHERE name=:n"),
                        {"t": device.tick(), "n": name})
        session.commit()
    finally:
        session.close()


def _op_add_contact(rng, device):
    session = device.session()
    try:
        name = rng.choice(CONTACT_NAMES)
        if session.query(Contact).filter_by(name=name).first():
            return
        session.add(Contact(id=f"{device.name}-{name}", name=name, uid=f"uid-{device.name}-{name}"))
        session.commit()
        session.execute(text("UPDATE contacts SET updated_at=:t WHERE name=:n"),
                        {"t": device.tick(), "n": name})
        session.commit()
    finally:
        session.close()


def _op_edit_project(rng, device):
    session = device.session()
    try:
        rows = session.query(Project).all()
        if not rows:
            return
        row = rng.choice(rows)
        row.color = rng.choice(["#111111", "#222222", "#333333"])
        row.status = rng.choice(["active", "completed", "archived"])
        row.updated_at = device.tick()
        session.commit()
    finally:
        session.close()


def _op_link_transaction(rng, device):
    session = device.session()
    try:
        txns = session.query(Transaction).all()
        projects = session.query(Project).all()
        if not txns or not projects:
            return
        txn, project = rng.choice(txns), rng.choice(projects)
        if session.query(TransactionProject).filter_by(
            transaction_id=txn.id, project_id=project.id
        ).first():
            return
        link = TransactionProject(transaction_id=txn.id, project_id=project.id,
                                  description=rng.choice([None, "dinner", "hotel"]))
        session.add(link)
        session.commit()
        session.execute(
            text("UPDATE transaction_projects SET updated_at=:t WHERE transaction_id=:x"),
            {"t": device.tick(), "x": txn.id},
        )
        session.commit()
    finally:
        session.close()


def _op_add_split(rng, device):
    session = device.session()
    try:
        txns = session.query(Transaction).all()
        contacts = session.query(Contact).all()
        if not txns or not contacts:
            return
        txn, contact = rng.choice(txns), rng.choice(contacts)
        if session.query(TransactionSplit).filter_by(
            transaction_id=txn.id, contact_id=contact.id
        ).first():
            return
        split = TransactionSplit(transaction_id=txn.id, contact_id=contact.id)
        split.share_amount = Decimal(f"{rng.randrange(1, 99)}.{rng.randrange(10, 99)}")
        session.add(split)
        session.commit()
        session.execute(
            text("UPDATE transaction_splits SET updated_at=:t WHERE transaction_id=:x"),
            {"t": device.tick(), "x": txn.id},
        )
        session.commit()
    finally:
        session.close()


def _op_add_member(rng, device):
    session = device.session()
    try:
        projects = session.query(Project).all()
        contacts = session.query(Contact).all()
        if not projects or not contacts:
            return
        project, contact = rng.choice(projects), rng.choice(contacts)
        if session.query(ProjectMember).filter_by(
            project_id=project.id, contact_id=contact.id
        ).first():
            return
        session.add(ProjectMember(project_id=project.id, contact_id=contact.id))
        session.commit()
    finally:
        session.close()


def _op_delete_project(rng, device):
    session = device.session()
    try:
        rows = session.query(Project).all()
        if not rows:
            return
        project = rng.choice(rows)
        from src.sync.tracking import bulk_delete

        bulk_delete(session, TransactionProject, project_id=project.id)
        bulk_delete(session, ProjectMember, project_id=project.id)
        session.delete(project)
        session.commit()
    finally:
        session.close()


def _op_delete_transaction(rng, device):
    session = device.session()
    try:
        rows = session.query(Transaction).all()
        if not rows:
            return
        txn = rng.choice(rows)
        from src.sync.tracking import bulk_delete

        bulk_delete(session, TransactionProject, transaction_id=txn.id)
        bulk_delete(session, TransactionSplit, transaction_id=txn.id)
        session.delete(txn)
        session.commit()
    finally:
        session.close()


OPERATIONS = [
    (_op_add_transaction, 10),
    (_op_edit_transaction, 8),
    (_op_add_project, 5),
    (_op_add_contact, 5),
    (_op_edit_project, 4),
    (_op_link_transaction, 6),
    (_op_add_split, 5),
    (_op_add_member, 4),
    (_op_delete_project, 2),
    (_op_delete_transaction, 2),
]
_WEIGHTED = [op for op, weight in OPERATIONS for _ in range(weight)]


def _drive_to_quiescence(devices, transport, max_rounds=12):
    """Sync everyone until nothing changes. Returns the number of rounds needed.

    Bounded: a system that converges in value but never stops reporting changes would publish
    forever, and that is a defect worth failing on rather than looping past.
    """
    for round_no in range(1, max_rounds + 1):
        changed = False
        for device in devices:
            result = device.sync(transport)
            for report in result.merges.values():
                if report.get("error"):
                    raise AssertionError(f"merge error on {device.name}: {report['error']}")
                if (
                    report.get("inserted")
                    or report.get("updated")
                    or report.get("deletions_applied")
                    or report.get("uid_converged")
                ):
                    changed = True
        if not changed:
            return round_no
    raise AssertionError(f"did not quiesce within {max_rounds} rounds")


@pytest.mark.parametrize("seed", range(12))
def test_random_histories_converge(tmp_path, seed):
    """N devices, random edits, random sync order -- every device must end up identical."""
    rng = random.Random(seed)
    device_count = rng.choice([2, 3, 4])
    devices = [SoakDevice(tmp_path, f"dev{i}") for i in range(device_count)]
    transport = FakeTransport()

    for _ in range(rng.randrange(20, 60)):
        device = rng.choice(devices)
        rng.choice(_WEIGHTED)(rng, device)
        # Sync at random moments, so merges interleave with edits rather than all happening at the end.
        if rng.random() < 0.35:
            rng.choice(devices).sync(transport)

    rounds = _drive_to_quiescence(devices, transport)

    states = [device.state() for device in devices]
    first = states[0]
    for device, state in zip(devices[1:], states[1:]):
        if state != first:
            differing = {k for k in first if first[k] != state[k]}
            raise AssertionError(
                f"seed={seed}: {devices[0].name} and {device.name} disagree on {sorted(differing)}\n"
                f"  {devices[0].name}: { {k: first[k] for k in differing} }\n"
                f"  {device.name}: { {k: state[k] for k in differing} }"
            )
    assert rounds <= 12


@pytest.mark.parametrize("seed", range(6))
def test_money_is_never_altered_by_merging(tmp_path, seed):
    """Every amount on every device must be one that was actually created somewhere.

    Convergence alone would be satisfied by all devices agreeing on a *wrong* number, so this checks
    conservation separately.
    """
    rng = random.Random(1000 + seed)
    devices = [SoakDevice(tmp_path, f"dev{i}") for i in range(3)]
    transport = FakeTransport()

    created: dict[tuple[str, str], str] = {}
    for _ in range(40):
        device = rng.choice(devices)
        _op_add_transaction(rng, device)
        session = device.session()
        try:
            for txn in session.query(Transaction).all():
                # Derived from the key, so every device must agree on it.
                created[(txn.source, txn.source_id)] = str(_facts_for(txn.source_id)[0])
        finally:
            session.close()
        _op_edit_transaction(rng, device)
        if rng.random() < 0.4:
            rng.choice(devices).sync(transport)

    _drive_to_quiescence(devices, transport)

    for device in devices:
        session = device.session()
        try:
            for txn in session.query(Transaction).all():
                key = (txn.source, txn.source_id)
                assert str(txn._amount) == created[key], (
                    f"seed={seed}: {device.name} has {txn._amount} for {key}, "
                    f"but {created[key]} was created"
                )
        finally:
            session.close()


def test_a_late_joining_device_catches_up(tmp_path):
    """A phone switched on after weeks must reach the same state, not a partial one."""
    rng = random.Random(99)
    established = [SoakDevice(tmp_path, "mac"), SoakDevice(tmp_path, "laptop")]
    transport = FakeTransport()

    for _ in range(40):
        rng.choice(_WEIGHTED)(rng, rng.choice(established))
        if rng.random() < 0.3:
            rng.choice(established).sync(transport)
    _drive_to_quiescence(established, transport)

    latecomer = SoakDevice(tmp_path, "phone")
    everyone = established + [latecomer]
    _drive_to_quiescence(everyone, transport)

    expected = established[0].state()
    assert latecomer.state() == expected
