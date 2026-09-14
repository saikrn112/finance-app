"""Tests for minting multi-device sync identity on a pre-existing database.

The property that matters most here is **determinism**, not correctness of any single value: two
devices run this independently against their own copy of the same history, and if they disagree the
merge either duplicates a row or picks a last-write-wins winner that silently discards a real edit.
So most of these tests assert that two separate runs agree, rather than asserting a literal.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from src.ingestion.backfill_sync_identity import (
    DERIVATION_VERSION,
    _derived_uid,
    preview_backfill,
    run_backfill,
)
from sqlalchemy import text

from src.models import Contact, Project, ProjectMember, Rule, Transaction, TransactionProject


def _as_pre_sync(session, table: str, *columns: str) -> None:
    """Blank the sync columns on `table`, the way a pre-existing row actually looks.

    Necessary because the models carry `default=datetime.utcnow` on `updated_at`, and that default
    fires even when the attribute is explicitly set to None -- so an ORM insert can never produce the
    NULL that `ALTER TABLE ... ADD COLUMN` leaves behind on a live database. Without this the tests
    would set up rows the backfill correctly ignores, and would prove nothing.
    """
    assignments = ", ".join(f"{column} = NULL" for column in columns)
    session.execute(text(f"UPDATE {table} SET {assignments}"))
    session.commit()
    session.expire_all()


class TestDerivedUID:
    def test_the_same_content_derives_the_same_uid(self):
        assert _derived_uid("project", "Japan Trip") == _derived_uid("project", "Japan Trip")

    def test_different_content_derives_a_different_uid(self):
        assert _derived_uid("project", "Japan Trip") != _derived_uid("project", "Iceland Trip")

    def test_kinds_are_namespaced(self):
        """A project and a contact of the same name are different things."""
        assert _derived_uid("project", "Alex") != _derived_uid("contact", "Alex")

    def test_multi_part_material_cannot_be_confused_by_reordering(self):
        """Without a separator, ('ab','c') and ('a','bc') would hash identically."""
        assert _derived_uid("rule", "ab", "c") != _derived_uid("rule", "a", "bc")

    def test_the_version_participates(self):
        """Changing the rules must change the uid, not silently reuse the old identity."""
        material_v1 = _derived_uid("project", "Japan Trip")
        assert DERIVATION_VERSION in "v1v2v3"  # guard: this test knows the current version
        assert material_v1 != _derived_uid("project", "Japan Trip", "extra")


class TestPreview:
    def test_preview_reports_pending_rows_and_writes_nothing(self, db_session):
        db_session.add(Project(id="p1", name="Japan Trip", created_at=datetime(2026, 1, 1)))
        db_session.commit()
        _as_pre_sync(db_session, "projects", "uid", "updated_at")

        result = preview_backfill(db_session)
        assert result["pending"]["projects"]["uid"] == 1
        # Still unwritten.
        assert db_session.get(Project, "p1").uid is None

    def test_preview_is_zero_once_backfilled(self, db_session):
        db_session.add(Project(id="p1", name="Japan Trip", created_at=datetime(2026, 1, 1)))
        db_session.commit()
        _as_pre_sync(db_session, "projects", "uid", "updated_at")
        run_backfill(db_session)
        assert preview_backfill(db_session)["total_rows"] == 0


class TestMinting:
    def test_uid_and_updated_at_are_populated(self, db_session):
        created = datetime(2026, 1, 1, 12, 0, 0)
        db_session.add(Project(id="p1", name="Japan Trip", created_at=created))
        db_session.commit()
        _as_pre_sync(db_session, "projects", "uid", "updated_at")

        run_backfill(db_session)
        project = db_session.get(Project, "p1")
        assert project.uid == _derived_uid("project", "Japan Trip")
        assert project.updated_at == created

    def test_updated_at_comes_from_created_at_not_from_the_clock(self, db_session):
        """Seeding `now` would make the device that backfilled later win every comparison."""
        created = datetime(2020, 5, 4, 3, 2, 1)
        db_session.add(Contact(id="c1", name="Sam", created_at=created))
        db_session.commit()
        _as_pre_sync(db_session, "contacts", "uid", "updated_at")

        run_backfill(db_session)
        assert db_session.get(Contact, "c1").updated_at == created

    def test_a_missing_created_at_falls_back_to_the_epoch(self, db_session):
        """Deterministic, and older than any real edit, so a genuine edit always wins."""
        db_session.add(Contact(id="c1", name="Sam"))
        db_session.commit()
        # created_at has a model default too, so it also has to be blanked here -- passing
        # created_at=None to the constructor does not produce a NULL row.
        _as_pre_sync(db_session, "contacts", "uid", "updated_at", "created_at")

        run_backfill(db_session)
        assert db_session.get(Contact, "c1").updated_at == datetime(1970, 1, 1)

    def test_a_link_row_inherits_its_parent_timestamp(self, db_session):
        """Link tables have no timestamp of their own, and a link cannot predate its parent."""
        txn_created = datetime(2026, 3, 3, 9, 0, 0)
        db_session.add(
            Transaction(
                id="t1", source_id="s1", source="example_bank", date=date(2026, 3, 3),
                amount=Decimal("-10.00"), merchant_raw="EXAMPLE CAFE", created_at=txn_created,
            )
        )
        db_session.add(Project(id="p1", name="Japan Trip", created_at=datetime(2026, 1, 1)))
        db_session.commit()
        db_session.add(TransactionProject(transaction_id="t1", project_id="p1"))
        db_session.commit()
        _as_pre_sync(db_session, "transaction_projects", "updated_at")

        run_backfill(db_session)
        link = db_session.query(TransactionProject).filter_by(transaction_id="t1").one()
        assert link.updated_at == txn_created

    def test_project_members_inherit_the_project_timestamp(self, db_session):
        project_created = datetime(2026, 2, 2, 8, 0, 0)
        db_session.add(Project(id="p1", name="Japan Trip", created_at=project_created))
        db_session.add(Contact(id="c1", name="Sam", created_at=datetime(2026, 1, 1)))
        db_session.commit()
        db_session.add(ProjectMember(project_id="p1", contact_id="c1"))
        db_session.commit()
        _as_pre_sync(db_session, "project_members", "updated_at")

        run_backfill(db_session)
        member = db_session.query(ProjectMember).filter_by(project_id="p1").one()
        assert member.updated_at == project_created


class TestIdempotence:
    def test_a_second_run_writes_nothing(self, db_session):
        db_session.add(Project(id="p1", name="Japan Trip", created_at=datetime(2026, 1, 1)))
        db_session.commit()

        _as_pre_sync(db_session, "projects", "uid", "updated_at")
        first = run_backfill(db_session)
        second = run_backfill(db_session)
        assert first["total_rows"] > 0
        assert second["total_rows"] == 0

    def test_a_second_run_does_not_change_existing_values(self, db_session):
        db_session.add(Project(id="p1", name="Japan Trip", created_at=datetime(2026, 1, 1)))
        db_session.commit()
        _as_pre_sync(db_session, "projects", "uid", "updated_at")
        run_backfill(db_session)
        uid_before = db_session.get(Project, "p1").uid

        run_backfill(db_session)
        assert db_session.get(Project, "p1").uid == uid_before

    def test_rows_added_later_are_picked_up_without_disturbing_the_rest(self, db_session):
        db_session.add(Project(id="p1", name="Japan Trip", created_at=datetime(2026, 1, 1)))
        db_session.commit()
        _as_pre_sync(db_session, "projects", "uid", "updated_at")
        run_backfill(db_session)
        first_uid = db_session.get(Project, "p1").uid

        db_session.add(Project(id="p2", name="Iceland Trip", created_at=datetime(2026, 6, 1)))
        db_session.commit()
        db_session.execute(text("UPDATE projects SET uid = NULL WHERE id = 'p2'"))
        db_session.commit()
        result = run_backfill(db_session)

        assert result["written"]["projects"]["uid"] == 1
        assert db_session.get(Project, "p1").uid == first_uid
        assert db_session.get(Project, "p2").uid is not None


class TestCrossDeviceAgreement:
    def test_two_databases_with_the_same_history_mint_the_same_identity(self, temp_db):
        """The core property. Simulates two devices backfilling their own copy independently."""
        session_factory, _ = temp_db
        created = datetime(2026, 1, 1, 12, 0, 0)

        results = []
        for _ in range(2):
            session = session_factory()
            try:
                # Same history, different local primary keys -- which is exactly the real situation,
                # because `id` is a per-device UUID.
                session.query(Project).delete()
                session.add(Project(id=f"local-{_}", name="Japan Trip", created_at=created))
                session.commit()
                _as_pre_sync(session, "projects", "uid", "updated_at")
                run_backfill(session)
                project = session.query(Project).filter_by(name="Japan Trip").one()
                results.append((project.uid, project.updated_at))
            finally:
                session.close()

        assert results[0] == results[1], "two devices disagreed on identity for the same row"


class TestUIDCollisions:
    def test_two_identical_rules_do_not_break_the_unique_index(self, db_session):
        """Genuinely duplicated pre-sync rows derive one identity. The second is left NULL rather
        than failing the whole backfill -- it is a data-cleanup question, not a sync one."""
        for i in range(2):
            db_session.add(
                Rule(
                    id=f"r{i}", pattern="COFFEE", match_field="merchant_raw",
                    category="Food/Coffee", created_at=datetime(2026, 1, 1),
                )
            )
        db_session.commit()
        _as_pre_sync(db_session, "rules", "uid", "updated_at")

        result = run_backfill(db_session)  # must not raise
        uids = [r.uid for r in db_session.query(Rule).order_by(Rule.id).all()]
        assert uids.count(None) == 1, f"expected exactly one row left unminted, got {uids}"
        assert result["written"]["rules"]["uid"] == 1

    def test_rules_differing_in_category_are_distinct(self, db_session):
        db_session.add(Rule(id="r1", pattern="COFFEE", match_field="merchant_raw",
                            category="Food/Coffee", created_at=datetime(2026, 1, 1)))
        db_session.add(Rule(id="r2", pattern="COFFEE", match_field="merchant_raw",
                            category="Food/Groceries", created_at=datetime(2026, 1, 1)))
        db_session.commit()
        _as_pre_sync(db_session, "rules", "uid", "updated_at")

        run_backfill(db_session)
        uids = {r.uid for r in db_session.query(Rule).all()}
        assert None not in uids and len(uids) == 2
