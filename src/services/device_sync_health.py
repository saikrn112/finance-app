"""Local, safe status for device convergence; never returns provider tokens or payloads."""
from datetime import datetime, timezone
from src.models.transaction import AppMetadata

KEY = "device_sync_health"

def record(db, *, successful: bool, problem: str | None = None) -> None:
    import json
    row = db.get(AppMetadata, KEY)
    state = read(db)
    now = datetime.now(timezone.utc).isoformat()
    state.update(last_attempt=now, last_error=None if successful else (problem or "Device sync did not complete. Try again or reconnect Google Drive."))
    if successful:
        state["last_success"] = now
    if row is None:
        row = AppMetadata(key=KEY)
        db.add(row)
    row.value = json.dumps(state)
    try:
        db.commit()
    except Exception:
        # Health reporting must never poison a completed financial transaction.
        db.rollback()

def read(db) -> dict:
    import json
    row = db.get(AppMetadata, KEY)
    try:
        state = json.loads(row.value) if row and row.value else {}
        return state if isinstance(state, dict) else {}
    except (ValueError, TypeError):
        return {}
