from datetime import date
from decimal import Decimal

from src.ingestion.import_service import preview_import, commit_import
from src.models import SyncLog, Transaction


def test_repeated_statement_transactions_both_import(db_session):
    """Two legitimate purchases at the same merchant for the same amount on nearby
    dates (within 3-day heuristic window) must both import when there is no
    Plaid row present — they are distinct transactions, not duplicates."""
    content = (
        b"Transaction Date,Amount,Description\n"
        b"02/10/2026,-5.25,EXAMPLE COFFEE 12345\n"
        b"02/12/2026,-5.25,EXAMPLE COFFEE 12345\n"
    )
    preview = preview_import(
        db_session,
        file_bytes=content,
        filename="example_card.csv",
        source="example_card",
        kind="csv",
    )
    result = commit_import(db_session, preview["import_id"])

    rows = db_session.query(Transaction).filter(Transaction.source == "Example Card").all()
    assert result["imported"] == 2
    assert result["skipped"] == 0
    assert len(rows) == 2


def test_reimport_same_file_is_idempotent(db_session):
    """Re-importing the same file (identical file hash) returns already_imported
    without inserting any new rows."""
    content = b"Transaction Date,Amount,Description\n02/15/2026,-18.00,PACIFIC COFFEE\n"
    preview1 = preview_import(
        db_session,
        file_bytes=content,
        filename="example_card.csv",
        source="example_card",
        kind="csv",
    )
    commit_import(db_session, preview1["import_id"])

    preview2 = preview_import(
        db_session,
        file_bytes=content,
        filename="example_card.csv",
        source="example_card",
        kind="csv",
    )
    result2 = commit_import(db_session, preview2["import_id"])

    rows = db_session.query(Transaction).filter(Transaction.source == "Example Card").all()
    assert result2["status"] == "already_imported"
    assert result2["imported"] == 0
    assert len(rows) == 1


def test_statement_vs_plaid_heuristic_dedup_still_works(db_session, monkeypatch):
    from src.plugins.registry import get_csv_config
    monkeypatch.setattr(get_csv_config("example_card"), "account_col", "Account")
    """A statement row that matches an existing Plaid row by heuristic (same source,
    nearby date, same signed amount and currency/account identity, similar merchant) must still be suppressed."""
    db_session.add(
        Transaction(
            source="Example Card",
            source_id="plaid-example_card-1",
            origin="plaid",
            account_last4="1001",
            currency="USD",
            date=date(2026, 2, 16),
            amount=Decimal("-23.47"),
            merchant_raw="MOLLY TEA SUNNYVALE",
            merchant_clean="Molly Tea",
            category="Dining",
            category_source="rule",
        )
    )
    db_session.commit()

    content = b"Transaction Date,Amount,Description,Account\n02/15/2026,-23.47,MOLLY TEA SUNNYVALE,1001\n"
    preview = preview_import(
        db_session,
        file_bytes=content,
        filename="example_card.csv",
        source="example_card",
        kind="csv",
    )
    result = commit_import(db_session, preview["import_id"])

    assert result["imported"] == 0
    assert result["skipped"] == 1


def test_statement_preview_matches_authorized_date_before_nearby_posted_date(db_session):
    """A later statement purchase should claim a Plaid row whose authorized date matches it."""
    from src.ingestion.import_service import NormalizedImportTransaction, _preview_duplicates

    db_session.add(Transaction(
        source="Example Card",
        source_id="plaid-example-sports",
        origin="plaid",
        account_last4="4242",
        currency="USD",
        date=date(2026, 4, 19),
        authorized_date=date(2026, 4, 18),
        amount=Decimal("-25.00"),
        merchant_raw="EXAMPLE SPORTS",
    ))
    db_session.commit()
    statement_rows = [
        NormalizedImportTransaction("Example Card", "stmt-17", date(2026, 4, 17), -25.0,
                                    "SQ *EXAMPLE SPORTS ANYTOWN", "4242", origin="statements"),
        NormalizedImportTransaction("Example Card", "stmt-18", date(2026, 4, 18), -25.0,
                                    "SQ *EXAMPLE SPORTS ANYTOWN", "4242", origin="statements"),
    ]

    preview = _preview_duplicates(
        db_session, statement_rows, existing_log=None, total_records=2,
        record_type="transactions", payload=None, payloads=None,
        source="Example Card", kind="statement_pdf",
    )

    assert preview["items"][0]["duplicate_reason"] is None
    assert preview["items"][1]["duplicate_reason"]["existing_date"] == "2026-04-19"


