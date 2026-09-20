"""Every persisted table the app reads travels, and survives a round trip intact.

This is the rule the schema now follows: if a row is persisted and read by the app, it syncs. The
alternative — sync only observed facts and recompute the derived ones per device — fails here
because two of the three clients cannot ingest. An iOS app runs neither Plaid nor PDF imports, so if
`source_balance_history` and `investment_period_facts` did not travel it would show a wrong net
worth until every derivation was reimplemented in Swift.

Derived rows are therefore `FACT`s: keyed by a natural key, insert-if-unseen, never
last-write-wins. Two devices deriving the same row produce identical values under the same key, so
the second is simply already present.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models import (
    AccountActivity,
    AccountSnapshot,
    InvestmentHoldingSnapshot,
    InvestmentPeriodFact,
    Payslip,
    PayslipLineItem,
    RetirementStatement,
    RetirementTransaction,
    SourceBalanceHistory,
)
from src.models.database import Base
from src.sync import refs, schema
from src.sync.engine import grant_merge_consent, run_sync
from src.sync.payload import FORBIDDEN_TABLES, build_payload, find_unsyncable
from src.sync.merge import merge_payload
from tests.sync_fakes import FakeTransport


FACT_TABLES = (
    "payslips",
    "payslip_line_items",
    "account_snapshots",
    "investment_holding_snapshots",
    "source_balance_history",
    "investment_period_facts",
    "account_activity",
    "retirement_transactions",
    "retirement_statements",
)


def _database(tmp_path, name):
    engine = create_engine(f"sqlite:///{tmp_path}/{name}.db")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _seed_facts(db):
    """One row in each newly-synced table, with every NOT NULL column populated."""
    payslip = Payslip(
        source="acme_payroll", employer="Acme", pay_date=date(2026, 9, 15),
        pay_period_start=date(2026, 9, 1), pay_period_end=date(2026, 9, 15),
        signature="sig-abc", currency="USD", filename="sept.pdf",
    )
    payslip.gross = Decimal("5000.00")
    payslip.net = Decimal("3800.00")
    payslip.total_taxes = Decimal("900.00")
    payslip.total_deductions = Decimal("300.00")
    db.add(payslip)
    db.flush()

    line = PayslipLineItem(payslip_id=payslip.id, section="taxes", label="Federal")
    line.amount = Decimal("700.00")
    line.ytd = Decimal("6300.00")  # guarded setter, added with the other four
    db.add(line)

    snap = AccountSnapshot(
        source="Example Bank", account_key="acct-1", account_name="Everyday",
        account_group="bank_account", connection_state="plaid", currency="USD",
        synced_at=datetime(2026, 9, 15, 10, 0, 0),
    )
    snap.current_value = Decimal("1234.56")
    db.add(snap)

    holding = InvestmentHoldingSnapshot(
        source="Example Brokerage", plaid_account_id="inv-1", account_name="Brokerage",
        security_id="sec-1", ticker="EXMP", name="Example Fund",
        quantity=Decimal("10.5"), price=Decimal("100.25"), currency="USD", type="equity",
        synced_at=datetime(2026, 9, 15, 10, 0, 0),
    )
    holding.value = Decimal("1052.63")
    holding.cost_basis = Decimal("900.00")
    db.add(holding)

    balance = SourceBalanceHistory(
        source_key="example_bank", account_key="acct-1", account_name="Everyday",
        source="Example Bank", account_group="bank_account", date=date(2026, 9, 15),
        currency="USD", provenance="account_snapshot",
    )
    balance.value = Decimal("1234.56")
    db.add(balance)

    period = InvestmentPeriodFact(
        source_key="example_brokerage", source="Example Brokerage",
        period_start=date(2026, 8, 31), period_end=date(2026, 9, 15),
        currency="USD", provenance="plaid_snapshot_activity",
    )
    period.beginning_value = Decimal("1000.00")
    period.ending_value = Decimal("1052.63")
    period.inflow = Decimal("25.00")
    period.market_gain = Decimal("27.63")
    db.add(period)

    activity = AccountActivity(
        source_id="act-1", source_key="example_brokerage", source="Example Brokerage",
        account_id="inv-1", account_last4="1234", date=date(2026, 9, 14),
        description="DIVIDEND", merchant=None, activity_type="dividend", currency="USD",
    )
    activity.amount = Decimal("12.34")
    db.add(activity)

    retirement = RetirementTransaction(
        source="example_retirement", source_id="ret-1", date=date(2026, 9, 1),
        type="Employee Pre-Tax", contribution_source="Employee", fund="Target Fund",
        currency="USD", units=Decimal("7.8142"), unit_price=Decimal("138.22"),
    )
    retirement.amount = Decimal("1080.54")
    db.add(retirement)

    statement = RetirementStatement(
        source="example_retirement", plan_name="Plan", period_start=date(2026, 7, 1),
        period_end=date(2026, 9, 30), currency="USD", rate_of_return=Decimal("2.5000"),
    )
    # Money columns are private and guarded; assigning the public name goes through the setter.
    statement.beginning_balance = Decimal("10000.00")
    statement.ending_balance = Decimal("11000.00")
    statement.employee_contributions = Decimal("500.00")
    statement.employer_contributions = Decimal("250.00")
    statement.market_change = Decimal("250.00")
    statement.vested_balance = Decimal("10500.00")
    db.add(statement)
    db.commit()


def _counts(db):
    return {spec.name: db.query(schema.model_for(spec)).count() for spec in schema.TABLES}


# --- the rule itself -----------------------------------------------------------------------------


class TestCoverage:
    def test_the_previously_unsynced_tables_all_have_specs_now(self):
        named = {spec.name for spec in schema.TABLES}
        for table in FACT_TABLES:
            assert table in named, f"{table} still does not sync"

    @pytest.mark.parametrize("spec", schema.TABLES, ids=lambda s: s.name)
    def test_every_required_column_travels(self, spec):
        """A NOT NULL column left out of a spec makes the peer's insert fail the constraint."""
        model = schema.model_for(spec)
        carried = {f.name for f in spec.all_fields} | {f.attribute for f in spec.all_fields}
        # Link rows get their parent foreign keys from the reference, not from the wire.
        parent_keys = {"payslip_id", "transaction_id", "project_id", "contact_id"}
        missing = [
            c.name
            for c in model.__table__.columns
            if not c.primary_key and not c.nullable and c.default is None
            and c.name not in carried and c.name not in parent_keys
        ]
        assert missing == [], f"{spec.name} would insert NULL into {missing}"

    @pytest.mark.parametrize("spec", schema.TABLES, ids=lambda s: s.name)
    def test_every_field_names_a_real_mapped_column(self, spec):
        """`attr` must point at the mapped attribute, not the blocked public name.

        Money columns are private (`_ending_value`) with a property that raises on read, so a spec
        that names the public form reads nothing and writes nowhere. That is how
        retirement_statements shipped a spec whose seven money fields all silently missed.
        """
        from sqlalchemy import inspect as sa_inspect

        mapped = {a.key for a in sa_inspect(schema.model_for(spec)).attrs}
        wrong = [f.name for f in spec.all_fields if f.attribute not in mapped]
        assert wrong == [], f"{spec.name}: fields not mapped to a column: {wrong}"

    @pytest.mark.parametrize("spec", schema.TABLES, ids=lambda s: s.name)
    def test_every_spec_is_nameable(self, spec):
        """Either own-column `ref_attrs`, or a link resolver keyed on its kind."""
        from src.sync import merge

        has_resolver = spec.kind in merge._LINK_KINDS if hasattr(merge, "_LINK_KINDS") else True
        assert spec.ref_attrs or has_resolver, f"{spec.name} can be neither named nor found"

    def test_device_local_tables_stay_out(self):
        assert "plaid_product_enrollments" in FORBIDDEN_TABLES, (
            "derived from sync_log, which is excluded, so it is device-local"
        )
        named = {spec.name for spec in schema.TABLES}
        assert not (named & set(FORBIDDEN_TABLES)), "a table cannot be both synced and forbidden"


