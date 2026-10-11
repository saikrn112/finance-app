"""Desired invariants using synthetic in-memory data only.

Run with the finance app source on PYTHONPATH and isolated FINANCE_APP_* paths.
These fail against the audited implementation, exposing behavior not covered by its suite.
No private statements, credentials, cloud writes, or production database access are used.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models import Base, Transaction
from src.ingestion.plaid_sync import apply_plaid_sync_batch
from src.sync.engine import run_sync, grant_merge_consent
from tests.sync_fakes import FakeTransport


def _db():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    grant_merge_consent(session)
    return session


def _packet(key, amount='-25', account='1111', currency='USD', pending=False, pending_id=None):
    return dict(source_id=key, date=date(2026, 1, 1), amount=Decimal(amount),
                merchant_raw='EXAMPLE CAFE', merchant_clean='Example Cafe',
                account_last4=account, plaid_account_id='example-' + account,
                currency=currency, pending=pending, pending_transaction_id=pending_id)


@pytest.mark.parametrize('change', [dict(account='2222'), dict(amount='25'), dict(currency='EUR')],
                         ids=['different-account', 'opposite-sign', 'different-currency'])
def test_valid_plaid_row_is_not_suppressed_by_unrelated_statement(change):
    session = _db()
    session.add(Transaction(source='Example Bank', source_id='statement',
                            date=date(2026, 1, 1), amount=Decimal('-25'),
                            merchant_raw='EXAMPLE CAFE', origin='statements',
                            account_last4='1111', currency='USD'))
    session.commit()
    counts = apply_plaid_sync_batch(session, 'Example Bank', [_packet('plaid', **change)], [], [])
    session.commit()
    assert counts.skipped_statement_overlap == 0
    assert session.query(Transaction).count() == 2
    session.close()


def test_pending_promotion_does_not_resurrect_old_identity_on_peer():
    a, b = _db(), _db()
    transport = FakeTransport()
    apply_plaid_sync_batch(a, 'Example Bank', [_packet('pending', pending=True)], [], [])
    a.commit()
    run_sync(a, transport, device_id='example-a')
    run_sync(b, transport, device_id='example-b')
    apply_plaid_sync_batch(a, 'Example Bank', [_packet('posted', pending_id='pending')], [], [])
    a.commit()
    run_sync(a, transport, device_id='example-a')
    run_sync(b, transport, device_id='example-b')
    run_sync(a, transport, device_id='example-a')
    assert a.query(Transaction).count() == 1
    assert b.query(Transaction).count() == 1
    assert a.query(Transaction).one().source_id == 'posted'
    a.close()
    b.close()


def test_provider_correction_reaches_existing_peer_row():
    a, b = _db(), _db()
    transport = FakeTransport()
    apply_plaid_sync_batch(a, 'Example Bank', [_packet('modified')], [], [])
    a.commit()
    run_sync(a, transport, device_id='example-a')
    run_sync(b, transport, device_id='example-b')
    apply_plaid_sync_batch(a, 'Example Bank', [], [_packet('modified', amount='-30')], [])
    a.commit()
    run_sync(a, transport, device_id='example-a')
    run_sync(b, transport, device_id='example-b')
    assert a.query(Transaction).one()._amount == b.query(Transaction).one()._amount
    a.close()
    b.close()


def test_one_statement_row_does_not_suppress_multiple_real_repeated_charges():
    session = _db()
    session.add(Transaction(source='Example Bank', source_id='statement',
                            date=date(2026, 1, 1), amount=Decimal('-25'),
                            merchant_raw='EXAMPLE CAFE', origin='statements',
                            account_last4='1111', currency='USD'))
    session.commit()
    counts = apply_plaid_sync_batch(session, 'Example Bank',
                                    [_packet('posted-1'), _packet('posted-2')], [], [])
    session.commit()
    assert counts.skipped_statement_overlap == 1
    assert session.query(Transaction).count() == 2
    session.close()


def test_pending_transition_preserves_peer_manual_edits_and_links():
    from src.models import Project, Contact, TransactionProject, TransactionSplit
    a, b = _db(), _db()
    transport = FakeTransport()
    apply_plaid_sync_batch(a, 'Example Bank', [_packet('pending', pending=True)], [], [])
    a.commit()
    run_sync(a, transport, device_id='example-a')
    run_sync(b, transport, device_id='example-b')
    old = b.query(Transaction).one()
    old.category, old.category_source, old.notes = 'Food/Custom', 'user', 'Synthetic user note'
    project, contact = Project(name='Example Trip'), Contact(name='Example Person')
    b.add_all([project, contact]); b.flush()
    b.add(TransactionProject(transaction_id=old.id, project_id=project.id, description='Synthetic project note'))
    b.add(TransactionSplit(transaction_id=old.id, contact_id=contact.id, share_amount=Decimal('7.50')))
    b.commit()
    # The remote user edits have not been published when the provider promotes on A.
    apply_plaid_sync_batch(a, 'Example Bank', [_packet('posted', pending_id='pending')], [], [])
    a.commit()
    run_sync(a, transport, device_id='example-a')
    run_sync(b, transport, device_id='example-b')
    run_sync(a, transport, device_id='example-a')
    for session in [a, b]:
        row = session.query(Transaction).one()
        assert row.source_id == 'posted'
        assert row.category == 'Food/Custom' and row.category_source == 'user'
        assert row.notes == 'Synthetic user note'
        assert session.query(TransactionProject).one().transaction_id == row.id
        assert session.query(TransactionSplit).one().transaction_id == row.id
        assert session.query(TransactionSplit).one()._share_amount == Decimal('7.50')
        session.close()


def test_unobserved_peer_money_edit_is_reported_and_cannot_overwrite_facts():
    from src.sync.payload import build_payload
    from src.sync.merge import merge_payload
    a, b = _db(), _db()
    transport = FakeTransport()
    apply_plaid_sync_batch(a, 'Example Bank', [_packet('unchanged')], [], [])
    a.commit()
    run_sync(a, transport, device_id='example-a')
    run_sync(b, transport, device_id='example-b')
    # A direct edit does not mint a provider observation/revision.
    b.query(Transaction).one().amount = Decimal('-999')
    b.commit()
    report = merge_payload(a, build_payload(b, device_id='example-b'))
    assert a.query(Transaction).one()._amount == Decimal('-25')
    assert report.fact_conflicts == {'transactions': 1}
    a.close(); b.close()


def test_divergent_provider_revisions_are_explicit_conflicts():
    from src.sync.payload import build_payload
    from src.sync.merge import merge_payload
    a, b = _db(), _db()
    transport = FakeTransport()
    apply_plaid_sync_batch(a, 'Example Bank', [_packet('fork')], [], [])
    a.commit()
    run_sync(a, transport, device_id='example-a'); run_sync(b, transport, device_id='example-b')
    apply_plaid_sync_batch(a, 'Example Bank', [], [_packet('fork', amount='-30')], []); a.commit()
    apply_plaid_sync_batch(b, 'Example Bank', [], [_packet('fork', amount='-35')], []); b.commit()
    assert merge_payload(a, build_payload(b, device_id='example-b')).fact_conflicts == {'transactions': 1}
    assert merge_payload(b, build_payload(a, device_id='example-a')).fact_conflicts == {'transactions': 1}
    assert a.query(Transaction).one()._amount == Decimal('-30')
    assert b.query(Transaction).one()._amount == Decimal('-35')
    a.close(); b.close()


def test_statement_claim_survives_replay_and_peer_plaid_copy():
    from src.models import TransactionProject, Project
    a, b = _db(), _db()
    transport = FakeTransport()
    statement = Transaction(source='Example Bank', source_id='statement', date=date(2026, 1, 1),
                            amount=Decimal('-25'), merchant_raw='EXAMPLE CAFE', origin='statements',
                            account_last4='1111', currency='USD')
    a.add(statement); a.commit()
    apply_plaid_sync_batch(b, 'Example Bank', [_packet('posted')], [], []); b.commit()
    row = b.query(Transaction).one()
    row.notes = 'Keep this synthetic note'
    project = Project(name='Example Project'); b.add(project); b.flush()
    b.add(TransactionProject(transaction_id=row.id, project_id=project.id)); b.commit()
    apply_plaid_sync_batch(a, 'Example Bank', [_packet('posted')], [], []); a.commit()
    run_sync(a, transport, device_id='example-a'); run_sync(b, transport, device_id='example-b')
    run_sync(a, transport, device_id='example-a')
    assert a.query(Transaction).one().source_id == b.query(Transaction).one().source_id == 'statement'
    assert b.query(Transaction).one().notes == 'Keep this synthetic note'
    assert b.query(TransactionProject).one().transaction_id == b.query(Transaction).one().id
    replay = apply_plaid_sync_batch(a, 'Example Bank', [_packet('posted')], [], []); a.commit()
    assert replay.skipped_statement_overlap == 1 and a.query(Transaction).count() == 1
    a.close(); b.close()
