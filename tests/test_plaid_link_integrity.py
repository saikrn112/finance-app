"""Deleting or replacing a transaction must not silently strand the user's work.

SQLite declares these foreign keys `NO ACTION` and runs with `PRAGMA foreign_keys = 0`, so nothing
refuses a delete that leaves join rows pointing at a missing transaction. Rows left that way are
invisible in the app -- every read joins through the parent -- *and* unsyncable, because a row that
cannot be named cannot go in a payload. 77 of them accumulated before anyone noticed, across eight
projects.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.ingestion.plaid_sync import (
    apply_plaid_sync_batch,
    remove_plaid_transaction,
    upsert_plaid_transaction,
)
from src.models import (
    Contact,
    Project,
    Transaction,
    TransactionProject,
    TransactionProjectSplit,
    TransactionSplit,
)
from src.processing.categorizer import RuleMatcher


@pytest.fixture
def matcher(temp_rules_file):
    return RuleMatcher(temp_rules_file)


def _txn(db, source_id, *, source="Chase", origin="plaid", when=date(2026, 9, 10), amount="-40.00"):
    txn = Transaction(
        source=source, source_id=source_id, origin=origin, date=when,
        merchant_raw="EXAMPLE STORE", merchant_clean="Example Store", category="Dining",
    )
    txn.amount = Decimal(amount)
    db.add(txn)
    db.flush()
    return txn


def _file_under_project(db, txn, *, name="Trip", contacts=("self-ore",), share=None):
    project = db.query(Project).filter(Project.name == name).first()
    if project is None:
        project = Project(name=name, color="#22c55e", status="active")
        db.add(project)
        db.flush()
    db.add(TransactionProject(transaction_id=txn.id, project_id=project.id, description="dinner"))
    for cid in contacts:
        if db.query(Contact).filter(Contact.id == cid).first() is None:
            db.add(Contact(id=cid, name=cid, color="#3b82f6"))
        row = TransactionSplit(transaction_id=txn.id, contact_id=cid)
        if share is not None:
            row._share_amount = Decimal(share)
        db.add(row)
    db.flush()
    return project


def _dangling(db) -> dict[str, int]:
    out = {}
    for model in (TransactionProject, TransactionSplit, TransactionProjectSplit):
        rows = db.query(model).all()
        live = {t.id for t in db.query(Transaction).all()}
        out[model.__tablename__] = sum(1 for r in rows if r.transaction_id not in live)
    return out


class TestDeletingATransaction:
    def test_its_links_go_with_it_rather_than_dangling(self, db_session):
        txn = _txn(db_session, "gone-1")
        _file_under_project(db_session, txn)
        db_session.commit()

        removed = remove_plaid_transaction(db_session, "Chase", "gone-1")
        db_session.commit()

        assert removed == 1
        assert _dangling(db_session) == {
            "transaction_projects": 0,
            "transaction_splits": 0,
            "transaction_project_splits": 0,
        }

    def test_other_transactions_links_are_untouched(self, db_session):
        doomed = _txn(db_session, "gone-2")
        keeper = _txn(db_session, "keep-1")
        _file_under_project(db_session, doomed)
        _file_under_project(db_session, keeper)
        db_session.commit()

        remove_plaid_transaction(db_session, "Chase", "gone-2")
        db_session.commit()

        assert db_session.query(TransactionProject).filter_by(transaction_id=keeper.id).count() == 1
        assert db_session.query(TransactionSplit).filter_by(transaction_id=keeper.id).count() == 1

    def test_removing_a_transaction_that_is_not_there_is_harmless(self, db_session):
        assert remove_plaid_transaction(db_session, "Chase", "never-existed") == 0

    def test_a_plaid_removal_batch_leaves_nothing_dangling(self, db_session):
        txn = _txn(db_session, "removed-by-plaid")
        _file_under_project(db_session, txn)
        db_session.commit()

        apply_plaid_sync_batch(
            db_session, institution="Chase", added=[], modified=[],
            removed=["removed-by-plaid"],
        )
        db_session.commit()

        assert sum(_dangling(db_session).values()) == 0


class TestPromotionCarriesTheWork:
    """A pending charge that posts is the same expense; what the user attached must follow."""

    def test_the_split_survives_the_promotion_not_just_the_project(self, db_session, matcher):
        pending = _txn(db_session, "pending-1")
        _file_under_project(db_session, pending, contacts=("self-ore", "friend-1"))
        db_session.commit()

        action = upsert_plaid_transaction(
            db_session, "Chase",
            {
                "source_id": "posted-1", "pending_transaction_id": "pending-1", "pending": False,
                "date": date(2026, 9, 12), "amount": -40.00,
                "merchant_raw": "EXAMPLE STORE", "merchant_clean": "Example Store",
                "account_last4": "7521",
            },
            matcher,
        )
        db_session.commit()

        assert action == "pending_promoted"
        posted = db_session.query(Transaction).filter_by(source_id="posted-1").one()
        assert db_session.query(TransactionProject).filter_by(transaction_id=posted.id).count() == 1
        assert db_session.query(TransactionSplit).filter_by(transaction_id=posted.id).count() == 2, (
            "the project moved but the split did not, so who owed what was lost"
        )

    def test_an_unequal_share_is_carried_across_verbatim(self, db_session, matcher):
        pending = _txn(db_session, "pending-2")
        _file_under_project(db_session, pending, contacts=("self-ore",), share="17.50")
        db_session.commit()

        upsert_plaid_transaction(
            db_session, "Chase",
            {
                "source_id": "posted-2", "pending_transaction_id": "pending-2", "pending": False,
                "date": date(2026, 9, 12), "amount": -40.00,
                "merchant_raw": "EXAMPLE STORE", "merchant_clean": "Example Store",
                "account_last4": "7521",
            },
            matcher,
        )
        db_session.commit()

        posted = db_session.query(Transaction).filter_by(source_id="posted-2").one()
        row = db_session.query(TransactionSplit).filter_by(transaction_id=posted.id).one()
        assert row._share_amount == Decimal("17.50")

    def test_the_per_project_note_is_carried_too(self, db_session, matcher):
        pending = _txn(db_session, "pending-3")
        _file_under_project(db_session, pending)
        db_session.commit()

        upsert_plaid_transaction(
            db_session, "Chase",
            {
                "source_id": "posted-3", "pending_transaction_id": "pending-3", "pending": False,
                "date": date(2026, 9, 12), "amount": -40.00,
                "merchant_raw": "EXAMPLE STORE", "merchant_clean": "Example Store",
                "account_last4": "7521",
            },
            matcher,
        )
        db_session.commit()

        posted = db_session.query(Transaction).filter_by(source_id="posted-3").one()
        link = db_session.query(TransactionProject).filter_by(transaction_id=posted.id).one()
        assert link.description == "dinner"

    def test_nothing_dangles_after_a_promotion(self, db_session, matcher):
        pending = _txn(db_session, "pending-4")
        _file_under_project(db_session, pending, contacts=("self-ore", "friend-1"))
        db_session.commit()

        upsert_plaid_transaction(
            db_session, "Chase",
            {
                "source_id": "posted-4", "pending_transaction_id": "pending-4", "pending": False,
                "date": date(2026, 9, 12), "amount": -40.00,
                "merchant_raw": "EXAMPLE STORE", "merchant_clean": "Example Store",
                "account_last4": "7521",
            },
            matcher,
        )
        db_session.commit()

        assert sum(_dangling(db_session).values()) == 0


class TestStatementOverlap:
    """The statement row already represents the expense, so the Plaid copy goes -- but not the work."""

    def test_the_users_work_moves_to_the_surviving_statement_row(self, db_session, matcher):
        statement = _txn(db_session, "stmt-1", origin="statements", when=date(2026, 9, 9))
        plaid_row = _txn(db_session, "plaid-dup-1", when=date(2026, 9, 10))
        _file_under_project(db_session, plaid_row, contacts=("self-ore", "friend-1"))
        db_session.commit()

        action = upsert_plaid_transaction(
            db_session, "Chase",
            {
                "source_id": "plaid-dup-1", "pending_transaction_id": None, "pending": False,
                "date": date(2026, 9, 10), "amount": -40.00,
                "merchant_raw": "EXAMPLE STORE", "merchant_clean": "Example Store",
                "account_last4": "7521",
            },
            matcher,
        )
        db_session.commit()

        assert action == "skipped_statement_overlap"
        assert db_session.query(TransactionProject).filter_by(transaction_id=statement.id).count() == 1
        assert db_session.query(TransactionSplit).filter_by(transaction_id=statement.id).count() == 2
        assert sum(_dangling(db_session).values()) == 0