def test_investment_pdf_matches_prior_csv_by_account_date_and_value(db_session):
    from src.ingestion.import_service import _preview_investment_duplicates

    db_session.add(SyncLog(
        source="Example Health Account",
        sync_type="import_investment_csv",
        status="success",
        extra_data={"payload": {
            "account": {"account_id": "123452468", "statement_date": "2026-02-28"},
            "summary": {"ending_value": 125.50},
        }},
    ))
    db_session.commit()

    preview = _preview_investment_duplicates(
        db_session,
        {"account": {"account_id": "2468", "statement_date": "2026-02-28", "source_id": "pdf-version"},
         "summary": {"ending_value": 125.50}, "holdings": []},
        "Example Health Account", "investment_statement_pdf",
    )

    assert preview["duplicate_count"] == 1
    assert preview["importable_count"] == 0
    assert preview["items"][0]["duplicate_reason"]["type"] == "investment_statement"


def test_preview_import_marks_cross_source_duplicates(db_session, monkeypatch):
    from src.plugins.registry import get_csv_config
    monkeypatch.setattr(get_csv_config("example_charge_card"), "account_col", "Account")
    db_session.add(
        Transaction(
            source="Example Charge Card",
            source_id="plaid-1",
            origin="plaid",
            account_last4="1001",
            currency="USD",
            date=date(2026, 2, 8),
            amount=Decimal("-132.65"),
            merchant_raw="Example Store",
            merchant_clean="Example Store",
            category="Groceries",
            category_source="rule",
        )
    )
    db_session.commit()

    content = b"Date,Amount,Description,Account\n02/07/2026,132.65,EXAMPLE STORE 1234 ANYTOWN CA,1001\n"
    preview = preview_import(
        db_session,
        file_bytes=content,
        filename="example_charge_card.csv",
        source="example_charge_card",
        kind="csv",
    )

    assert preview["duplicate_summary"]["duplicate_count"] == 1
    assert preview["duplicate_summary"]["items"][0]["duplicate_reason"]["type"] == "heuristic"


def test_preview_import_marks_legacy_statement_duplicates(db_session):
    db_session.add(
        Transaction(
            source="Example Card",
            source_id="Example Card_2024-08-20_16.0_0",
            origin="statements",
            date=date(2024, 8, 20),
            amount=Decimal("-16.00"),
            merchant_raw="DD DOORDASH CAVA 855-973-1040 CA",
            merchant_clean="Cava",
            category="Dining",
            category_source="rule",
        )
    )
    db_session.commit()

    content = b"Transaction Date,Amount,Description\n08/20/2024,-16.00,DD DOORDASH CAVA 855-973-1040 CA\n"
    preview = preview_import(
        db_session,
        file_bytes=content,
        filename="example_card.csv",
        source="example_card",
        kind="csv",
    )

    assert preview["duplicate_summary"]["duplicate_count"] == 1
    assert preview["duplicate_summary"]["items"][0]["duplicate_reason"]["type"] == "legacy_statement"


