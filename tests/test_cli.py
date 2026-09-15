"""Tests for CLI commands."""
import pytest
from click.testing import CliRunner
from unittest.mock import patch, MagicMock
import tempfile
import os
from pathlib import Path

from src.main import cli, _clean_merchant


class TestCleanMerchant:
    """Tests for merchant name cleanup."""

    def test_removes_asterisk_suffix(self):
        # The asterisk and everything after it goes; no provider aliasing happens here,
        # that lives in the plugin repo.
        assert _clean_merchant("EXAMPLE DELIVERY *TRIP 123") == "EXAMPLE DELIVERY"

    def test_removes_hash_numbers(self):
        assert _clean_merchant("WHOLEFDS #12345") == "WHOLEFDS"

    def test_normalizes_whitespace(self):
        assert _clean_merchant("MERCHANT   NAME") == "MERCHANT NAME"

    def test_truncates_long_names(self):
        long_name = "A" * 100
        result = _clean_merchant(long_name)
        assert len(result) <= 50

    def test_handles_empty_after_cleanup(self):
        result = _clean_merchant("*12345")
        assert len(result) > 0  # Falls back to original truncated


class TestFetchCommand:
    """Tests for fetch command."""

    def test_fetch_without_config(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["fetch"])
        assert result.exit_code == 0
        assert "not yet configured" in result.output


class TestImportCommand:
    """Tests for import command."""

    def test_import_requires_file(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["import", "--source", "example_card"])
        assert result.exit_code != 0

    def test_import_requires_source(self):
        runner = CliRunner()
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(b"Transaction Date,Amount,Description\n")
            path = f.name
        
        result = runner.invoke(cli, ["import", "--file", path])
        assert result.exit_code != 0
        os.unlink(path)

    def test_import_validates_source(self):
        runner = CliRunner()
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(b"Transaction Date,Amount,Description\n")
            path = f.name
        
        result = runner.invoke(cli, ["import", "--file", path, "--source", "invalid"])
        assert result.exit_code != 0
        os.unlink(path)

    def test_import_dry_run(self):
        runner = CliRunner()
        csv_content = """Transaction Date,Amount,Description
02/01/2026,-45.00,TEST MERCHANT
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            f.write(csv_content)
            path = f.name
        
        with patch("src.models.init_db"), \
             patch("src.models.SessionLocal") as mock_session:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.first.return_value = None
            mock_session.return_value = mock_db
            
            result = runner.invoke(cli, ["import", "--file", path, "--source", "example_card", "--dry-run"])
            assert "[DRY RUN]" in result.output
        
        os.unlink(path)


class TestServeCommand:
    """Tests for serve command."""

    def test_serve_default_options(self):
        runner = CliRunner()
        with patch("src.models.init_db"), \
             patch("uvicorn.run") as mock_run:
            result = runner.invoke(cli, ["serve"])
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            assert call_args.kwargs["host"] == "0.0.0.0"
            assert call_args.kwargs["port"] == 8000

    def test_serve_custom_port(self):
        runner = CliRunner()
        with patch("src.models.init_db"), \
             patch("uvicorn.run") as mock_run:
            result = runner.invoke(cli, ["serve", "--port", "9000"])
            call_args = mock_run.call_args
            assert call_args.kwargs["port"] == 9000


class TestReparseCommand:
    """Tests for reparse command."""

    def test_reparse_no_files(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("src.data_paths.RAW_ROOT", Path(tmpdir)):
                result = runner.invoke(cli, ["reparse"])
                assert "No raw files found" in result.output


class TestCategorizeCommand:
    """Tests for categorize command."""

    def test_categorize_no_uncategorized(self):
        runner = CliRunner()
        with patch("src.models.init_db"), \
             patch("src.models.SessionLocal") as mock_session:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.all.return_value = []
            mock_session.return_value = mock_db
            
            result = runner.invoke(cli, ["categorize"])
            assert "No uncategorized transactions" in result.output

    def test_categorize_with_llm_flag(self):
        runner = CliRunner()
        with patch("src.models.init_db"), \
             patch("src.models.SessionLocal") as mock_session, \
             patch("src.processing.categorizer.CategorizationEngine") as mock_engine:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.all.return_value = []
            mock_session.return_value = mock_db
            
            result = runner.invoke(cli, ["categorize", "--use-llm"])
            assert result.exit_code == 0
