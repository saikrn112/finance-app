from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid
import zipfile

from fastapi import HTTPException

from src.config import settings


BACKUP_MANIFEST_NAME = "backup.manifest.json"
LOCAL_METADATA_NAME = "vault_metadata.json"
RESTORE_RECEIPT_NAME = "restore_receipt.json"


def vault_metadata_path() -> Path:
    return Path(settings.app.runtime_dir) / LOCAL_METADATA_NAME


def restore_receipt_path() -> Path:
    return Path(settings.app.runtime_dir) / RESTORE_RECEIPT_NAME


def load_vault_metadata() -> dict:
    path = vault_metadata_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_vault_metadata(data: dict) -> None:
    path = vault_metadata_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def clear_local_vault_state() -> None:
    vault_metadata_path().unlink(missing_ok=True)
    restore_receipt_path().unlink(missing_ok=True)


def ensure_vault_metadata(provider: str | None = None, provider_email: str | None = None) -> dict:
    data = load_vault_metadata()
    changed = False
    if not data.get("vault_id"):
        data["vault_id"] = str(uuid.uuid4())
        data["created_at"] = _utc_now()
        changed = True
    if not data.get("device_id"):
        data["device_id"] = str(uuid.uuid4())
        changed = True
    if not data.get("device_label"):
        data["device_label"] = _default_device_label()
        changed = True
    if provider and data.get("provider") != provider:
        data["provider"] = provider
        changed = True
    if provider_email and data.get("provider_email") != provider_email:
        data["provider_email"] = provider_email
        changed = True
    if changed:
        save_vault_metadata(data)
    return data


def mark_backup_succeeded(
    provider: str,
    backup_manifest: dict,
    remote_file_id: str | None = None,
    remote_manifest_file_id: str | None = None,
) -> dict:
    data = ensure_vault_metadata(provider=provider)
    data["vault_id"] = backup_manifest["vault_id"]
    data["last_backup_id"] = backup_manifest["backup_id"]
    data["last_backup_parent_id"] = backup_manifest.get("parent_backup_id")
    data["last_backup_at"] = backup_manifest["created_at"]
    data["last_backup_archive_sha256"] = backup_manifest.get("archive_sha256")
    if remote_file_id:
        data["last_backup_file_id"] = remote_file_id
    if remote_manifest_file_id:
        data["last_manifest_file_id"] = remote_manifest_file_id
    data.pop("dirty", None)
    save_vault_metadata(data)
    return data


def mark_restore_succeeded(backup_manifest: dict) -> dict:
    metadata = ensure_vault_metadata()
    metadata["vault_id"] = backup_manifest["vault_id"]
    metadata["restored_from_backup_id"] = backup_manifest["backup_id"]
    metadata["restored_from_device_id"] = backup_manifest.get("device_id")
    metadata["restored_from_device_label"] = backup_manifest.get("device_label")
    metadata["last_restore_at"] = _utc_now()
    save_vault_metadata(metadata)
    return metadata


def build_backup_bundle() -> tuple[Path, dict]:
    metadata = ensure_vault_metadata()
    created_at = _utc_now()
    backup_id = str(uuid.uuid4())
    prefix = settings.google_drive.appdata_manifest_prefix

    with tempfile.TemporaryDirectory(prefix="vault-bundle-") as tmp:
        root = Path(tmp) / "vault"
        state_root = root / "state"
        state_root.mkdir(parents=True, exist_ok=True)

        app_data_root = state_root / "app_data"
        _copy_app_data(app_data_root)

        config_path = Path("config.yaml")
        if config_path.exists():
            shutil.copy2(config_path, state_root / "config.yaml")

        rules_path = Path("rules")
        if rules_path.exists():
            shutil.copytree(rules_path, state_root / "rules", dirs_exist_ok=True)

        payload = _build_payload_inventory(state_root)
        manifest = {
            "manifest_version": 2,
            "vault_id": metadata["vault_id"],
            "backup_id": backup_id,
            "parent_backup_id": metadata.get("last_backup_id"),
            "backup_type": "full",
            "device_id": metadata["device_id"],
            "device_label": metadata["device_label"],
            "created_at": created_at,
            "app_version": os.getenv("FINANCE_APP_VERSION", "dev"),
            "schema_version": 1,
            "payload": payload,
            "restore_scope": {
                "includes_app_data": True,
                "includes_rules": Path(state_root / "rules").exists(),
                "includes_config": Path(state_root / "config.yaml").exists(),
            },
        }
        (root / BACKUP_MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True))

        timestamp_slug = created_at.replace(":", "-")
        archive_name = f"{prefix}-{metadata['vault_id']}-{backup_id}-{timestamp_slug}.fvault"
        archive_path = Path(tempfile.gettempdir()) / archive_name
        if archive_path.exists():
            archive_path.unlink()
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(root.rglob("*")):
                if file_path.is_file():
                    zf.write(file_path, arcname=file_path.relative_to(root))

        manifest["archive_name"] = archive_name
        manifest["archive_sha256"] = _sha256(archive_path)
        return archive_path, manifest


