"""Tombstone recording: deletions have to survive a merge.

A delete with no tombstone does not just fail to propagate -- the peer still has the row, sends it
back, and the merge re-inserts it. For a transaction that is a wrong balance, so these tests care
much more about *coverage* of delete paths than about the tombstone's contents.
"""
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from src.models import (
    Contact,
    ContactSplitwiseLink,
    Project,
    ProjectMember,
    Rule,
    Tombstone,
    Transaction,
    TransactionProject,
    TransactionSplit,
)
from src.sync import refs
from src.sync.tracking import bulk_delete, pause_tombstones, resume_tombstones


@pytest.fixture(autouse=True)
def _tombstones_on():
    """Recording is global state, so a test that pauses it must not leak into the next one."""
    resume_tombstones()
    yield
    resume_tombstones()


def _txn(db, source="example_bank", source_id="s1", txn_id="t1"):
    txn = Transaction(
        id=txn_id, source_id=source_id, source=source, date=date(2026, 3, 3),
        amount=Decimal("-10.00"), merchant_raw="EXAMPLE CAFE",
    )
    db.add(txn)
    db.commit()
    return txn


def _tombstone_refs(db, kind: str) -> set[str]:
    return {t.ref for t in db.query(Tombstone).filter(Tombstone.kind == kind).all()}


class TestOrmDeletes:
    def test_deleting_a_project_records_a_tombstone_keyed_by_name(self, db_session):
        db_session.add(Project(id="p1", name="Japan Trip"))
        db_session.commit()

        db_session.delete(db_session.get(Project, "p1"))
        db_session.commit()

        assert _tombstone_refs(db_session, refs.KIND_PROJECT) == {refs.project_ref("Japan Trip")}

    def test_deleting_a_transaction_is_keyed_by_source_and_source_id(self, db_session):
        _txn(db_session)
        db_session.delete(db_session.get(Transaction, "t1"))
        db_session.commit()

        assert _tombstone_refs(db_session, refs.KIND_TRANSACTION) == {
            refs.transaction_ref("example_bank", "s1")
        }

    def test_deleting_a_contact_records_a_tombstone(self, db_session):
        db_session.add(Contact(id="c1", name="Sam"))
        db_session.commit()

        db_session.delete(db_session.get(Contact, "c1"))
        db_session.commit()

        assert _tombstone_refs(db_session, refs.KIND_CONTACT) == {refs.contact_ref("Sam")}

    def test_a_rule_without_a_uid_records_nothing_rather_than_crashing(self, db_session):
        """Pre-backfill rows have no uid, so they cannot be named. Losing propagation is acceptable;
        making every delete route 500 is not."""
        db_session.add(Rule(id="r1", pattern="COFFEE", category="Food/Coffee"))
        db_session.commit()
        db_session.execute(
            __import__("sqlalchemy").text("UPDATE rules SET uid = NULL WHERE id = 'r1'")
        )
        db_session.commit()

        db_session.delete(db_session.get(Rule, "r1"))
        db_session.commit()  # must not raise

        assert db_session.query(Rule).count() == 0


class TestLinkRows:
    def test_a_link_is_named_by_its_parents_not_by_local_ids(self, db_session):
        _txn(db_session)
        db_session.add(Project(id="p1", name="Japan Trip"))
        db_session.commit()
        db_session.add(TransactionProject(transaction_id="t1", project_id="p1"))
        db_session.commit()

        link = db_session.query(TransactionProject).one()
        db_session.delete(link)
        db_session.commit()

        expected = refs.transaction_project_ref(
            refs.transaction_ref("example_bank", "s1"), "Japan Trip"
        )
        assert _tombstone_refs(db_session, refs.KIND_TRANSACTION_PROJECT) == {expected}

    def test_deleting_a_parent_and_its_links_in_one_flush_still_names_the_links(self, db_session):
        """The ordering trap: the links are named after the project, and both go in the same flush.
        Naming has to happen before the DELETE, or the project's name is already unreadable."""
        _txn(db_session)
        db_session.add(Project(id="p1", name="Japan Trip"))
        db_session.commit()
        db_session.add(TransactionProject(transaction_id="t1", project_id="p1"))
        db_session.commit()

        bulk_delete(db_session, TransactionProject, project_id="p1")
        db_session.delete(db_session.get(Project, "p1"))
        db_session.commit()

        assert _tombstone_refs(db_session, refs.KIND_PROJECT) == {refs.project_ref("Japan Trip")}
        assert _tombstone_refs(db_session, refs.KIND_TRANSACTION_PROJECT) == {
            refs.transaction_project_ref(refs.transaction_ref("example_bank", "s1"), "Japan Trip")
        }

    def test_a_split_is_named_by_transaction_and_contact(self, db_session):
        _txn(db_session)
        db_session.add(Contact(id="c1", name="Sam"))
        db_session.commit()
        db_session.add(TransactionSplit(transaction_id="t1", contact_id="c1"))
        db_session.commit()

        bulk_delete(db_session, TransactionSplit, contact_id="c1")
        db_session.commit()

        assert _tombstone_refs(db_session, refs.KIND_TRANSACTION_SPLIT) == {
            refs.transaction_split_ref(refs.transaction_ref("example_bank", "s1"), "Sam")
        }


