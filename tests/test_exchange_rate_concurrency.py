"""Cold-start rates cannot race or commit unrelated financial edits."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import io
import json
from threading import Barrier

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models import Base, ExchangeRate, Transaction
from src.services import exchange_rates


def test_simultaneous_cold_start_is_atomic_and_fetches_once(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path}/rates.db', connect_args={'timeout': 10})
    Base.metadata.create_all(engine)
    calls = []
    def response(*args, **kwargs):
        calls.append(1)
        return io.BytesIO(json.dumps({'result': 'success', 'rates': {'USD': 1, 'EUR': 0.9}}).encode())
    monkeypatch.setattr('urllib.request.urlopen', response)
    barrier = Barrier(8)
    def load(_):
        with sessionmaker(bind=engine)() as session:
            barrier.wait()
            exchange_rates.ensure_rates_fresh(session)
            assert session.query(ExchangeRate).count() == 9
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(load, range(8)))
    assert calls == [1]
    engine.dispose()


def test_refresh_does_not_commit_or_rollback_caller_financial_changes(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path}/rates.db')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: io.BytesIO(
        b'{"result":"success","rates":{"USD":1,"EUR":0.9}}'))
    with factory() as caller:
        caller.add(Transaction(source='Example Bank', source_id='unsaved', date=date(2026, 1, 1),
                               amount=-5, merchant_raw='EXAMPLE MERCHANT'))
        exchange_rates.ensure_rates_fresh(caller)
        with factory() as observer:
            assert observer.query(Transaction).count() == 0
        assert len(caller.new) == 1
        caller.rollback()
    engine.dispose()


def test_refresh_cache_is_scoped_to_database(tmp_path, monkeypatch):
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: io.BytesIO(
        b'{"result":"success","rates":{"USD":1,"EUR":0.9}}'))
    for name in ['one', 'two']:
        engine = create_engine(f'sqlite:///{tmp_path}/{name}.db')
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as session:
            exchange_rates.ensure_rates_fresh(session)
            assert session.query(ExchangeRate).count() == 9
        engine.dispose()
