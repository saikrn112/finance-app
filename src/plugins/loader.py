from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from typing import Any

from src.plugins.base import ParserPlugin

logger = logging.getLogger(__name__)

_registry: list[ParserPlugin] = []
_loaded: bool = False


def _plugin_dirs() -> list[Path]:
    """Return all directories to scan for plugin files."""
    dirs: list[Path] = []

    # Repo-level plugins/ directory
    repo_root = Path(__file__).resolve().parents[2]
    dirs.append(repo_root / "plugins")

    # User-level plugins directory
    user_dir = Path.home() / ".finance-app" / "plugins"
    if user_dir.is_dir():
        dirs.append(user_dir)

    # Environment variable override
    env_dir = os.environ.get("FINANCE_PLUGINS_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.is_dir():
            dirs.append(p)

    return dirs


def _load_plugin_file(path: Path) -> list[ParserPlugin]:
    """Load a single plugin file and call its register() function."""
    spec = importlib.util.spec_from_file_location(f"plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        logger.warning("Could not create module spec for %s", path)
        return []

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        logger.warning("Failed to load plugin %s: %s", path.name, exc)
        return []

    register_fn = getattr(module, "register", None)
    if register_fn is None:
        logger.warning("Plugin %s has no register() function, skipping", path.name)
        return []

    try:
        result = register_fn()
    except Exception as exc:
        logger.warning("Plugin %s register() raised: %s", path.name, exc)
        return []

    if isinstance(result, ParserPlugin):
        return [result]
    if isinstance(result, list):
        return [p for p in result if isinstance(p, ParserPlugin)]

    logger.warning("Plugin %s register() returned unexpected type: %s", path.name, type(result))
    return []


def load_plugins() -> list[ParserPlugin]:
    """Discover and load all plugins. Idempotent — only runs once."""
    global _registry, _loaded
    if _loaded:
        return _registry

    plugins: list[ParserPlugin] = []
    for plugin_dir in _plugin_dirs():
        if not plugin_dir.is_dir():
            continue
        for py_file in sorted(plugin_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            loaded = _load_plugin_file(py_file)
            if loaded:
                logger.info("Loaded %d plugin(s) from %s", len(loaded), py_file.name)
                plugins.extend(loaded)

    _registry = plugins
    _loaded = True
    logger.info("Plugin loading complete: %d plugins registered", len(_registry))
    return _registry


def get_registry() -> list[ParserPlugin]:
    """Return the current registry (loads if not yet loaded)."""
    if not _loaded:
        load_plugins()
    return _registry


def reset_registry() -> None:
    """Reset the plugin registry (for testing)."""
    global _registry, _loaded
    _registry = []
    _loaded = False
