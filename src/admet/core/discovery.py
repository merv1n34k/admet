from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from admet.core.session import MANIFEST_FILENAME, PROJECT_EXTENSION


ENV_ROOT = "ADMET_PROJECTS_ROOT"


@dataclass(frozen=True)
class ProjectRef:
    path: Path
    project_id: str
    updated: str
    recording_count: int
    run_count: int
    file_count: int = 0


def projects_root(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get(ENV_ROOT)
    if env:
        return Path(env).expanduser().resolve()
    project_dir = Path.cwd() / "projects"
    if project_dir.is_dir():
        return project_dir.resolve()
    return Path.cwd()


def discover_projects(root: str | Path | None = None) -> list[ProjectRef]:
    base = projects_root(root)
    if not base.is_dir():
        return []
    refs = []
    for child in sorted(base.glob(f"*{PROJECT_EXTENSION}")):
        manifest = child / MANIFEST_FILENAME
        if not manifest.is_file():
            continue
        refs.append(_ref_from_manifest(child, manifest))
    return sorted(refs, key=lambda ref: ref.updated, reverse=True)


def _ref_from_manifest(project_path: Path, manifest: Path) -> ProjectRef:
    data = _read_json(manifest)
    return ProjectRef(
        path=project_path.resolve(),
        project_id=str(data.get("project_id") or project_path.stem),
        updated=str(data.get("updated_at") or ""),
        recording_count=_recording_count(project_path, data),
        run_count=_run_count(project_path, data),
        file_count=_file_count(data),
    )


def _file_count(manifest: dict[str, Any]) -> int:
    roles = {"analysis_video", "analysis_image_dir"}
    return sum(
        1
        for file in manifest.get("files", ())
        if isinstance(file, dict) and file.get("role") in roles
    )


def _recording_count(project_path: Path, manifest: dict[str, Any]) -> int:
    metadata = _read_json(project_path / "records" / "metadata.json")
    count = _int_value(metadata.get("recording_count"))
    if count:
        return count
    for item in _manifest_items(manifest):
        if item.get("id") == "acq-records":
            item_metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            return _int_value(item_metadata.get("recording_count"))
    return 0


def _run_count(project_path: Path, manifest: dict[str, Any]) -> int:
    metadata = _read_json(project_path / "analysis" / "metadata.json")
    count = _int_value(metadata.get("run_count"))
    if count:
        return count
    return sum(1 for item in _manifest_items(manifest) if item.get("project_type") == "analysis_run")


def _manifest_items(manifest: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return tuple(item for item in manifest.get("items", ()) if isinstance(item, dict))


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