def test_commit_import_skips_duplicates_and_inserts_new_rows(db_session, monkeypatch):
    from src.plugins.registry import get_csv_config
    monkeypatch.setattr(get_csv_config("example_card"), "account_col", "Account")
    db_session.add(
        Transaction(
            source="Example Card",
            source_id="existing",
            origin="plaid",
            account_last4="1001",
            currency="USD",
            date=date(2026, 2, 16),
            amount=Decimal("-23.47"),
            merchant_raw="MOLLY TEA SUNNYVALE",
            merchant_clean="Molly Tea",
            category="Dining",
            category_source="rule",
        )
    )
    db_session.commit()

    content = b"Transaction Date,Amount,Description,Account\n02/15/2026,-23.47,MOLLY TEA SUNNYVALE,1001\n02/17/2026,-18.00,PACIFIC COFFEE,1001\n"
    preview = preview_import(
        db_session,
        file_bytes=content,
        filename="example_card.csv",
        source="example_card",
        kind="csv",
    )
    result = commit_import(db_session, preview["import_id"])

    rows = db_session.query(Transaction).filter(Transaction.source == "Example Card").all()
    assert result["imported"] == 1
    assert result["skipped"] == 1
    assert len(rows) == 2


def test_preview_marks_reimported_file_as_duplicate(db_session):
    content = b"Transaction Date,Amount,Description\n02/15/2026,-18.00,PACIFIC COFFEE\n"
    first = preview_import(
        db_session,
        file_bytes=content,
        filename="example_card.csv",
        source="example_card",
        kind="csv",
    )
    commit_import(db_session, first["import_id"])

    second = preview_import(
        db_session,
        file_bytes=content,
        filename="example_card.csv",
        source="example_card",
        kind="csv",
    )

    assert second["already_imported"] is True
    assert second["duplicate_summary"]["importable_count"] == 0
    assert second["duplicate_summary"]["duplicate_count"] == 1
    assert second["duplicate_summary"]["items"][0]["duplicate_reason"]["type"] == "file_hash"


def test_commit_payslip_import_persists_payload(db_session, monkeypatch):
    monkeypatch.setattr(
        "src.ingestion.import_service._payslip_parser",
        lambda source: lambda path: {
            "employer": "Acme Corp",
            "pay_date": "02/14/2026",
            "gross": 6022.40,
            "net": 3856.52,
            "total_taxes": 1554.18,
            "total_deductions": 611.70,
            "taxes": {"federal": 790.10},
            "deductions": {"401k": 233.40},
            "earnings": {"regular": 6022.40},
            "benefits": {},
        },
    )

    preview = preview_import(
        db_session,
        file_bytes=b"%PDF-1.4 fake",
        filename="acme-paystub.pdf",
        source="acme_payroll",
        kind="payslip_pdf",
    )
    result = commit_import(db_session, preview["import_id"])
    log = db_session.query(SyncLog).filter(SyncLog.sync_type == "import_payslip_pdf").one()

    assert preview["record_type"] == "payslip"
    assert preview["payload"]["employer"] == "Acme Corp"
    assert result["imported"] == 1
    assert log.extra_data["payload"]["pay_date"] == "02/14/2026"


def test_payslip_preview_marks_semantic_duplicates(db_session, monkeypatch):
    monkeypatch.setattr(
        "src.ingestion.import_service._payslip_parser",
        lambda source: lambda path: {
            "employer": "Acme Corp",
            "pay_date": "02/14/2026",
            "gross": 6022.40,
            "net": 3856.52,
            "total_taxes": 1554.18,
            "total_deductions": 611.70,
            "taxes": {"federal": 790.10},
            "deductions": {"401k": 233.40},
            "earnings": {"regular": 6022.40},
            "benefits": {},
        },
    )

    first = preview_import(
        db_session,
        file_bytes=b"%PDF-1.4 first",
        filename="acme-paystub-v1.pdf",
        source="acme_payroll",
        kind="payslip_pdf",
    )
    commit_import(db_session, first["import_id"])

    second = preview_import(
        db_session,
        file_bytes=b"%PDF-1.4 second",
        filename="acme-paystub-v2.pdf",
        source="acme_payroll",
        kind="payslip_pdf",
    )
    result = commit_import(db_session, second["import_id"])

    assert second["already_imported"] is False
    assert second["duplicate_summary"]["duplicate_count"] == 1
    assert second["duplicate_summary"]["importable_count"] == 0
    assert second["duplicate_summary"]["items"][0]["duplicate_reason"]["type"] == "payslip_signature"
    assert result["status"] == "already_imported"
    assert db_session.query(SyncLog).filter(SyncLog.sync_type == "import_payslip_pdf").count() == 1


