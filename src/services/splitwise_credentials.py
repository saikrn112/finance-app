"""Where Splitwise app credentials live.

Preference order: credentials entered in the app UI (stored in the database), then
config.yaml. Storing them in the database keeps them out of the repo — `data/` is gitignored
whereas `config.yaml` is a file the user would otherwise have to hand-edit and risk
committing.

Secrets are write-only from the UI's perspective: `describe()` reports only whether a value
is present plus a short suffix for recognition, never the value itself.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.config import settings
from src.integrations.splitwise import Credentials, credentials_from_settings
from src.models import SyncLog

_SOURCE = "splitwise"
_CONFIG_TYPE = "splitwise_config"


def _config_row(db: Session) -> SyncLog | None:
    return (
        db.query(SyncLog)
        .filter(SyncLog.source == _SOURCE, SyncLog.sync_type == _CONFIG_TYPE)
        .order_by(SyncLog.created_at.desc())
        .first()
    )


def resolve(db: Session) -> Credentials:
    """Effective credentials: database first, then config.yaml for each field."""
    fallback = credentials_from_settings()
    row = _config_row(db)
    stored = (row.extra_data or {}) if row else {}
    return Credentials(
        client_id=stored.get("client_id") or fallback.client_id,
        client_secret=stored.get("client_secret") or fallback.client_secret,
        # A redirect URI is only meaningful for the OAuth flow; default to the app's own
        # callback so the user does not have to work it out.
        redirect_uri=stored.get("redirect_uri") or fallback.redirect_uri or settings.splitwise.redirect_uri,
        api_key=stored.get("api_key") or "",
    )


def save(
    db: Session,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    redirect_uri: str | None = None,
    api_key: str | None = None,
) -> Credentials:
    """Upsert whichever fields were supplied. Blank strings clear a field."""
    row = _config_row(db)
    if row is None:
        row = SyncLog(source=_SOURCE, sync_type=_CONFIG_TYPE, status="configured", extra_data={})
        db.add(row)
    stored = dict(row.extra_data or {})
    for key, value in (
        ("client_id", client_id),
        ("client_secret", client_secret),
        ("redirect_uri", redirect_uri),
        ("api_key", api_key),
    ):
        if value is None:
            continue
        value = value.strip()
        if value:
            stored[key] = value
        else:
            stored.pop(key, None)
    row.extra_data = stored
    db.commit()
    return resolve(db)


def clear(db: Session) -> None:
    for row in db.query(SyncLog).filter(
        SyncLog.source == _SOURCE, SyncLog.sync_type == _CONFIG_TYPE
    ).all():
        db.delete(row)
    db.commit()


def describe(db: Session) -> dict:
    """Safe-to-return shape: presence and origin, never the secret."""
    row = _config_row(db)
    stored = (row.extra_data or {}) if row else {}
    creds = resolve(db)

    def origin(field: str) -> str | None:
        if stored.get(field):
            return "app"
        if getattr(settings.splitwise, field, ""):
            return "config"
        return None

    return {
        "usable": creds.usable,
        "auth_mode": "api_key" if creds.has_api_key else ("oauth" if creds.has_oauth else None),
        "client_id_present": bool(creds.client_id),
        "client_id_hint": creds.client_id[-6:] if creds.client_id else None,
        "client_secret_present": bool(creds.client_secret),
        "api_key_present": bool(creds.api_key),
        "redirect_uri": creds.redirect_uri,
        "client_id_source": origin("client_id"),
        "client_secret_source": origin("client_secret"),
        "api_key_source": "app" if stored.get("api_key") else None,
    }
