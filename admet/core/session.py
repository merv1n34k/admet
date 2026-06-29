from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_EXTENSION = ".admetp"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SessionFile:
    id: str
    path: str
    role: str
    media_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionCache:
    id: str
    path: str
    engine: str
    file_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionItem:
    id: str
    project_type: str
    engine: str
    settings: dict[str, Any] = field(default_factory=dict)
    files: tuple[str, ...] = ()
    caches: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdmetSession:
    project_id: str
    project_type: str
    files: tuple[SessionFile, ...] = ()
    caches: tuple[SessionCache, ...] = ()
    items: tuple[SessionItem, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdmetSession:
        schema_version = data.get("schema_version")
        if schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported .admetp schema version: {schema_version!r}")
        session = cls(
            project_id=data["project_id"],
            project_type=data["project_type"],
            files=tuple(SessionFile(**item) for item in data.get("files", ())),
            caches=tuple(SessionCache(**item) for item in data.get("caches", ())),
            items=tuple(
                SessionItem(
                    **{
                        **item,
                        "files": tuple(item.get("files", ())),
                        "caches": tuple(item.get("caches", ())),
                    }
                )
                for item in data.get("items", ())
            ),
            metadata=dict(data.get("metadata", {})),
            schema_version=schema_version,
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
    target.parent.mkdir(parents=True, exist_ok=True)
    session = session.touch()
    validate_session(session)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(session.to_dict(), handle, indent=2, sort_keys=True)
    return target


def load_session(path: str | Path) -> AdmetSession:
    with session_path(path).open("r", encoding="utf-8") as handle:
        return AdmetSession.from_dict(json.load(handle))


def session_path(path: str | Path) -> Path:
    target = Path(path)
    if target.suffix != PROJECT_EXTENSION:
        target = target.with_suffix(PROJECT_EXTENSION)
    return target


def resolve_session_path(project_path: str | Path, stored_path: str) -> Path:
    path = Path(stored_path)
    if path.is_absolute():
        return path
    return session_path(project_path).parent / path


def validate_session(session: AdmetSession) -> None:
    if not session.project_id:
        raise ValueError("project_id is required")
    if not session.project_type:
        raise ValueError("project_type is required")

    file_ids = _unique_ids("files", (item.id for item in session.files))
    cache_ids = _unique_ids("caches", (item.id for item in session.caches))
    _unique_ids("items", (item.id for item in session.items))

    for cache in session.caches:
        if cache.file_id and cache.file_id not in file_ids:
            raise ValueError(f"cache {cache.id!r} references unknown file {cache.file_id!r}")

    for item in session.items:
        missing_files = set(item.files) - file_ids
        if missing_files:
            raise ValueError(f"item {item.id!r} references unknown files: {sorted(missing_files)!r}")
        missing_caches = set(item.caches) - cache_ids
        if missing_caches:
            raise ValueError(f"item {item.id!r} references unknown caches: {sorted(missing_caches)!r}")


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
