"""Persist connection health independently of successful individual sync stages."""
from datetime import datetime, timedelta


AUTH_CODES = {"ITEM_LOGIN_REQUIRED", "ITEM_LOCKED", "USER_PERMISSION_REVOKED"}
RETRY_CODES = {"INSTITUTION_NOT_RESPONDING", "INTERNAL_SERVER_ERROR", "RATE_LIMIT_EXCEEDED"}


def blocked_error(log):
    extra = log.extra_data or {}
    error = extra.get("last_sync_error") or {}
    if extra.get("item_gone") or error.get("code") == "ITEM_NOT_FOUND":
        return {**error, "code": "ITEM_NOT_FOUND", "action": "relink"}
    if error.get("code") in AUTH_CODES:
        return {**error, "action": "reconnect"}
    retry_at = extra.get("retry_after")
    if retry_at and datetime.fromisoformat(retry_at) > datetime.utcnow():
        return {**error, "action": "retry_later", "retry_after": retry_at}
    return None


def record_error(log, stage, error):
    extra = dict(log.extra_data or {})
    errors = dict(extra.get("sync_stage_errors") or {})
    errors[stage] = {**error, "stage": stage}
    extra["sync_stage_errors"] = errors
    extra["last_sync_error"] = errors[stage]
    if error.get("code") == "ITEM_NOT_FOUND":
        extra["item_gone"] = True
    if error.get("code") in RETRY_CODES:
        failures = min(int(extra.get("retry_count", 0)) + 1, 8)
        extra["retry_count"] = failures
        extra["retry_after"] = (datetime.utcnow() + timedelta(minutes=min(5 * 2 ** (failures - 1), 360))).isoformat()
    log.extra_data = extra


def record_success(log, stage):
    extra = dict(log.extra_data or {})
    errors = dict(extra.get("sync_stage_errors") or {})
    errors.pop(stage, None)
    extra["sync_stage_errors"] = errors
    last = extra.get("last_sync_error") or {}
    if errors:
        extra["last_sync_error"] = next(iter(errors.values()))
    elif last.get("stage", "transactions") == stage:
        extra.pop("last_sync_error", None)
    if not extra.get("last_sync_error"):
        extra.pop("retry_after", None)
        extra.pop("retry_count", None)
    log.extra_data = extra