def test_payslip_preview_marks_legacy_payslip_duplicates(db_session, monkeypatch):
    monkeypatch.setattr(
        "src.ingestion.import_service._payslip_parser",
        lambda source: lambda path: {
            "employer": "Acme Corp",
            "pay_date": "02/14/2026",
            "gross": 6022.40,
            "net": 3856.52,
            "total_taxes": 1554.18,
            "total_deductions": 611.70,
            "taxes": {"federal": 790.10},
            "deductions": {"401k": 233.40},
            "earnings": {"regular": 6022.40},
            "benefits": {},
        },
    )
    monkeypatch.setattr(
        "src.ingestion.import_service.parse_all_payslips",
        lambda: [
            {
                "employer": "Acme Corp",
                "pay_date": "02/14/2026",
                "gross": 6022.40,
                "net": 3856.52,
                "total_taxes": 1554.18,
                "total_deductions": 611.70,
            }
        ],
    )

    preview = preview_import(
        db_session,
        file_bytes=b"%PDF-1.4 legacy",
        filename="acme-paystub.pdf",
        source="acme_payroll",
        kind="payslip_pdf",
    )

    assert preview["duplicate_summary"]["duplicate_count"] == 1
    assert preview["duplicate_summary"]["importable_count"] == 0
    assert preview["duplicate_summary"]["items"][0]["duplicate_reason"]["type"] == "payslip_signature"
    assert preview["duplicate_summary"]["items"][0]["duplicate_reason"]["existing_origin"] == "legacy_payslips"


def test_commit_retirement_import_persists_payload(db_session):
    content = (
        b"Date,Transaction Type,Source,Fund Name,Unit Count,Unit Value,Transaction Amount\n"
        b"02/14/26,Employee Pre-Tax,Employee,Example Target Fund,7.8142,138.22,1080.54\n"
        b"02/14/26,Employer Match,Employer Match,Example Target Fund,3.9071,138.22,540.27\n"
    )

    preview = preview_import(
        db_session,
        file_bytes=content,
        filename="example_retirement.csv",
        source="example_retirement",
        kind="retirement_csv",
    )
    result = commit_import(db_session, preview["import_id"])
    log = db_session.query(SyncLog).filter(SyncLog.sync_type == "import_retirement_csv").one()

    assert preview["record_type"] == "retirement"
    assert preview["duplicate_summary"]["total_transactions"] == 2
    assert preview["payload"]["summary"]["balance"] == 1620.12
    assert result["imported"] == 2
    assert len(log.extra_data["payload"]["transactions"]) == 2


