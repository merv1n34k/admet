"""What system checks have already been run, and when.

A check describes the rig, not the sample, so its history is not confined to the
project that happened to be open when it was taken. Every project under the
discovery root is scanned -- the same manifests the project picker already reads
-- and the newest check of each kind is what the cadence is judged against.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from admet.core.discovery import projects_root
from admet.core.session import MANIFEST_FILENAME, PROJECT_EXTENSION

# A check is expected weekly. Chips, tubing and liquids all drift on their own
# schedule, but a week is short enough that a change is still traceable to what
# was done in between.
CHECK_INTERVAL_DAYS = 7

CHECK_ROLE = "system_check"


@dataclass(frozen=True)
class CheckRecord:
    """One stored check, as the manifest describes it."""

    kind: str
    recorded_at: str
    summary: str
    project_id: str
    path: Path

    @property
    def moment(self) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(self.recorded_at)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def age_days(self, now: datetime) -> float | None:
        moment = self.moment
        if moment is None:
            return None
        return (now - moment).total_seconds() / 86400.0

    def is_due(self, now: datetime, interval_days: int = CHECK_INTERVAL_DAYS) -> bool:
        """True when this check is old enough that another is owed.

        A record whose timestamp cannot be read counts as due: an unreadable date
        is not evidence that the rig was checked recently.
        """
        moment = self.moment
        if moment is None:
            return True
        return now - moment >= timedelta(days=interval_days)


def discover_checks(root: str | Path | None = None) -> tuple[CheckRecord, ...]:
    """Every stored check under the projects root, newest first."""
    base = projects_root(root)
    if not base.is_dir():
        return ()
    records: list[CheckRecord] = []
    for project in sorted(base.glob(f"*{PROJECT_EXTENSION}")):
        records.extend(_checks_in_project(project))
    return tuple(sorted(records, key=lambda record: record.recorded_at, reverse=True))


def latest_check(records: tuple[CheckRecord, ...], kind: str) -> CheckRecord | None:
    return next((record for record in records if record.kind == kind), None)


def load_check(path: str | Path) -> dict[str, Any]:
    """Read one stored check back. Returns an empty dict if it cannot be read."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _checks_in_project(project: Path) -> list[CheckRecord]:
    manifest = project / MANIFEST_FILENAME
    if not manifest.is_file():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []

    project_id = str(data.get("project_id") or project.stem)
    records = []
    for file in data.get("files", ()):
        if not isinstance(file, dict) or file.get("role") != CHECK_ROLE:
            continue
        metadata = file.get("metadata") if isinstance(file.get("metadata"), dict) else {}
        stored = Path(str(file.get("path") or ""))
        records.append(
            CheckRecord(
                kind=str(metadata.get("kind") or ""),
                recorded_at=str(metadata.get("recorded_at") or ""),
                summary=str(metadata.get("summary") or ""),
                project_id=project_id,
                path=stored if stored.is_absolute() else project / stored,
            )
        )
    return records
