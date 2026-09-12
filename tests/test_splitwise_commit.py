"""Splitwise staging, batching, and amend behaviour.

The network layer is faked. What matters here is the logic that cannot be checked against
the live API without credentials: that a project drains in batches, that progress survives
a mid-batch failure, that an unmapped member blocks the commit outright, and that shares
reconcile exactly.
"""
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from src.integrations import splitwise
from src.models import (
    Contact,
    ContactSplitwiseLink,
    Project,
    ProjectMember,
    SplitwiseCommit,
    Transaction,
    TransactionProject,
    TransactionSplit,
)
from src.services import splitwise_commit as svc


class FakeSplitwise:
    """Records calls; mimics create/update_expense return shapes."""

    def __init__(self, fail_on: int | None = None):
        self.created: list[dict] = []
        self.updated: list[tuple[int, dict]] = []
        self.groups: list[dict] = []
        self.fail_on = fail_on
        self._next_id = 1000

    build_expense_payload = staticmethod(splitwise.build_expense_payload)

    def create_group(self, _token, *, name, member_user_ids):
        self.groups.append({"name": name, "members": member_user_ids})
        return {"id": 555}

    def create_expense(self, _token, payload):
        if self.fail_on is not None and len(self.created) == self.fail_on:
            raise HTTPException(status_code=502, detail="Splitwise rate limited")
        self.created.append(payload)
        self._next_id += 1
        return {"id": self._next_id}

    def update_expense(self, _token, expense_id, payload):
        self.updated.append((expense_id, payload))
        return {"id": expense_id}


@pytest.fixture
def project_env(db_session):
    """A project, a self-contact, one friend, and 6 equally-split transactions."""
    me = Contact(name="Owner", is_self=True)
    friend = Contact(name="Friend")
    db_session.add_all([me, friend])
    db_session.flush()
    db_session.add_all([
        ContactSplitwiseLink(contact_id=me.id, splitwise_user_id="1"),
        ContactSplitwiseLink(contact_id=friend.id, splitwise_user_id="2"),
    ])

    project = Project(name="Trip")
    db_session.add(project)
    db_session.flush()
    db_session.add_all([
        ProjectMember(project_id=project.id, contact_id=me.id),
        ProjectMember(project_id=project.id, contact_id=friend.id),
    ])

    for index in range(6):
        txn = Transaction(
            source_id=f"sw{index}", source="example_card", date=date(2026, 3, index + 1),
            amount=Decimal("-100.00"), merchant_raw=f"MERCHANT {index}",
            merchant_clean=f"Merchant {index}", category="Dining", currency="USD",
        )
        db_session.add(txn)
        db_session.flush()
        db_session.add(TransactionProject(
            transaction_id=txn.id, project_id=project.id, description=f"note {index}",
        ))
        for contact in (me, friend):
            # Splits are keyed by transaction only: who owes what is a property of the
            # expense, not of the project it happens to be filed under.
            db_session.add(TransactionSplit(
                transaction_id=txn.id, contact_id=contact.id,
            ))
    db_session.commit()
    return project, me, friend


def test_commit_drains_in_batches(db_session, project_env):
    project, _, _ = project_env
    api = FakeSplitwise()

    first = svc.commit_project(db_session, project.id, "tok", batch_size=4, api=api)
    assert first["created"] == 4
    assert first["pending"] == 2
    assert first["done"] is False

    second = svc.commit_project(db_session, project.id, "tok", batch_size=4, api=api)
    assert second["created"] == 2
    assert second["pending"] == 0
    assert second["done"] is True

    # Six expenses, no duplicates.
    assert len(api.created) == 6
    assert len({p["description"] for p in api.created}) == 6


def test_already_committed_transactions_are_not_resent(db_session, project_env):
    project, _, _ = project_env
    api = FakeSplitwise()
    svc.commit_project(db_session, project.id, "tok", batch_size=10, api=api)
    again = svc.commit_project(db_session, project.id, "tok", batch_size=10, api=api)
    assert again["created"] == 0
    assert again["skipped"] == 6
    assert len(api.created) == 6


def test_progress_survives_a_mid_batch_failure(db_session, project_env):
    """A rate limit must not lose the expenses that already succeeded."""
    project, _, _ = project_env
    api = FakeSplitwise(fail_on=2)  # third expense blows up

    result = svc.commit_project(db_session, project.id, "tok", batch_size=6, api=api)
    assert result["created"] == 2
    assert result["batch_failures"], "the failure should be reported"

    committed = db_session.query(SplitwiseCommit).filter_by(state="committed").count()
    failed = db_session.query(SplitwiseCommit).filter_by(state="failed").count()
    assert committed == 2
    assert failed == 1

    # Retrying with a healthy API finishes the rest, including the previously failed one.
    healthy = FakeSplitwise()
    resumed = svc.commit_project(db_session, project.id, "tok", batch_size=6, api=healthy)
    assert resumed["created"] == 4
    assert resumed["pending"] == 0