def manifest_filename(vault_id: str) -> str:
    return f"{settings.google_drive.appdata_manifest_prefix}-{vault_id}-latest.manifest.json"


def restore_backup_bundle(archive_path: Path, manifest: dict | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="vault-restore-") as tmp:
        staging = Path(tmp) / "staging"
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(staging)

        local_manifest = json.loads((staging / BACKUP_MANIFEST_NAME).read_text())
        expected = manifest or local_manifest
        if local_manifest.get("backup_id") != expected.get("backup_id"):
            raise HTTPException(status_code=400, detail="Backup archive and manifest do not match")

        _verify_payload(staging / "state", local_manifest.get("payload") or [])
        summary = _validate_staged_restore(staging / "state")
        _promote_staged_restore(staging / "state")
        metadata = mark_restore_succeeded(local_manifest)
        _write_restore_receipt(local_manifest, summary, metadata)
        return {
            "status": "restored",
            "vault_id": local_manifest["vault_id"],
            "backup_id": local_manifest["backup_id"],
            "created_at": local_manifest["created_at"],
            "summary": summary,
        }


def _copy_app_data(dest: Path) -> None:
    source = Path(settings.app.data_dir)
    runtime_rel = Path(settings.app.runtime_dir).relative_to(source)
    dest.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return
    for file_path in sorted(source.rglob("*")):
        if not file_path.is_file():
            continue
        rel_path = file_path.relative_to(source)
        if rel_path == runtime_rel / LOCAL_METADATA_NAME:
            continue
        if rel_path == runtime_rel / RESTORE_RECEIPT_NAME:
            continue
        if runtime_rel in rel_path.parents and ".oauth" in rel_path.parts:
            continue
        # Skip WAL/shared-memory sidecars. _snapshot_sqlite() uses the sqlite3 backup API,
        # which yields a fully checkpointed standalone database; shipping a stale -wal
        # alongside it would let SQLite replay outdated pages over the snapshot on restore.
        if file_path.name.endswith(("-wal", "-shm", "-journal")):
            continue
        target = dest / rel_path
        if file_path.suffix == ".db":
            _snapshot_sqlite(file_path, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, target)


