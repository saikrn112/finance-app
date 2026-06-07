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
        b"02/10/2026,-5.25,STARBUCKS STORE 12345\n"
        b"02/12/2026,-5.25,STARBUCKS STORE 12345\n"
    )
    preview = preview_import(
        db_session,
        file_bytes=content,
        filename="chase.csv",
        source="chase",
        kind="csv",
    )
    result = commit_import(db_session, preview["import_id"])

    rows = db_session.query(Transaction).filter(Transaction.source == "Chase").all()
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
        filename="chase.csv",
        source="chase",
        kind="csv",
    )
    commit_import(db_session, preview1["import_id"])

    preview2 = preview_import(
        db_session,
        file_bytes=content,
        filename="chase.csv",
        source="chase",
        kind="csv",
    )
    result2 = commit_import(db_session, preview2["import_id"])

    rows = db_session.query(Transaction).filter(Transaction.source == "Chase").all()
    assert result2["status"] == "already_imported"
    assert result2["imported"] == 0
    assert len(rows) == 1


def test_statement_vs_plaid_heuristic_dedup_still_works(db_session):
    """A statement row that matches an existing Plaid row by heuristic (same source,
    nearby date, same absolute amount, similar merchant) must still be suppressed."""
    db_session.add(
        Transaction(
            source="Chase",
            source_id="plaid-chase-1",
            origin="plaid",
            date=date(2026, 2, 16),
            amount=Decimal("-23.47"),
            merchant_raw="MOLLY TEA SUNNYVALE",
            merchant_clean="Molly Tea",
            category="Dining",
            category_source="rule",
        )
    )
    db_session.commit()

    content = b"Transaction Date,Amount,Description\n02/15/2026,-23.47,MOLLY TEA SUNNYVALE\n"
    preview = preview_import(
        db_session,
        file_bytes=content,
        filename="chase.csv",
        source="chase",
        kind="csv",
    )
    result = commit_import(db_session, preview["import_id"])

    assert result["imported"] == 0
    assert result["skipped"] == 1


def test_preview_import_marks_cross_source_duplicates(db_session):
    db_session.add(
        Transaction(
            source="American Express",
            source_id="plaid-1",
            origin="plaid",
            date=date(2026, 2, 8),
            amount=Decimal("-132.65"),
            merchant_raw="Walmart",
            merchant_clean="Walmart",
            category="Groceries",
            category_source="rule",
        )
    )
    db_session.commit()

    content = b"Date,Amount,Description\n02/07/2026,132.65,WAL-MART NEIGHBORHOOD MARKET 1234 ANYTOWN CA\n"
    preview = preview_import(
        db_session,
        file_bytes=content,
        filename="amex.csv",
        source="amex",
        kind="csv",
    )

    assert preview["duplicate_summary"]["duplicate_count"] == 1
    assert preview["duplicate_summary"]["items"][0]["duplicate_reason"]["type"] == "heuristic"