# --- round trip ----------------------------------------------------------------------------------


class TestRoundTrip:
    def test_every_fact_table_reaches_a_second_device(self, tmp_path):
        source = _database(tmp_path, "a")()
        target = _database(tmp_path, "b")()
        try:
            _seed_facts(source)
            grant_merge_consent(target)

            payload = json.loads(json.dumps(build_payload(source, device_id="a"), default=str))
            merge_payload(target, payload)

            before, after = _counts(source), _counts(target)
            for table in FACT_TABLES:
                assert after[table] == before[table] == 1, (
                    f"{table}: {before[table]} on the source, {after[table]} after merging"
                )
        finally:
            source.close()
            target.close()

    def test_money_arrives_exact(self, tmp_path):
        """Amounts travel as decimal strings; a float round trip would drift."""
        source = _database(tmp_path, "a")()
        target = _database(tmp_path, "b")()
        try:
            _seed_facts(source)
            grant_merge_consent(target)
            payload = json.loads(json.dumps(build_payload(source, device_id="a"), default=str))
            merge_payload(target, payload)

            assert target.query(Payslip).one()._gross == Decimal("5000.00")
            assert target.query(AccountSnapshot).one()._current_value == Decimal("1234.56")
            assert target.query(SourceBalanceHistory).one()._value == Decimal("1234.56")
            assert target.query(InvestmentPeriodFact).one()._inflow == Decimal("25.00")
            assert target.query(AccountActivity).one()._amount == Decimal("12.34")
            assert target.query(RetirementTransaction).one()._amount == Decimal("1080.54")
        finally:
            source.close()
            target.close()

    def test_merging_twice_inserts_once(self, tmp_path):
        """Derived rows are facts, so a second delivery is recognised, not duplicated or fought over."""
        source = _database(tmp_path, "a")()
        target = _database(tmp_path, "b")()
        try:
            _seed_facts(source)
            grant_merge_consent(target)
            payload = json.loads(json.dumps(build_payload(source, device_id="a"), default=str))

            merge_payload(target, payload)
            first = _counts(target)
            merge_payload(target, payload)

            assert _counts(target) == first
        finally:
            source.close()
            target.close()

    def test_the_line_item_lands_under_its_own_payslip(self, tmp_path):
        """Its payslip_id is a local UUID, so it must be re-resolved against the peer's row."""
        source = _database(tmp_path, "a")()
        target = _database(tmp_path, "b")()
        try:
            _seed_facts(source)
            grant_merge_consent(target)
            payload = json.loads(json.dumps(build_payload(source, device_id="a"), default=str))
            merge_payload(target, payload)

            payslip = target.query(Payslip).one()
            line = target.query(PayslipLineItem).one()
            assert line.payslip_id == payslip.id
            assert line.payslip_id != source.query(PayslipLineItem).one().payslip_id, (
                "a local id was copied across instead of being resolved"
            )
        finally:
            source.close()
            target.close()

    def test_nothing_is_unnameable(self, tmp_path):
        source = _database(tmp_path, "a")()
        try:
            _seed_facts(source)
            assert find_unsyncable(source) == {}
        finally:
            source.close()

    def test_two_devices_converge_through_a_transport(self, tmp_path):
        a_factory, b_factory = _database(tmp_path, "ta"), _database(tmp_path, "tb")
        transport = FakeTransport()
        a, b = a_factory(), b_factory()
        try:
            _seed_facts(a)
            grant_merge_consent(a)
            grant_merge_consent(b)

            run_sync(a, transport, device_id="a")
            run_sync(b, transport, device_id="b")
            run_sync(a, transport, device_id="a")

            for table in FACT_TABLES:
                assert _counts(b)[table] == 1, f"{table} never reached the peer"
        finally:
            a.close()
            b.close()
