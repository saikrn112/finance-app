from pydantic_settings import BaseSettings
from pydantic import BaseModel
from pathlib import Path
import os
import yaml

# src/config.py -> src/ -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PlaidConfig(BaseModel):
    client_id: str = ""
    secret: str = ""
    environment: str = "sandbox"


class GeminiConfig(BaseModel):
    api_key: str = ""


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class AppConfig(BaseModel):
    mode: str = "live"
    data_dir: str = "data"
    runtime_dir: str = "data/runtime/prod"
    display_currency: str = "USD"


class DatabaseConfig(BaseModel):
    path: str = "data/runtime/prod/finances.db"


class GoogleDriveConfig(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    appdata_manifest_prefix: str = "finance-app-vault"
    folder_name: str = "Finance Vault"
    oauth_client_type: str = "desktop"


class SplitwiseConfig(BaseModel):
    """Optional. Absent credentials simply mean the integration is unavailable."""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    # Splitwise creates one expense per API call; there is no documented bulk endpoint.
    # Commits are therefore batched client-side so a large project drains over several
    # presses rather than one long request. Tune if the real limits differ.
    commit_batch_size: int = 4


class Settings(BaseSettings):
    app: AppConfig = AppConfig()
    plaid: PlaidConfig = PlaidConfig()
    gemini: GeminiConfig = GeminiConfig()
    server: ServerConfig = ServerConfig()
    database: DatabaseConfig = DatabaseConfig()
    google_drive: GoogleDriveConfig = GoogleDriveConfig()
    splitwise: SplitwiseConfig = SplitwiseConfig()

    @staticmethod
    def _deep_update(base: dict, overrides: dict) -> dict:
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key] = Settings._deep_update(base[key], value)
            else:
                base[key] = value
        return base

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Settings":
        data: dict = {}
        # FINANCE_APP_CONFIG lets a host process (e.g. the macOS app bundle) keep
        # config.yaml outside the working directory. Without it the config is only
        # ever found when the process is launched from the repository root.
        config_path = Path(os.getenv("FINANCE_APP_CONFIG") or path)
        example_path = Path("config.yaml.example")
        if not example_path.exists():
            example_path = _PROJECT_ROOT / "config.yaml.example"
        if config_path.exists():
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
        elif example_path.exists():
            with open(example_path) as f:
                data = yaml.safe_load(f) or {}

        overrides: dict = {}
        if mode := os.getenv("FINANCE_APP_MODE"):
            overrides.setdefault("app", {})["mode"] = mode
        if data_dir := os.getenv("FINANCE_APP_DATA_DIR"):
            overrides.setdefault("app", {})["data_dir"] = data_dir
        if runtime_dir := os.getenv("FINANCE_APP_RUNTIME_DIR"):
            overrides.setdefault("app", {})["runtime_dir"] = runtime_dir
        if db_path := os.getenv("FINANCE_APP_DB_PATH"):
            overrides.setdefault("database", {})["path"] = db_path
        if port := os.getenv("FINANCE_APP_PORT"):
            overrides.setdefault("server", {})["port"] = int(port)
        if host := os.getenv("FINANCE_APP_HOST"):
            overrides.setdefault("server", {})["host"] = host
        if google_redirect_uri := os.getenv("FINANCE_APP_GOOGLE_REDIRECT_URI"):
            overrides.setdefault("google_drive", {})["redirect_uri"] = google_redirect_uri
        if google_folder_name := os.getenv("FINANCE_APP_GOOGLE_FOLDER_NAME"):
            overrides.setdefault("google_drive", {})["folder_name"] = google_folder_name
        if google_client_id := os.getenv("FINANCE_APP_GOOGLE_CLIENT_ID"):
            overrides.setdefault("google_drive", {})["client_id"] = google_client_id
        if google_client_secret := os.getenv("FINANCE_APP_GOOGLE_CLIENT_SECRET"):
            overrides.setdefault("google_drive", {})["client_secret"] = google_client_secret
        if google_client_type := os.getenv("FINANCE_APP_GOOGLE_CLIENT_TYPE"):
            overrides.setdefault("google_drive", {})["oauth_client_type"] = google_client_type
        if sw_client_id := os.getenv("FINANCE_APP_SPLITWISE_CLIENT_ID"):
            overrides.setdefault("splitwise", {})["client_id"] = sw_client_id
        if sw_client_secret := os.getenv("FINANCE_APP_SPLITWISE_CLIENT_SECRET"):
            overrides.setdefault("splitwise", {})["client_secret"] = sw_client_secret
        if sw_redirect := os.getenv("FINANCE_APP_SPLITWISE_REDIRECT_URI"):
            overrides.setdefault("splitwise", {})["redirect_uri"] = sw_redirect
        if sw_batch := os.getenv("FINANCE_APP_SPLITWISE_BATCH_SIZE"):
            overrides.setdefault("splitwise", {})["commit_batch_size"] = int(sw_batch)
        if display_currency := os.getenv("FINANCE_APP_DISPLAY_CURRENCY"):
            overrides.setdefault("app", {})["display_currency"] = display_currency

        if overrides:
            data = cls._deep_update(data, overrides)

        app_data = data.get("app", {})
        db_data = data.get("database", {})
        runtime_dir = app_data.get("runtime_dir") or (Path(app_data["data_dir"]) / "runtime" / "prod" if app_data.get("data_dir") else None)
        if runtime_dir and not app_data.get("runtime_dir"):
            data.setdefault("app", {})["runtime_dir"] = str(runtime_dir)
        if runtime_dir and not db_data.get("path"):
            data.setdefault("database", {})["path"] = str(Path(runtime_dir) / "finances.db")

        instance = cls(**data) if data else cls()
        if not instance.google_drive.redirect_uri:
            port = instance.server.port
            instance.google_drive.redirect_uri = f"http://localhost:{port}/api/settings/vault/google/callback"
        if not instance.splitwise.redirect_uri:
            port = instance.server.port
            instance.splitwise.redirect_uri = f"http://localhost:{port}/api/splitwise/callback"
        return instance

    @property
    def is_demo(self) -> bool:
        return self.app.mode == "demo"


settings = Settings.load()


def get_config() -> dict:
    """Return config as dict for backward compatibility."""
    return {
        "plaid": {
            "client_id": settings.plaid.client_id,
            "secret": settings.plaid.secret,
            "environment": settings.plaid.environment,
        },
        "gemini": {"api_key": settings.gemini.api_key},
        "google_drive": {
            "client_id": settings.google_drive.client_id,
            "client_secret": settings.google_drive.client_secret,
            "redirect_uri": settings.google_drive.redirect_uri,
            "appdata_manifest_prefix": settings.google_drive.appdata_manifest_prefix,
            "folder_name": settings.google_drive.folder_name,
            "oauth_client_type": settings.google_drive.oauth_client_type,
        },
    }
