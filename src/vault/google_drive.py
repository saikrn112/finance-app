from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import hashlib
import http.client
import json
import mimetypes
from pathlib import Path
import secrets
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fastapi import HTTPException

from src.config import settings


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
GOOGLE_DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
GOOGLE_DRIVE_DOWNLOAD_URL = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
GOOGLE_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/drive.file",
]


def create_google_auth_url() -> str:
    _require_google_config()
    state = secrets.token_urlsafe(24)
    query_payload = {
        "client_id": settings.google_drive.client_id,
        "redirect_uri": settings.google_drive.redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    pending_state = {"created_at": _utc_now()}
    if _use_pkce():
        verifier = _pkce_verifier()
        pending_state["code_verifier"] = verifier
        query_payload["code_challenge"] = _pkce_challenge(verifier)
        query_payload["code_challenge_method"] = "S256"
    _write_pending_state(state, pending_state)
    query = urlencode(query_payload)
    return f"{GOOGLE_AUTH_URL}?{query}"


def validate_google_state(state: str) -> dict[str, Any]:
    pending = _read_pending_state(state)
    if not pending:
        raise HTTPException(status_code=400, detail="Invalid Google OAuth state")
    created_at_raw = pending.get("created_at")
    if not created_at_raw:
        _delete_pending_state(state)
        raise HTTPException(status_code=400, detail="Invalid Google OAuth state")
    created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
    if datetime.now(timezone.utc) - created_at > timedelta(minutes=15):
        _delete_pending_state(state)
        raise HTTPException(status_code=400, detail="Expired Google OAuth state")
    return pending


def exchange_google_code(code: str, pending_state: dict[str, Any] | None = None) -> dict[str, Any]:
    _require_google_config()
    payload_data = {
        "code": code,
        "client_id": settings.google_drive.client_id,
        "client_secret": settings.google_drive.client_secret,
        "redirect_uri": settings.google_drive.redirect_uri,
        "grant_type": "authorization_code",
    }
    if _use_pkce():
        verifier = (pending_state or {}).get("code_verifier")
        if not verifier:
            raise HTTPException(status_code=400, detail="Missing Google PKCE verifier")
        payload_data["code_verifier"] = verifier
    payload = urlencode(payload_data).encode()
    request = Request(
        GOOGLE_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    response = _json_request(request)
    access_token = response.get("access_token")
    if not access_token:
        raise HTTPException(status_code=500, detail="Google token exchange failed")
    userinfo = get_google_userinfo(access_token)
    response["userinfo"] = userinfo
    return response


def refresh_google_token(refresh_token: str) -> dict[str, Any]:
    _require_google_config()
    payload_data = {
        "refresh_token": refresh_token,
        "client_id": settings.google_drive.client_id,
        "client_secret": settings.google_drive.client_secret,
        "grant_type": "refresh_token",
    }
    payload = urlencode(payload_data).encode()
    request = Request(
        GOOGLE_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    response = _json_request(request)
    if not response.get("access_token"):
        raise HTTPException(status_code=500, detail="Google token refresh failed")
    return response


def get_google_userinfo(access_token: str) -> dict[str, Any]:
    request = Request(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    return _json_request(request)


def list_drive_files(
    access_token: str,
    *,
    name: str | None = None,
    parent_id: str | None = None,
    mime_type: str | None = None,
) -> list[dict[str, Any]]:
    query = "trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    if name:
        safe_name = name.replace("'", "\\'")
        query += f" and name = '{safe_name}'"
    if mime_type:
        safe_mime = mime_type.replace("'", "\\'")
        query += f" and mimeType = '{safe_mime}'"
    params = urlencode(
        {
            "q": query,
            "fields": "files(id,name,mimeType,createdTime,modifiedTime,size,parents)",
            "pageSize": 100,
        }
    )
    request = Request(
        f"{GOOGLE_DRIVE_FILES_URL}?{params}",
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    return (_json_request(request).get("files") or [])


def ensure_visible_app_folder(access_token: str) -> dict[str, Any]:
    folder_name = settings.google_drive.folder_name
    existing = list_drive_files(
        access_token,
        name=folder_name,
        mime_type=FOLDER_MIME_TYPE,
    )
    if existing:
        return existing[0]

    boundary = f"==============={secrets.token_hex(12)}=="
    metadata = {"name": folder_name, "mimeType": FOLDER_MIME_TYPE}
    multipart = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    request = Request(
        f"{GOOGLE_DRIVE_UPLOAD_URL}?uploadType=multipart",
        data=multipart,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        method="POST",
    )
    return _json_request(request)


def ensure_child_folder(access_token: str, *, parent_id: str, name: str) -> dict[str, Any]:
    existing = list_drive_files(
        access_token,
        name=name,
        parent_id=parent_id,
        mime_type=FOLDER_MIME_TYPE,
    )
    if existing:
        return existing[0]

    boundary = f"==============={secrets.token_hex(12)}=="
    metadata = {"name": name, "mimeType": FOLDER_MIME_TYPE, "parents": [parent_id]}
    multipart = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    request = Request(
        f"{GOOGLE_DRIVE_UPLOAD_URL}?uploadType=multipart",
        data=multipart,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        method="POST",
    )
    return _json_request(request)


def upload_multipart_file(
    access_token: str,
    *,
    name: str,
    content_bytes: bytes,
    mime_type: str,
    parent_id: str | None = None,
    file_id: str | None = None,
) -> dict[str, Any]:
    boundary = f"==============={secrets.token_hex(12)}=="
    metadata: dict[str, Any] = {"name": name}
    if parent_id and not file_id:
        metadata["parents"] = [parent_id]
    multipart = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode() + content_bytes + f"\r\n--{boundary}--\r\n".encode()
    if file_id:
        url = f"{GOOGLE_DRIVE_UPLOAD_URL}/{file_id}?uploadType=multipart"
        method = "PATCH"
    else:
        url = f"{GOOGLE_DRIVE_UPLOAD_URL}?uploadType=multipart"
        method = "POST"
    request = Request(
        url,
        data=multipart,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        method=method,
    )
    return _json_request(request)


def upload_file_resumable(
    access_token: str,
    *,
    name: str,
    file_path: Path,
    mime_type: str,
    parent_id: str | None = None,
    file_id: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name}
    if parent_id:
        metadata["parents"] = [parent_id]

    if file_id:
        init_url = f"{GOOGLE_DRIVE_UPLOAD_URL}/{file_id}?uploadType=resumable"
        init_method = "PATCH"
    else:
        init_url = f"{GOOGLE_DRIVE_UPLOAD_URL}?uploadType=resumable"
        init_method = "POST"

    init_request = Request(
        init_url,
        data=json.dumps(metadata).encode(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime_type,
            "X-Upload-Content-Length": str(file_path.stat().st_size),
        },
        method=init_method,
    )

    try:
        with urlopen(init_request) as response:
            upload_url = response.headers.get("Location")
        if not upload_url:
            raise HTTPException(status_code=500, detail="Google resumable upload session did not return a Location header")
    except HTTPException:
        raise
    except HTTPError as exc:
        detail = f"Google resumable session failed: {exc}"
        try:
            body = exc.read().decode("utf-8", "ignore").strip()
            if body:
                detail = f"{detail} :: {body[:1000]}"
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=detail) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Google resumable session failed: {exc}") from exc

    parsed = urlparse(upload_url)
    connection = http.client.HTTPSConnection(parsed.netloc, timeout=600)
    try:
        connection.putrequest("PUT", parsed.path + (f"?{parsed.query}" if parsed.query else ""))
        connection.putheader("Authorization", f"Bearer {access_token}")
        connection.putheader("Content-Type", mime_type)
        connection.putheader("Content-Length", str(file_path.stat().st_size))
        connection.endheaders()

        with file_path.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                connection.send(chunk)

        response = connection.getresponse()
        response_body = response.read().decode("utf-8", "ignore")
        if response.status not in (200, 201):
            raise HTTPException(
                status_code=500,
                detail=f"Google resumable upload failed: HTTP {response.status} {response.reason} :: {response_body[:1000]}",
            )
        return json.loads(response_body or "{}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Google resumable upload failed: {exc}") from exc
    finally:
        connection.close()


def download_file_bytes(access_token: str, file_id: str) -> bytes:
    request = Request(
        f"{GOOGLE_DRIVE_DOWNLOAD_URL}/{file_id}?alt=media",
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with urlopen(request) as response:
            return response.read()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Google file download failed: {exc}") from exc


def download_file_to_path(
    access_token: str,
    file_id: str,
    dest: Path,
    *,
    expected_size: int | None = None,
    on_progress: Any | None = None,
) -> None:
    request = Request(
        f"{GOOGLE_DRIVE_DOWNLOAD_URL}/{file_id}?alt=media",
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with urlopen(request) as response:
            total = expected_size or int(response.headers.get("Content-Length") or 0)
            downloaded = 0
            with dest.open("wb") as fh:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if on_progress and total:
                        on_progress(downloaded, total)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Google file download failed: {exc}") from exc


def ensure_fresh_google_access_token(extra_data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    access_token = extra_data.get("access_token")
    expires_at = extra_data.get("expires_at")
    refresh_token = extra_data.get("refresh_token")
    now = datetime.now(timezone.utc)
    if access_token and expires_at:
        try:
            if now < datetime.fromisoformat(expires_at.replace("Z", "+00:00")) - timedelta(minutes=2):
                return access_token, extra_data
        except Exception:
            pass
    if not refresh_token:
        raise HTTPException(status_code=400, detail="Google Drive needs to be reconnected")
    refreshed = refresh_google_token(refresh_token)
    updated = dict(extra_data)
    updated["access_token"] = refreshed["access_token"]
    updated["expires_at"] = _expiry_timestamp(int(refreshed.get("expires_in", 3600)))
    if refreshed.get("refresh_token"):
        updated["refresh_token"] = refreshed["refresh_token"]
    return updated["access_token"], updated


def token_payload_to_extra(response: dict[str, Any]) -> dict[str, Any]:
    userinfo = response.get("userinfo") or {}
    return {
        "provider": "google_drive",
        "access_token": response.get("access_token"),
        "refresh_token": response.get("refresh_token"),
        "token_type": response.get("token_type"),
        "scope": response.get("scope"),
        "expires_at": _expiry_timestamp(int(response.get("expires_in", 3600))),
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "sub": userinfo.get("sub"),
    }


def _expiry_timestamp(expires_in_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_request(request: Request) -> dict[str, Any]:
    try:
        with urlopen(request) as response:
            return json.loads(response.read().decode())
    except HTTPException:
        raise
    except HTTPError as exc:
        detail = f"Google API request failed: {exc}"
        try:
            body = exc.read().decode("utf-8", "ignore").strip()
            if body:
                detail = f"{detail} :: {body[:1000]}"
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=detail) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Google API request failed: {exc}") from exc


def _require_google_config() -> None:
    if not settings.google_drive.client_id:
        raise HTTPException(status_code=400, detail="Google OAuth client is not configured for this app")
    if not settings.google_drive.client_secret:
        raise HTTPException(status_code=400, detail="Google OAuth client secret is not configured for this app")


def _use_pkce() -> bool:
    return settings.google_drive.oauth_client_type.lower() == "desktop"


def _pkce_verifier() -> str:
    return secrets.token_urlsafe(64)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _pending_state_dir() -> Path:
    path = Path(settings.app.runtime_dir) / ".oauth" / "google_pending"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pending_state_path(state: str) -> Path:
    return _pending_state_dir() / f"{state}.json"


def _write_pending_state(state: str, payload: dict[str, Any]) -> None:
    _pending_state_path(state).write_text(json.dumps(payload, indent=2, sort_keys=True))


def _read_pending_state(state: str) -> dict[str, Any] | None:
    path = _pending_state_path(state)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def delete_google_pending_state(state: str) -> None:
    _delete_pending_state(state)


def _delete_pending_state(state: str) -> None:
    _pending_state_path(state).unlink(missing_ok=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
