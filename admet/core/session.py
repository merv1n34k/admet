from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_EXTENSION = ".admetp"
MANIFEST_FILENAME = "manifest.json"


@dataclass(frozen=True)
class SessionFile:
    id: str
    path: str
    role: str
    media_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionItem:
    id: str
    project_type: str
    engine: str
    settings: dict[str, Any] = field(default_factory=dict)
    files: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdmetSession:
    project_id: str
    project_type: str
    files: tuple[SessionFile, ...] = ()
    items: tuple[SessionItem, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdmetSession:
        session = cls(
            project_id=str(data.get("project_id") or data.get("id") or "project"),
            project_type=str(data.get("project_type") or "combined"),
            files=tuple(
                _session_file_from_dict(item, index)
                for index, item in enumerate(data.get("files", ()), start=1)
            ),
            items=tuple(
                _session_item_from_dict(item, index)
                for index, item in enumerate(data.get("items", ()), start=1)
            ),
            metadata=dict(data.get("metadata", {})),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )
        validate_session(session)
        return session

    def touch(self) -> AdmetSession:
        now = _now()
        created_at = self.created_at or now
        return replace(self, created_at=created_at, updated_at=now)


def new_session(
    project_id: str,
    project_type: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> AdmetSession:
    now = _now()
    return AdmetSession(
        project_id=project_id,
        project_type=project_type,
        metadata=dict(metadata or {}),
        created_at=now,
        updated_at=now,
    )


def save_session(path: str | Path, session: AdmetSession) -> Path:
    target = session_path(path)
    target.mkdir(parents=True, exist_ok=True)
    (target / "records").mkdir(exist_ok=True)
    session = session.touch()
    session = _relativize_session_paths(session, target)
    validate_session(session)
    with _manifest_path(target).open("w", encoding="utf-8") as handle:
        json.dump(session.to_dict(), handle, indent=2, sort_keys=True)
    return target


def load_session(path: str | Path) -> AdmetSession:
    with _manifest_path(path).open("r", encoding="utf-8") as handle:
        return AdmetSession.from_dict(json.load(handle))


def session_path(path: str | Path) -> Path:
    target = Path(path)
    if target.name == MANIFEST_FILENAME:
        target = target.parent
    if target.suffix != PROJECT_EXTENSION:
        target = target.with_suffix(PROJECT_EXTENSION)
    return target


def resolve_session_path(
    project_path: str | Path,
    stored_path: str,
    *,
    media_root: str | Path | None = None,
) -> Path:
    path = Path(stored_path)
    if path.is_absolute():
        return path
    root = Path(media_root) if media_root is not None else session_path(project_path)
    return root / path


def missing_files(
    session: AdmetSession,
    project_path: str | Path,
    *,
    media_root: str | Path | None = None,
) -> list[tuple[SessionFile, Path]]:
    missing: list[tuple[SessionFile, Path]] = []
    for file in session.files:
        resolved = resolve_session_path(project_path, file.path, media_root=media_root)
        if not resolved.exists():
            missing.append((file, resolved))
    return missing


def content_cache_key(file_path: str | Path, settings: dict[str, Any] | None = None) -> str:
    digest = hashlib.sha256()
    with Path(file_path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    digest.update(b"\0")
    digest.update(json.dumps(settings or {}, sort_keys=True, default=str).encode("utf-8"))
    return digest.hexdigest()


def validate_session(session: AdmetSession) -> None:
    if not session.project_id:
        raise ValueError("project_id is required")
    if not session.project_type:
        raise ValueError("project_type is required")

    file_ids = _unique_ids("files", (item.id for item in session.files))
    _unique_ids("items", (item.id for item in session.items))

    for item in session.items:
        missing_files = set(item.files) - file_ids
        if missing_files:
            raise ValueError(f"item {item.id!r} references unknown files: {sorted(missing_files)!r}")


def _manifest_path(path: str | Path) -> Path:
    return session_path(path) / MANIFEST_FILENAME


def _session_file_from_dict(data: dict[str, Any], index: int) -> SessionFile:
    return SessionFile(
        id=str(data.get("id") or f"file-{index}"),
        path=str(data.get("path") or ""),
        role=str(data.get("role") or "media"),
        media_type=str(data.get("media_type") or ""),
        metadata=dict(data.get("metadata", {})),
    )


def _session_item_from_dict(data: dict[str, Any], index: int) -> SessionItem:
    return SessionItem(
        id=str(data.get("id") or f"item-{index}"),
        project_type=str(data.get("project_type") or "combined"),
        engine=str(data.get("engine") or ""),
        settings=dict(data.get("settings", {})),
        files=tuple(data.get("files", ())),
        metadata=dict(data.get("metadata", {})),
    )


def _relativize_session_paths(session: AdmetSession, root: Path) -> AdmetSession:
    root = root.resolve()
    return replace(
        session,
        files=tuple(
            replace(file, path=_relativize_path(file.path, root, metadata=file.metadata))
            for file in session.files
        ),
    )


def _relativize_path(path: str, root: Path, *, metadata: dict[str, Any]) -> str:
    if not path:
        return path
    stored = Path(path)
    if not stored.is_absolute():
        return stored.as_posix()
    try:
        return stored.resolve().relative_to(root).as_posix()
    except ValueError:
        if _allows_external_path(metadata):
            return str(stored)
        raise ValueError(
            f"absolute path outside project bundle requires metadata.external=true: {path}"
        ) from None


def _allows_external_path(metadata: dict[str, Any]) -> bool:
    return bool(
        metadata.get("external")
        or metadata.get("external_media")
        or metadata.get("nas")
    )


def _unique_ids(label: str, values) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if not value:
            raise ValueError(f"{label} entries require ids")
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        raise ValueError(f"duplicate {label} ids: {sorted(duplicates)!r}")
    return seen


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