class TestBulkDelete:
    def test_bulk_delete_records_tombstones_where_query_delete_would_not(self, db_session):
        """The whole reason `bulk_delete` exists."""
        db_session.add(Contact(id="c1", name="Sam"))
        db_session.commit()
        db_session.add(ContactSplitwiseLink(contact_id="c1", splitwise_user_id="42"))
        db_session.commit()

        removed = bulk_delete(db_session, ContactSplitwiseLink, contact_id="c1")
        db_session.commit()

        assert removed == 1
        assert _tombstone_refs(db_session, refs.KIND_CONTACT_SPLITWISE_LINK) == {
            refs.contact_splitwise_link_ref("Sam")
        }

    def test_a_raw_query_delete_records_nothing(self, db_session):
        """Documents the gap `bulk_delete` exists to avoid -- if this ever starts passing, the hook
        has gained bulk-delete visibility and `bulk_delete` may no longer be needed."""
        db_session.add(Contact(id="c1", name="Sam"))
        db_session.commit()
        db_session.add(ContactSplitwiseLink(contact_id="c1", splitwise_user_id="42"))
        db_session.commit()

        db_session.query(ContactSplitwiseLink).filter_by(contact_id="c1").delete()
        db_session.commit()

        assert _tombstone_refs(db_session, refs.KIND_CONTACT_SPLITWISE_LINK) == set()

    def test_bulk_delete_returns_zero_and_records_nothing_when_no_rows_match(self, db_session):
        assert bulk_delete(db_session, ContactSplitwiseLink, contact_id="nope") == 0
        db_session.commit()
        assert db_session.query(Tombstone).count() == 0


class TestPausing:
    def test_paused_recording_writes_no_tombstone(self, db_session):
        """The merge pauses recording: applying a peer's tombstone deletes local rows, and recording
        that as our own deletion would restamp deleted_at with our clock."""
        db_session.add(Project(id="p1", name="Japan Trip"))
        db_session.commit()

        pause_tombstones()
        db_session.delete(db_session.get(Project, "p1"))
        db_session.commit()

        assert db_session.query(Tombstone).count() == 0


class TestIdempotence:
    def test_recording_the_same_ref_twice_keeps_one_row(self, db_session):
        """Deleting a project, then a re-created project of the same name, must not violate the
        composite primary key."""
        for _ in range(2):
            db_session.add(Project(id="p1", name="Japan Trip"))
            db_session.commit()
            db_session.delete(db_session.get(Project, "p1"))
            db_session.commit()

        assert db_session.query(Tombstone).count() == 1


class TestNoUntrackedBulkDeletes:
    """A static guard, so a *future* bulk delete on a synced table fails here instead of silently
    losing deletions. The hook cannot see `query(...).delete()`, and that is invisible at runtime."""

    #: Sites that are bulk deletes on purpose, with the reason.
    ALLOWED = {
        # Clearing transactions is a local repair before re-importing, not a claim the history never
        # existed. Propagating it would let a re-import on one device wipe another.
        ("src/api/routes/settings.py", "Transaction"),
    }

    SYNCED_MODELS = (
        "Transaction", "Project", "Contact", "Rule", "Subscription",
        "TransactionProject", "ProjectMember", "TransactionSplit",
        "TransactionProjectSplit", "ContactSplitwiseLink",
    )

    def test_no_new_untracked_bulk_deletes(self):
        import re

        root = Path(__file__).resolve().parent.parent
        offenders = []
        for path in sorted((root / "src").rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            rel = str(path.relative_to(root))
            text = path.read_text()
            for match in re.finditer(r"query\(\s*(\w+)\s*\)[^\n]*?\.delete\(\)", text):
                model = match.group(1)
                if model not in self.SYNCED_MODELS:
                    continue
                if (rel, model) in self.ALLOWED:
                    continue
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{rel}:{line} bulk-deletes {model}")

        assert not offenders, (
            "These bulk deletes bypass the tombstone hook, so the rows will reappear on the next "
            "merge. Use src.sync.tracking.bulk_delete, or add to ALLOWED with a reason:\n  "
            + "\n  ".join(offenders)
        )