def test_unmapped_member_blocks_the_commit(db_session, project_env):
    project, _, _ = project_env
    stranger = Contact(name="Stranger")
    db_session.add(stranger)
    db_session.flush()
    db_session.add(ProjectMember(project_id=project.id, contact_id=stranger.id))
    db_session.commit()

    api = FakeSplitwise()
    with pytest.raises(svc.SplitwiseNotReady) as excinfo:
        svc.commit_project(db_session, project.id, "tok", api=api)
    assert "Stranger" in excinfo.value.detail
    assert api.created == [], "nothing may be pushed when someone is unmapped"


def test_missing_self_contact_blocks_the_commit(db_session, project_env):
    project, me, _ = project_env
    me.is_self = False
    db_session.commit()

    with pytest.raises(svc.SplitwiseNotReady) as excinfo:
        svc.commit_project(db_session, project.id, "tok", api=FakeSplitwise())
    assert "yourself" in excinfo.value.detail


def test_unequal_shares_are_pushed_verbatim(db_session, project_env):
    project, me, friend = project_env
    txn_id = db_session.query(TransactionProject).filter_by(project_id=project.id).first().transaction_id
    rows = db_session.query(TransactionSplit).filter_by(transaction_id=txn_id).all()
    for row in rows:
        row.share_amount = Decimal("75.00") if row.contact_id == me.id else Decimal("25.00")
    db_session.commit()

    api = FakeSplitwise()
    svc.commit_project(db_session, project.id, "tok", batch_size=1, api=api)

    payload = api.created[0]
    owed = {payload[f"users__{i}__user_id"]: payload[f"users__{i}__owed_share"] for i in (0, 1)}
    assert owed == {1: "75.00", 2: "25.00"}
    # Splitwise requires both sides to reconcile to cost.
    assert sum(float(v) for k, v in payload.items() if k.endswith("owed_share")) == 100.0
    assert sum(float(v) for k, v in payload.items() if k.endswith("paid_share")) == 100.0


def test_amend_updates_the_existing_expense_instead_of_duplicating(db_session, project_env):
    project, me, friend = project_env
    api = FakeSplitwise()
    svc.commit_project(db_session, project.id, "tok", batch_size=1, api=api)
    assert len(api.created) == 1

    # Change the shares after committing.
    txn_id = api.created[0]["description"]  # not an id; look the transaction up properly
    record = db_session.query(SplitwiseCommit).filter_by(state="committed").first()
    rows = db_session.query(TransactionSplit).filter_by(transaction_id=record.transaction_id).all()
    for row in rows:
        row.share_amount = Decimal("60.00") if row.contact_id == me.id else Decimal("40.00")
    db_session.commit()

    amended = svc.commit_project(db_session, project.id, "tok", batch_size=1, amend=True, api=api)
    assert amended["updated"] == 1
    assert len(api.created) == 1, "amend must not create a second expense"
    assert api.updated and api.updated[0][0] == int(record.expense_id)


def test_amend_leaves_unchanged_transactions_alone(db_session, project_env):
    project, _, _ = project_env
    api = FakeSplitwise()
    svc.commit_project(db_session, project.id, "tok", batch_size=6, api=api)
    result = svc.commit_project(db_session, project.id, "tok", batch_size=6, amend=True, api=api)
    assert result["updated"] == 0
    assert api.updated == []


def test_group_is_created_once_and_reused(db_session, project_env):
    project, _, _ = project_env
    api = FakeSplitwise()
    svc.commit_project(db_session, project.id, "tok", batch_size=2, api=api)
    svc.commit_project(db_session, project.id, "tok", batch_size=2, api=api)
    assert len(api.groups) == 1
    assert project.splitwise_group_id == "555"
    assert all(p["group_id"] == 555 for p in api.created)


def test_preview_reports_what_a_commit_would_do(db_session, project_env):
    project, _, _ = project_env
    before = svc.pending_summary(db_session, project.id)
    assert before == {
        "project_id": project.id,
        "transactions": 6,
        "committed": 0,
        "pending": 6,
        "failed": [],
        "batch_size": before["batch_size"],
        "unmapped_members": [],
    }
    svc.commit_project(db_session, project.id, "tok", batch_size=4, api=FakeSplitwise())
    after = svc.pending_summary(db_session, project.id)
    assert (after["committed"], after["pending"]) == (4, 2)
