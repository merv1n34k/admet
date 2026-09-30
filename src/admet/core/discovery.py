from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
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
    last_opened: str = ""


def project_ref_label(ref: ProjectRef) -> str:
    """One-line description of a project, shared by the analyze and control pickers."""
    try:
        opened = datetime.fromisoformat(ref.last_opened).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        opened = "—"
    return (
        f"{ref.project_id} · {ref.run_count} runs · last opened: {opened}"
    )


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


def prune_recent_projects(history) -> dict[str, str]:
    if not isinstance(history, dict):
        return {}
    recent = {}
    for path, opened in history.items():
        if not isinstance(path, str) or not isinstance(opened, str):
            continue
        try:
            project = Path(path).expanduser().resolve()
            if (project / MANIFEST_FILENAME).is_file():
                recent[str(project)] = opened
        except (OSError, ValueError):
            continue
    return recent


def discover_projects(root: str | Path | None = None, *, recent=None) -> list[ProjectRef]:
    base = projects_root(root)
    recent = prune_recent_projects(recent)
    paths = {Path(path) for path in recent}
    if base.is_dir():
        paths.update(child.resolve() for child in base.glob(f"*{PROJECT_EXTENSION}"))
    refs = []
    for child in sorted(paths):
        manifest = child / MANIFEST_FILENAME
        if not manifest.is_file():
            continue
        refs.append(_ref_from_manifest(child, manifest, recent.get(str(child), "")))
    return sorted(refs, key=lambda ref: (ref.last_opened, ref.updated), reverse=True)


def _ref_from_manifest(project_path: Path, manifest: Path, last_opened="") -> ProjectRef:
    data = _read_json(manifest)
    return ProjectRef(
        path=project_path.resolve(),
        project_id=str(data.get("project_id") or project_path.stem),
        updated=str(data.get("updated_at") or ""),
        recording_count=_recording_count(project_path, data),
        run_count=_run_count(project_path, data),
        file_count=_file_count(data),
        last_opened=last_opened,
    )


def _file_count(manifest: dict[str, Any]) -> int:
    return len({
        file["path"]
        for file in manifest.get("files", ())
        if isinstance(file, dict) and isinstance(file.get("path"), str) and file["path"]
    })


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
    protocol_count = sum(1 for path in (project_path / "records" / "protocols").glob("*/summary.json") if path.is_file())
    metadata = _read_json(project_path / "analysis" / "metadata.json")
    count = _int_value(metadata.get("run_count"))
    if not count:
        count = sum(1 for item in _manifest_items(manifest) if item.get("project_type") == "analysis_run")
    return protocol_count + count


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
