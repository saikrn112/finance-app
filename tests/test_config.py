"""Tests for configuration loading."""
import pytest
import tempfile
import os
from pathlib import Path

from src.config import Settings, PlaidConfig, GeminiConfig, ServerConfig, DatabaseConfig


class TestSettings:
    """Tests for Settings configuration."""

    def test_default_values(self):
        settings = Settings()
        assert settings.plaid.env == "development"
        assert settings.server.host == "0.0.0.0"
        assert settings.server.port == 8000
        assert settings.database.path == "data/finances.db"

    def test_load_from_yaml(self):
        config_content = """
plaid:
  client_id: "test_client"
  secret: "test_secret"
  env: "sandbox"
gemini:
  api_key: "test_key"
server:
  host: "127.0.0.1"
  port: 9000
database:
  path: "test.db"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(config_content)
            path = f.name
        
        settings = Settings.load(path)
        
        assert settings.plaid.client_id == "test_client"
        assert settings.plaid.secret == "test_secret"
        assert settings.plaid.env == "sandbox"
        assert settings.gemini.api_key == "test_key"
        assert settings.server.host == "127.0.0.1"
        assert settings.server.port == 9000
        assert settings.database.path == "test.db"
        
        os.unlink(path)

    def test_load_missing_file_returns_defaults(self):
        settings = Settings.load("/nonexistent/config.yaml")
        assert settings.plaid.client_id == ""
        assert settings.server.port == 8000

    def test_finance_app_config_env_var_overrides_path(self, monkeypatch):
        """The macOS bundle keeps config.yaml in Application Support, not the CWD."""
        config_content = """
server:
  port: 9123
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(config_content)
            path = f.name

        monkeypatch.setenv("FINANCE_APP_CONFIG", path)
        try:
            # The default argument must lose to the env var, not the other way round.
            settings = Settings.load()
            assert settings.server.port == 9123
        finally:
            os.unlink(path)

    def test_finance_app_config_pointing_nowhere_falls_back(self, monkeypatch):
        """A first run with no config yet must not raise."""
        monkeypatch.setenv("FINANCE_APP_CONFIG", "/nonexistent/finance/config.yaml")
        settings = Settings.load()
        assert settings.plaid.client_id == ""

    def test_partial_config(self):
        config_content = """
plaid:
  client_id: "partial_client"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(config_content)
            path = f.name
        
        settings = Settings.load(path)
        
        assert settings.plaid.client_id == "partial_client"
        assert settings.plaid.secret == ""  # Default
        assert settings.server.port == 8000  # Default
        
        os.unlink(path)


class TestPlaidConfig:
    """Tests for Plaid configuration."""

    def test_default_values(self):
        config = PlaidConfig()
        assert config.client_id == ""
        assert config.secret == ""
        assert config.env == "development"

    def test_custom_values(self):
        config = PlaidConfig(
            client_id="my_client",
            secret="my_secret",
            env="production"
        )
        assert config.client_id == "my_client"
        assert config.secret == "my_secret"
        assert config.env == "production"


class TestGeminiConfig:
    """Tests for Gemini configuration."""

    def test_default_values(self):
        config = GeminiConfig()
        assert config.api_key == ""

    def test_custom_values(self):
        config = GeminiConfig(api_key="test_api_key")
        assert config.api_key == "test_api_key"


class TestServerConfig:
    """Tests for server configuration."""

    def test_default_values(self):
        config = ServerConfig()
        assert config.host == "0.0.0.0"
        assert config.port == 8000

    def test_custom_values(self):
        config = ServerConfig(host="localhost", port=3000)
        assert config.host == "localhost"
        assert config.port == 3000


class TestDatabaseConfig:
    """Tests for database configuration."""

    def test_default_values(self):
        config = DatabaseConfig()
        assert config.path == "data/finances.db"

    def test_custom_values(self):
        config = DatabaseConfig(path="/custom/path/db.sqlite")
        assert config.path == "/custom/path/db.sqlite"
