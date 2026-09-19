"""A row that exists again must not still be under a tombstone.

Otherwise every peer deletes it on their next merge while the local copy sits there looking fine.
This is how the splits route lost data: it removes a transaction's split rows and re-inserts them
under the same identity on every edit, so each edit left a tombstone behind.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.ingestion.retract_stale_tombstones import preview_retraction, run_retraction
from src.models import Contact, Tombstone, Transaction, TransactionSplit
from src.sync import refs
from src.sync.tracking import resume_tombstones


def _seed(db):
    resume_tombstones()
    txn = Transaction(source="Chase", source_id="s-1", origin="plaid", date=date(2026, 9, 10),
                      merchant_raw="X", merchant_clean="X", category="Dining")
    txn.amount = Decimal("-40.00")
    contact = Contact(id="c-1", name="Someone", color="#22c55e")
    db.add_all([txn, contact])
    db.commit()
    return txn, contact


class TestRetractionOnRecreate:
    def test_re_creating_a_row_clears_its_tombstone(self, db_session):
        txn, contact = _seed(db_session)
        split = TransactionSplit(transaction_id=txn.id, contact_id=contact.id)
        db_session.add(split)
        db_session.commit()

        db_session.delete(split)
        db_session.commit()
        assert db_session.query(Tombstone).count() == 1, "the delete should record one"

        db_session.add(TransactionSplit(transaction_id=txn.id, contact_id=contact.id))
        db_session.commit()

        assert db_session.query(Tombstone).count() == 0, (
            "a tombstone left over a live row makes every peer delete it"
        )

    def test_a_delete_that_stays_deleted_keeps_its_tombstone(self, db_session):
        txn, contact = _seed(db_session)
        split = TransactionSplit(transaction_id=txn.id, contact_id=contact.id)
        db_session.add(split)
        db_session.commit()

        db_session.delete(split)
        db_session.commit()

        assert db_session.query(Tombstone).count() == 1, "propagation must survive"

    def test_an_unrelated_tombstone_is_untouched(self, db_session):
        txn, contact = _seed(db_session)
        db_session.add(
            Tombstone(kind=refs.KIND_PROJECT, ref=refs.project_ref("Deleted Trip"))
        )
        db_session.commit()

        db_session.add(TransactionSplit(transaction_id=txn.id, contact_id=contact.id))
        db_session.commit()

        assert db_session.query(Tombstone).count() == 1


class TestRepairOfExistingState:
    def test_preview_finds_tombstones_contradicted_by_a_live_row(self, db_session):
        txn, contact = _seed(db_session)
        split = TransactionSplit(transaction_id=txn.id, contact_id=contact.id)
        db_session.add(split)
        db_session.commit()
        # Forge the state the fix now prevents: a tombstone alongside its live row.
        db_session.add(
            Tombstone(
                kind=refs.KIND_TRANSACTION_SPLIT,
                ref=refs.transaction_split_ref(refs.transaction_ref("Chase", "s-1"), "Someone"),
            )
        )
        db_session.commit()

        preview = preview_retraction(db_session)

        assert preview["contradicted"] == 1
        assert preview["by_kind"] == {refs.KIND_TRANSACTION_SPLIT: 1}

    def test_apply_removes_only_the_contradicted_ones(self, db_session):
        txn, contact = _seed(db_session)
        db_session.add(TransactionSplit(transaction_id=txn.id, contact_id=contact.id))
        db_session.add(Tombstone(kind=refs.KIND_PROJECT, ref=refs.project_ref("Gone")))
        db_session.add(
            Tombstone(
                kind=refs.KIND_TRANSACTION_SPLIT,
                ref=refs.transaction_split_ref(refs.transaction_ref("Chase", "s-1"), "Someone"),
            )
        )
        db_session.commit()

        result = run_retraction(db_session)

        assert result["retracted"] == 1
        assert result["remaining"] == 0
        assert db_session.query(Tombstone).count() == 1, "a genuine deletion must survive"

    def test_it_is_idempotent(self, db_session):
        _seed(db_session)
        run_retraction(db_session)
        assert run_retraction(db_session)["retracted"] == 0
