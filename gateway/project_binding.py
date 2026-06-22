"""
Project binding — resolve session working directories from channel→project mappings.

Kept in a separate module so that upstream merges touching ``gateway/run.py``
do not conflict with the fork's project-binding logic.  Import from here with
a single line; everything else is isolated in this file.
"""

import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


_PROJECT_BINDING_CACHE: dict[str, Any] = {
    "path": None,
    "mtime_ns": None,
    "data": None,
}


def _candidate_project_config_paths() -> list[Path]:
    """Return plausible agent-project mapping files in priority order."""
    candidates = [
        os.getenv("HERMES_PROJECT_CONFIG_PATH", "").strip(),
        "/etc/agents/projects.yaml",
        "/workspace/k3s-home-cluster/agents/projects.yaml",
    ]
    seen: set[str] = set()
    resolved: list[Path] = []
    for raw in candidates:
        if not raw:
            continue
        path = str(Path(raw).expanduser())
        if path in seen:
            continue
        seen.add(path)
        resolved.append(Path(path))
    return resolved


def _load_project_bindings() -> dict[str, Any]:
    """Load the room→project mapping from projects.yaml or a ConfigMap wrapper."""
    for path in _candidate_project_config_paths():
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.debug("project binding stat failed for %s: %s", path, exc)
            continue

        mtime_ns = getattr(stat, "st_mtime_ns", None) or int(stat.st_mtime * 1e9)
        cached_path = _PROJECT_BINDING_CACHE.get("path")
        cached_mtime = _PROJECT_BINDING_CACHE.get("mtime_ns")
        if cached_path == str(path) and cached_mtime == mtime_ns:
            cached = _PROJECT_BINDING_CACHE.get("data")
            if isinstance(cached, dict):
                return cached

        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("project binding load failed for %s: %s", path, exc)
            continue

        if isinstance(raw, dict):
            embedded = raw.get("data", {}).get("projects.yaml")
            if isinstance(embedded, str) and embedded.strip():
                try:
                    raw = yaml.safe_load(embedded) or {}
                except Exception as exc:
                    logger.warning(
                        "embedded project binding load failed for %s: %s", path, exc
                    )
                    continue

        if not isinstance(raw, dict):
            logger.warning(
                "project binding file %s did not parse to a mapping", path
            )
            continue

        _PROJECT_BINDING_CACHE.update(
            {
                "path": str(path),
                "mtime_ns": mtime_ns,
                "data": raw,
            }
        )
        return raw

    return {}


def resolve_project_binding_for_chat(chat_id: str) -> dict[str, Any]:
    """Return the first configured project binding for a messaging room/chat."""
    room_id = str(chat_id or "").strip()
    if not room_id:
        return {}
    config = _load_project_bindings()
    projects = config.get("projects") if isinstance(config, dict) else None
    if not isinstance(projects, dict):
        return {}
    for project_name, entry in projects.items():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("room") or "").strip() != room_id:
            continue
        resolved = dict(entry)
        resolved.setdefault("project", project_name)
        return resolved
    return {}


def resolve_project_cwd_for_chat(chat_id: str) -> str:
    """Resolve a session-specific cwd from the room→project binding table."""
    binding = resolve_project_binding_for_chat(chat_id)
    raw_cwd = str(binding.get("defaultCwd") or binding.get("path") or "").strip()
    if not raw_cwd:
        return ""
    cwd_path = Path(raw_cwd).expanduser()
    if cwd_path.is_dir():
        return str(cwd_path)
    logger.warning(
        "project binding cwd missing for chat %s (project=%s, cwd=%s)",
        chat_id,
        binding.get("project") or "",
        raw_cwd,
    )
    return ""
