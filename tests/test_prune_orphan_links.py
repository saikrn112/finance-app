"""Pruning join rows whose transaction is gone.

They are unreachable, not merely untidy: every read joins through the parent, and a row that cannot
be named is excluded from every sync payload. So deleting them removes nothing visible -- but it is
still a delete against real data, hence preview-then-apply.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.ingestion.prune_orphan_links import preview_prune, run_prune
from src.models import (
    Contact, Project, Transaction, TransactionProject, TransactionProjectSplit, TransactionSplit,
)


def _seed(db, *, orphan: bool, tag: str = "a"):
    project = Project(name=f"Trip {tag}", color="#22c55e", status="active")
    contact = Contact(id=f"c-{tag}", name=f"Someone {tag}", color="#3b82f6")
    db.add_all([project, contact])
    db.flush()

    txn = Transaction(source="Chase", source_id=f"s-{tag}", origin="plaid", date=date(2026, 9, 10),
                      merchant_raw="X", merchant_clean="X", category="Dining")
    txn.amount = Decimal("-10.00")
    db.add(txn)
    db.flush()
    txn_id = txn.id

    db.add(TransactionProject(transaction_id=txn_id, project_id=project.id))
    db.add(TransactionSplit(transaction_id=txn_id, contact_id=contact.id))
    db.add(TransactionProjectSplit(transaction_id=txn_id, project_id=project.id,
                                   contact_id=contact.id))
    db.flush()
    if orphan:
        # Exactly how they arose: the transaction goes, the links stay.
        db.delete(txn)
    db.commit()
    return txn_id, project.id


def test_preview_reports_what_would_go_and_writes_nothing(db_session):
    _seed(db_session, orphan=True)

    preview = preview_prune(db_session)

    assert preview["total_rows"] == 3
    assert preview["missing_transactions"] == 1
    assert preview["affected_projects"] == {"Trip a": 1}
    assert preview_prune(db_session)["total_rows"] == 3, "preview must not mutate"


def test_apply_deletes_only_the_orphans(db_session):
    _seed(db_session, orphan=True)
    live_txn_id, project_id = _seed(db_session, orphan=False, tag="b")

    result = run_prune(db_session)

    assert result["total_deleted"] == 3
    assert result["remaining"] == 0
    assert db_session.query(TransactionProject).filter_by(transaction_id=live_txn_id).count() == 1
    assert db_session.query(TransactionSplit).filter_by(transaction_id=live_txn_id).count() == 1


def test_it_is_idempotent(db_session):
    _seed(db_session, orphan=True)
    run_prune(db_session)

    second = run_prune(db_session)

    assert second["total_deleted"] == 0
    assert preview_prune(db_session)["total_rows"] == 0


def test_a_clean_database_is_a_no_op(db_session):
    _seed(db_session, orphan=False)

    assert preview_prune(db_session)["total_rows"] == 0
    assert run_prune(db_session)["total_deleted"] == 0