def test_retirement_preview_and_commit_skip_existing_rows(db_session):
    first_content = (
        b"Date,Transaction Type,Source,Fund Name,Unit Count,Unit Value,Transaction Amount\n"
        b"02/14/26,Employee Pre-Tax,Employee,Example Target Fund,7.8142,138.22,1080.54\n"
        b"02/14/26,Employer Match,Employer Match,Example Target Fund,3.9071,138.22,540.27\n"
    )
    first = preview_import(
        db_session,
        file_bytes=first_content,
        filename="example_retirement-1.csv",
        source="example_retirement",
        kind="retirement_csv",
    )
    commit_import(db_session, first["import_id"])

    second_content = (
        b"Date,Transaction Type,Source,Fund Name,Unit Count,Unit Value,Transaction Amount\n"
        b"02/14/26,Employee Pre-Tax,Employee,Example Target Fund,7.8142,138.22,1080.54\n"
        b"02/14/26,Employer Match,Employer Match,Example Target Fund,3.9071,138.22,540.27\n"
        b"02/28/26,Employee Pre-Tax,Employee,Example Target Fund,8.0000,140.00,1120.00\n"
    )
    second = preview_import(
        db_session,
        file_bytes=second_content,
        filename="example_retirement-2.csv",
        source="example_retirement",
        kind="retirement_csv",
    )
    result = commit_import(db_session, second["import_id"])
    logs = db_session.query(SyncLog).filter(SyncLog.sync_type == "import_retirement_csv").order_by(SyncLog.created_at.asc()).all()

    assert second["already_imported"] is False
    assert second["duplicate_summary"]["duplicate_count"] == 2
    assert second["duplicate_summary"]["importable_count"] == 1
    assert result["status"] == "success"
    assert result["imported"] == 1
    assert result["skipped"] == 2
    assert len(logs) == 2
    assert len(logs[-1].extra_data["payload"]["transactions"]) == 1
    assert logs[-1].extra_data["payload"]["transactions"][0]["date"] == "2026-02-28"


def test_card_statement_credit_snapshot_uses_statement_date_and_asset_sign(db_session, monkeypatch):
    from src.ingestion import import_service
    from src.models import AccountSnapshot, SourceBalanceHistory
    monkeypatch.setattr(import_service, '_statement_parser', lambda _: lambda path: {
        'source': 'Example Card', 'account_last4': '4321',
        'beginning_balance': 0, 'ending_balance': -25,
        'end_date': '2026-02-12',
        'transactions': [{'date': '2026-02-10', 'amount': -25, 'description': 'Example refund'}],
    })
    preview = preview_import(db_session, file_bytes=b'synthetic credit statement', filename='example.pdf', source='example_card', kind='statement_pdf')
    commit_import(db_session, preview['import_id'])
    snapshot = db_session.query(AccountSnapshot).one()
    history = db_session.query(SourceBalanceHistory).one()
    assert snapshot._current_value == 25
    assert snapshot.synced_at.date() == date(2026, 2, 12)
    assert history._value == 25
    assert history.date == date(2026, 2, 12)


def test_statement_without_closing_date_does_not_invent_snapshot(db_session, monkeypatch):
    from src.ingestion import import_service
    from src.models import AccountSnapshot, SourceBalanceHistory
    monkeypatch.setattr(import_service, '_statement_parser', lambda _: lambda path: {
        'source': 'Example Card', 'account_last4': '4321', 'ending_balance': 0,
        'transactions': [{'date': '2026-02-10', 'amount': 25, 'description': 'Example purchase'}],
    })
    preview = preview_import(db_session, file_bytes=b'example activity only', filename='example.pdf', source='example_card', kind='statement_pdf')
    result = commit_import(db_session, preview['import_id'])
    assert result['imported'] == 1
    assert db_session.query(AccountSnapshot).count() == 0
    assert db_session.query(SourceBalanceHistory).count() == 0


def test_same_day_statements_keep_separate_account_balances(db_session, monkeypatch):
    from src.ingestion import import_service
    from src.models import SourceBalanceHistory
    for mask, balance in [('1111', 25), ('2222', 50)]:
        monkeypatch.setattr(import_service, '_statement_parser', lambda _, mask=mask, balance=balance: lambda path: {
            'source': 'Example Card', 'account_last4': mask, 'ending_balance': balance,
            'end_date': '2026-02-12',
            'transactions': [{'date': '2026-02-10', 'amount': balance, 'description': 'Example purchase'}],
        })
        preview = preview_import(db_session, file_bytes=('synthetic '+mask).encode(), filename='example.pdf', source='example_card', kind='statement_pdf')
        commit_import(db_session, preview['import_id'])
    history = db_session.query(SourceBalanceHistory).all()
    assert len(history) == 2
    assert len({row.account_key for row in history}) == 2
    assert {row._value for row in history} == {-25, -50}
