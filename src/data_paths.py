from __future__ import annotations

from pathlib import Path

from src.config import settings


DATA_ROOT = Path(settings.app.data_dir)
RAW_ROOT = DATA_ROOT / "raw"
DERIVED_ROOT = DATA_ROOT / "derived"
PARSED_ROOT = DERIVED_ROOT / "parsed"
MANIFESTS_ROOT = DERIVED_ROOT / "manifests"
PREVIEW_WORKSPACE_ROOT = DERIVED_ROOT / "preview_workspace"
RUNTIME_ROOT = Path(settings.app.runtime_dir)


def raw_source_dir(source_key: str) -> Path:
    from src.plugins.registry import get_source_raw_locations

    locations = get_source_raw_locations()
    if source_key not in locations:
        raise KeyError(f"Unknown source_key '{source_key}'")
    domain, dirname = locations[source_key]
    return RAW_ROOT / domain / dirname


def import_manifest_path(import_id: str) -> Path:
    return MANIFESTS_ROOT / f"{import_id}.json"


def import_preview_dir(import_id: str) -> Path:
    return PREVIEW_WORKSPACE_ROOT / import_id