def _build_payload_inventory(state_root: Path) -> list[dict]:
    payload: list[dict] = []
    for file_path in sorted(state_root.rglob("*")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(state_root).as_posix()
        payload.append(
            {
                "role": _role_for_path(rel),
                "path": rel,
                "size_bytes": file_path.stat().st_size,
                "sha256": _sha256(file_path),
            }
        )
    return payload


def _role_for_path(rel_path: str) -> str:
    db_rel = Path(settings.database.path)
    try:
        db_rel = db_rel.relative_to(settings.app.data_dir)
    except ValueError:
        db_rel = Path(db_rel.name)
    if rel_path == f"app_data/{db_rel.as_posix()}":
        return "primary_db"
    if rel_path.startswith("app_data/") and rel_path.endswith(".db"):
        return "app_db"
    if rel_path.startswith("rules/"):
        return "rules"
    if rel_path == "config.yaml":
        return "config"
    return "state_file"


def _verify_payload(state_root: Path, payload: list[dict]) -> None:
    for item in payload:
        path = state_root / item["path"]
        if not path.exists():
            raise HTTPException(status_code=400, detail=f"Backup payload missing file: {item['path']}")
        if _sha256(path) != item["sha256"]:
            raise HTTPException(status_code=400, detail=f"Backup payload hash mismatch: {item['path']}")


def _validate_staged_restore(state_root: Path) -> dict:
    db_rel = Path(settings.database.path)
    try:
        db_rel = db_rel.relative_to(settings.app.data_dir)
    except ValueError:
        db_rel = Path(db_rel.name)
    db_path = state_root / "app_data" / db_rel
    if not db_path.exists():
        raise HTTPException(status_code=400, detail="Backup is missing the primary database")

    summary = {"files": 0, "transactions": None}
    for file_path in state_root.rglob("*"):
        if file_path.is_file():
            summary["files"] += 1

    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA integrity_check").fetchone()
            row = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()
            summary["transactions"] = int(row[0]) if row else 0
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Restored database validation failed: {exc}") from exc
    return summary


def _promote_staged_restore(state_root: Path) -> None:
    app_data = Path(settings.app.data_dir)
    config_path = Path("config.yaml")
    rules_path = Path("rules")
    staged_app_data = state_root / "app_data"
    staged_config = state_root / "config.yaml"
    staged_rules = state_root / "rules"

    with tempfile.TemporaryDirectory(prefix="vault-promote-") as rollback_tmp:
        rollback_root = Path(rollback_tmp)
        rollback_app_data = rollback_root / "app_data"
        rollback_config = rollback_root / "config.yaml"
        rollback_rules = rollback_root / "rules"
        try:
            if staged_app_data.exists():
                _replace_tree_contents(app_data, staged_app_data, rollback_app_data)

            if config_path.exists():
                rollback_config.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(config_path), str(rollback_config))
            if staged_config.exists():
                shutil.move(str(staged_config), str(config_path))

            if rules_path.exists():
                _replace_tree_contents(rules_path, staged_rules, rollback_rules)
            if staged_rules.exists():
                if not rules_path.exists():
                    shutil.copytree(staged_rules, rules_path, dirs_exist_ok=True)
        except Exception as exc:
            if rollback_app_data.exists():
                _replace_tree_contents(app_data, rollback_app_data, rollback_root / "failed_app_data")
            if rollback_config.exists():
                if config_path.exists():
                    config_path.unlink()
                shutil.move(str(rollback_config), str(config_path))
            if rollback_rules.exists():
                _replace_tree_contents(rules_path, rollback_rules, rollback_root / "failed_rules")
            raise HTTPException(status_code=500, detail=f"Restore promotion failed: {exc}") from exc


def _replace_tree_contents(target_root: Path, source_root: Path, rollback_root: Path) -> None:
    rollback_root.parent.mkdir(parents=True, exist_ok=True)
    if target_root.exists():
        if rollback_root.exists():
            shutil.rmtree(rollback_root)
        shutil.copytree(target_root, rollback_root, dirs_exist_ok=True)
        for child in target_root.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        target_root.mkdir(parents=True, exist_ok=True)

    for child in source_root.iterdir():
        dest = target_root / child.name
        if child.is_dir():
            shutil.copytree(child, dest, dirs_exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, dest)
    _make_tree_writable(target_root)


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for path in root.rglob("*"):
        try:
            if path.is_dir():
                path.chmod(0o775)
            else:
                path.chmod(0o664)
        except Exception:
            continue


def _write_restore_receipt(backup_manifest: dict, summary: dict, metadata: dict) -> None:
    receipt = {
        "restored_at": _utc_now(),
        "vault_id": backup_manifest["vault_id"],
        "backup_id": backup_manifest["backup_id"],
        "backup_created_at": backup_manifest["created_at"],
        "source_device_id": backup_manifest.get("device_id"),
        "source_device_label": backup_manifest.get("device_label"),
        "local_device_id": metadata.get("device_id"),
        "summary": summary,
    }
    path = restore_receipt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True))


def _default_device_label() -> str:
    return os.getenv("FINANCE_APP_DEVICE_LABEL") or os.uname().nodename


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_sqlite(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(dest) as dst:
        src.backup(dst)
