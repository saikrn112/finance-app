import json
from datetime import date
from decimal import Decimal

from src.models import SyncLog, Transaction, Project, TransactionProject
from src.api.routes import sync
from src.ingestion.plaid_sync import apply_plaid_sync_batch


def _log(session):
    log = SyncLog(source='plaid', status='connected', plaid_cursor='original',
                  extra_data={'access_token': 'synthetic-token', 'institution_name': 'Example Bank'})
    session.add(log); session.commit()
    return log


def _packet(key, pending=False, pending_id=None):
    return dict(source_id=key, date=date(2026, 1, 1), amount=Decimal('-25'),
                merchant_raw='EXAMPLE CAFE', merchant_clean='Example Cafe', account_last4='1111',
                plaid_account_id='example-account', currency='USD', pending=pending,
                pending_transaction_id=pending_id)


def _stub(monkeypatch):
    monkeypatch.setattr(sync, 'record_plaid_usage', lambda *args, **kwargs: None)
    monkeypatch.setattr(sync, '_refresh_sidebar_snapshots', lambda *args, **kwargs: [])
    monkeypatch.setattr(sync, '_plaid_activity_destination', lambda log: 'transactions')


def test_pending_removal_and_posted_add_on_separate_pages_keep_user_work(db_session, monkeypatch):
    log = _log(db_session); _stub(monkeypatch)
    apply_plaid_sync_batch(db_session, 'Example Bank', [_packet('pending', pending=True)], [], [])
    row = db_session.query(Transaction).one(); row.notes = 'Synthetic note'
    project = Project(name='Example Trip'); db_session.add(project); db_session.flush()
    db_session.add(TransactionProject(transaction_id=row.id, project_id=project.id)); db_session.commit()
    pages = iter([
        dict(added=[], modified=[], removed=['pending'], cursor='page-one', has_more=True),
        dict(added=[_packet('posted', pending_id='pending')], modified=[], removed=[], cursor='final', has_more=False),
    ])
    monkeypatch.setattr(sync, 'sync_transactions', lambda *args: next(pages))
    result = sync._sync_plaid_locked(db_session)
    assert result['status'] == 'synced' and log.plaid_cursor == 'final'
    row = db_session.query(Transaction).one()
    assert row.source_id == 'posted' and row.notes == 'Synthetic note'
    assert db_session.query(TransactionProject).one().transaction_id == row.id


def test_pagination_mutation_restarts_original_cursor_without_partial_rows(db_session, monkeypatch):
    log = _log(db_session); _stub(monkeypatch)
    class Mutation(Exception):
        body = json.dumps({'error_code': 'TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION'})
    calls = []
    def page(token, cursor):
        calls.append(cursor)
        if len(calls) == 1:
            return dict(added=[_packet('discarded')], modified=[], removed=[], cursor='unstable', has_more=True)
        if len(calls) == 2:
            raise Mutation()
        return dict(added=[_packet('retained')], modified=[], removed=[], cursor='final', has_more=False)
    monkeypatch.setattr(sync, 'sync_transactions', page)
    result = sync._sync_plaid_locked(db_session)
    assert result['status'] == 'synced'
    assert calls == ['original', 'unstable', 'original']
    assert log.plaid_cursor == 'final'
    assert db_session.query(Transaction).one().source_id == 'retained'


def test_later_page_failure_does_not_commit_partial_financial_patch(db_session, monkeypatch):
    log = _log(db_session); _stub(monkeypatch)
    calls = []
    def page(token, cursor):
        calls.append(cursor)
        if len(calls) == 1:
            return dict(added=[_packet('partial')], modified=[], removed=[], cursor='unstable', has_more=True)
        raise RuntimeError('Synthetic provider failure')
    monkeypatch.setattr(sync, 'sync_transactions', page)
    result = sync._sync_plaid_locked(db_session)
    assert result['status'] == 'partial_error'
    assert log.plaid_cursor == 'original' and db_session.query(Transaction).count() == 0