def test_preview_import_marks_legacy_statement_duplicates(db_session):
    db_session.add(
        Transaction(
            source="Chase",
            source_id="Chase_2024-08-20_16.0_0",
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
        filename="chase.csv",
        source="chase",
        kind="csv",
    )

    assert preview["duplicate_summary"]["duplicate_count"] == 1
    assert preview["duplicate_summary"]["items"][0]["duplicate_reason"]["type"] == "legacy_statement"


def test_commit_import_skips_duplicates_and_inserts_new_rows(db_session):
    db_session.add(
        Transaction(
            source="Chase",
            source_id="existing",
            origin="plaid",
            date=date(2026, 2, 16),
            amount=Decimal("-23.47"),
            merchant_raw="MOLLY TEA SUNNYVALE",
            merchant_clean="Molly Tea",
            category="Dining",
            category_source="rule",
        )
    )
    db_session.commit()

    content = b"Transaction Date,Amount,Description\n02/15/2026,-23.47,MOLLY TEA SUNNYVALE\n02/17/2026,-18.00,PACIFIC COFFEE\n"
    preview = preview_import(
        db_session,
        file_bytes=content,
        filename="chase.csv",
        source="chase",
        kind="csv",
    )
    result = commit_import(db_session, preview["import_id"])

    rows = db_session.query(Transaction).filter(Transaction.source == "Chase").all()
    assert result["imported"] == 1
    assert result["skipped"] == 1
    assert len(rows) == 2


def test_preview_marks_reimported_file_as_duplicate(db_session):
    content = b"Transaction Date,Amount,Description\n02/15/2026,-18.00,PACIFIC COFFEE\n"
    first = preview_import(
        db_session,
        file_bytes=content,
        filename="chase.csv",
        source="chase",
        kind="csv",
    )
    commit_import(db_session, first["import_id"])

    second = preview_import(
        db_session,
        file_bytes=content,
        filename="chase.csv",
        source="chase",
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
        b"02/14/26,Employee Pre-Tax,Employee,Vanguard 2060,7.8142,138.22,1080.54\n"
        b"02/14/26,Employer Match,Employer Match,Vanguard 2060,3.9071,138.22,540.27\n"
    )

    preview = preview_import(
        db_session,
        file_bytes=content,
        filename="transamerica.csv",
        source="transamerica",
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
        b"02/14/26,Employee Pre-Tax,Employee,Vanguard 2060,7.8142,138.22,1080.54\n"
        b"02/14/26,Employer Match,Employer Match,Vanguard 2060,3.9071,138.22,540.27\n"
    )
    first = preview_import(
        db_session,
        file_bytes=first_content,
        filename="transamerica-1.csv",
        source="transamerica",
        kind="retirement_csv",
    )
    commit_import(db_session, first["import_id"])

    second_content = (
        b"Date,Transaction Type,Source,Fund Name,Unit Count,Unit Value,Transaction Amount\n"
        b"02/14/26,Employee Pre-Tax,Employee,Vanguard 2060,7.8142,138.22,1080.54\n"
        b"02/14/26,Employer Match,Employer Match,Vanguard 2060,3.9071,138.22,540.27\n"
        b"02/28/26,Employee Pre-Tax,Employee,Vanguard 2060,8.0000,140.00,1120.00\n"
    )
    second = preview_import(
        db_session,
        file_bytes=second_content,
        filename="transamerica-2.csv",
        source="transamerica",
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


def test_retirement_preview_marks_legacy_csv_duplicates(db_session, monkeypatch):
    first_preview = preview_import(
        db_session,
        file_bytes=(
            b"Date,Transaction Type,Source,Fund Name,Unit Count,Unit Value,Transaction Amount\n"
            b"02/14/26,Employee Pre-Tax,Employee,Vanguard 2060,7.8142,138.22,1080.54\n"
        ),
        filename="transamerica.csv",
        source="transamerica",
        kind="retirement_csv",
    )
    legacy_source_id = first_preview["payload"]["transactions"][0]["source_id"]
    monkeypatch.setattr(
        "src.ingestion.import_service._legacy_retirement_source_ids",
        lambda: {
            legacy_source_id: {
                "existing_id": f"legacy-retirement:{legacy_source_id}",
                "existing_origin": "legacy_retirement_csv",
            }
        },
    )

    preview = preview_import(
        db_session,
        file_bytes=(
            b"Date,Transaction Type,Source,Fund Name,Unit Count,Unit Value,Transaction Amount\n"
            b"02/14/26,Employee Pre-Tax,Employee,Vanguard 2060,7.8142,138.22,1080.54\n"
        ),
        filename="transamerica.csv",
        source="transamerica",
        kind="retirement_csv",
    )

    assert preview["duplicate_summary"]["duplicate_count"] == 1
    assert preview["duplicate_summary"]["importable_count"] == 0
    assert preview["duplicate_summary"]["items"][0]["duplicate_reason"]["type"] == "retirement_source_id"
    assert preview["duplicate_summary"]["items"][0]["duplicate_reason"]["existing_origin"] == "legacy_retirement_csv"
