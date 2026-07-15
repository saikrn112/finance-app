"""Tests for CSV import functionality."""
import pytest
import tempfile
from pathlib import Path
from datetime import date
from decimal import Decimal

from src.ingestion.csv_importer import parse_csv, get_file_hash, RawTransaction


class TestParseCSV:
    """Tests for CSV parsing with different providers."""

    def test_parse_chase_csv(self):
        content = """Transaction Date,Amount,Description
02/01/2026,-45.00,EXAMPLE GROCER #123
02/02/2026,-32.50,EXAMPLE DELIVERY
02/03/2026,100.00,EXAMPLE REFUND
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(content)
            path = Path(f.name)
        
        txns = list(parse_csv(path, "example_card"))
        assert len(txns) == 3
        
        assert txns[0].date == date(2026, 2, 1)
        assert txns[0].amount == Decimal("-45.00")
        assert txns[0].merchant_raw == "EXAMPLE GROCER #123"
        
        assert txns[2].amount == Decimal("100.00")  # Positive for refund
        path.unlink()

    def test_parse_amex_csv_negates_amount(self):
        content = """Date,Amount,Description
02/01/2026,45.00,RESTAURANT
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(content)
            path = Path(f.name)
        
        txns = list(parse_csv(path, "example_charge_card"))
        assert len(txns) == 1
        assert txns[0].amount == Decimal("-45.00")  # Negated
        path.unlink()

    def test_parse_apple_csv(self):
        content = """Transaction Date,Amount (USD),Merchant
02/01/2026,99.99,EXAMPLE STORE
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(content)
            path = Path(f.name)
        
        txns = list(parse_csv(path, "example_device_card"))
        assert len(txns) == 1
        assert txns[0].amount == Decimal("-99.99")  # Negated
        assert txns[0].merchant_raw == "EXAMPLE STORE"
        path.unlink()

    def test_parse_discover_csv(self):
        content = """Trans. Date,Amount,Description
02/01/2026,-50.00,EXAMPLE STORE
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(content)
            path = Path(f.name)
        
        txns = list(parse_csv(path, "example_credit_card"))
        assert len(txns) == 1
        assert txns[0].merchant_raw == "EXAMPLE STORE"
        path.unlink()

    def test_parse_bofa_csv(self):
        content = """Date,Amount,Description
02/01/2026,-75.00,EXAMPLE WHOLESALE
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(content)
            path = Path(f.name)
        
        txns = list(parse_csv(path, "example_bank"))
        assert len(txns) == 1
        assert txns[0].merchant_raw == "EXAMPLE WHOLESALE"
        path.unlink()

    def test_unknown_source_raises_error(self, temp_csv_file):
        with pytest.raises(ValueError, match="Unknown source"):
            list(parse_csv(temp_csv_file, "unknown_bank"))

    def test_skips_malformed_rows(self):
        content = """Transaction Date,Amount,Description
invalid_date,-45.00,MERCHANT
02/01/2026,not_a_number,MERCHANT2
02/02/2026,-30.00,VALID MERCHANT
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(content)
            path = Path(f.name)
        
        txns = list(parse_csv(path, "example_card"))
        assert len(txns) == 1
        assert txns[0].merchant_raw == "VALID MERCHANT"
        path.unlink()

    def test_skips_empty_date_rows(self):
        content = """Transaction Date,Amount,Description
,-45.00,MERCHANT
02/01/2026,-30.00,VALID
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(content)
            path = Path(f.name)
        
        txns = list(parse_csv(path, "example_card"))
        assert len(txns) == 1
        path.unlink()

    def test_handles_currency_symbols(self):
        content = """Transaction Date,Amount,Description
02/01/2026,"$1,234.56",BIG PURCHASE
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(content)
            path = Path(f.name)
        
        txns = list(parse_csv(path, "example_card"))
        assert len(txns) == 1
        assert txns[0].amount == Decimal("1234.56")
        path.unlink()

    def test_generates_unique_source_id(self):
        content = """Transaction Date,Amount,Description
02/01/2026,-45.00,MERCHANT A
02/01/2026,-45.00,MERCHANT B
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(content)
            path = Path(f.name)
        
        txns = list(parse_csv(path, "example_card"))
        assert txns[0].source_id != txns[1].source_id
        path.unlink()


class TestGetFileHash:
    """Tests for file hash generation."""

    def test_same_content_same_hash(self):
        content = "test content"
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f1:
            f1.write(content)
            path1 = Path(f1.name)
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f2:
            f2.write(content)
            path2 = Path(f2.name)
        
        assert get_file_hash(path1) == get_file_hash(path2)
        path1.unlink()
        path2.unlink()

    def test_different_content_different_hash(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f1:
            f1.write("content 1")
            path1 = Path(f1.name)
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f2:
            f2.write("content 2")
            path2 = Path(f2.name)
        
        assert get_file_hash(path1) != get_file_hash(path2)
        path1.unlink()
        path2.unlink()

    def test_hash_is_sha256_hex(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("test")
            path = Path(f.name)
        
        hash_val = get_file_hash(path)
        assert len(hash_val) == 64  # SHA256 hex length
        assert all(c in "0123456789abcdef" for c in hash_val)
        path.unlink()


class TestRawTransaction:
    """Tests for RawTransaction dataclass."""

    def test_default_account_last4(self):
        txn = RawTransaction(
            source_id="123",
            date=date(2026, 1, 1),
            amount=Decimal("-50.00"),
            merchant_raw="TEST"
        )
        assert txn.account_last4 == ""

    def test_all_fields(self):
        txn = RawTransaction(
            source_id="abc123",
            date=date(2026, 2, 15),
            amount=Decimal("-99.99"),
            merchant_raw="MERCHANT NAME",
            account_last4="1234"
        )
        assert txn.source_id == "abc123"
        assert txn.date == date(2026, 2, 15)
        assert txn.amount == Decimal("-99.99")
        assert txn.merchant_raw == "MERCHANT NAME"
        assert txn.account_last4 == "1234"
