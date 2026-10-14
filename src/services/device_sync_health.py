"""Local, safe status for device convergence; never returns provider tokens or payloads."""
from datetime import datetime, timedelta, timezone
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


#: Twelve missed five-minute rounds. Long enough that a slow network or one failed round never
#: raises the alarm, short enough that a dead Google sign-in is noticed the same day -- not a
#: week later, which is how long it went unnoticed before this existed.
STALE_AFTER_MINUTES = 60

#: No job legitimately holds the lock this long: a full Plaid pull or snapshot takes a minute or two.
STUCK_AFTER_MINUTES = 15

RECONNECT_REASON = "Google Drive sign-in expired. Reconnect it in Settings → Vault so your devices can sync."


def _parse(value):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def attention(
    state: dict,
    *,
    enabled: bool,
    configured: bool,
    peer_count: int,
    google_expired: bool = False,
    busy_job: str | None = None,
    busy_seconds: float | None = None,
    now: datetime | None = None,
) -> dict:
    """Whether the user should be told device sync has stopped, and why.

    Decided here rather than in each client, so the container, the macOS app and a future iOS app
    raise the same alarm from the same facts. Every branch answers "is data still moving between
    devices?" -- a stored token, or one successful round days ago, is not evidence that it is.
    """
    now = now or datetime.now(timezone.utc)
    last_success = _parse(state.get("last_success"))
    last_attempt = _parse(state.get("last_attempt"))
    quiet = {"needed": False, "reason": None, "last_success": state.get("last_success")}

    if not enabled:
        return quiet  # paused on purpose is not a fault
    if busy_job and busy_seconds is not None and busy_seconds > STUCK_AFTER_MINUTES * 60:
        # Checked before staleness: the honest reason sync stopped is not Google but a job that
        # never finished. Seen as a Plaid pull hung on a dead connection for 22 hours.
        return {**quiet, "needed": True,
                "reason": f"A {busy_job} has been stuck for {int(busy_seconds // 60)} minutes. Quit and reopen the app."}
    if google_expired:
        return {**quiet, "needed": True, "reason": RECONNECT_REASON}
    if not configured:
        return {**quiet, "needed": True, "reason": "Connect Google Drive in Settings → Vault so your devices can sync."}
    if state.get("last_error") and (last_success is None or (last_attempt and last_attempt > last_success)):
        return {**quiet, "needed": True, "reason": state["last_error"]}
    if last_success is None:
        if peer_count:
            return {**quiet, "needed": True, "reason": "This device has not completed a sync with your other devices yet."}
        return quiet  # a lone device with nothing to sync with
    if now - last_success > timedelta(minutes=STALE_AFTER_MINUTES):
        return {**quiet, "needed": True, "reason": "No device sync has succeeded recently. Check Google Drive in Settings."}
    return quiet
