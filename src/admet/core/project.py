from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any


from .session import (
    AdmetSession,
    SessionFile,
    SessionItem,
    load_session,
    new_session,
    save_session,
    session_path,
    write_atomic,
)


@dataclass(frozen=True)
class ControlRecordingTarget:
    recording_id: str
    video_path: Path
    fluidics_csv_path: Path

    @property
    def outputs(self) -> dict[str, Path]:
        return {"video": self.video_path, "fluidics_csv": self.fluidics_csv_path}


@dataclass(frozen=True)
class AnalysisRunTarget:
    run_id: str
    run_dir: Path
    raw_path: Path
    run_metadata_path: Path


class ProjectStore:
    def __init__(self, project_path: str | Path, session: AdmetSession | None = None):
        self.path = session_path(project_path)
        self.session = session if session is not None else load_session(self.path)

    @classmethod
    def create(
        cls,
        project_path: str | Path,
        project_id: str,
    ) -> ProjectStore:
        # Like touch: an existing project is opened as it is, never replaced.
        existing = session_path(project_path)
        if (existing / "manifest.json").is_file():
            return cls(existing)
        path = save_session(project_path, new_session(project_id))
        return cls(path)

    def save(self) -> None:
        self.path = save_session(self.path, self.session)

    def update_metadata(self, **metadata: Any) -> None:
        self.session = replace(
            self.session,
            metadata={**self.session.metadata, **{key: value for key, value in metadata.items() if value is not None}},
        )
        self.save()

    def upsert_file_path(
        self,
        source_path: str | Path,
        *,
        role: str,
        media_type: str = "",
        metadata: dict[str, Any] | None = None,
        id_hint: str = "",
    ) -> SessionFile:
        resolved = Path(source_path).resolve()
        stored_path = self._stored_path(resolved)
        file_metadata = dict(metadata or {})
        if Path(stored_path).is_absolute():
            file_metadata.setdefault("external", True)

        existing = next((file for file in self.session.files if file.path == stored_path), None)
        file = SessionFile(
            id=existing.id if existing is not None else _unique_file_id(_safe_id(id_hint or resolved.stem), self.session.files),
            path=stored_path,
            role=role,
            media_type=media_type,
            metadata=file_metadata,
        )
        files = _upsert_file(list(self.session.files), file)
        self.session = replace(self.session, files=tuple(files))
        return file

    def files_by_role(self, roles: set[str] | frozenset[str] | tuple[str, ...]) -> tuple[SessionFile, ...]:
        role_set = set(roles)
        return tuple(file for file in self.session.files if file.role in role_set)

    def resolve_file_path(self, file: SessionFile) -> Path:
        path = Path(file.path)
        return path if path.is_absolute() else self.path / path

    @property
    def records_dir(self) -> Path:
        return self.path / "records"

    @property
    def analysis_dir(self) -> Path:
        return self.path / "analysis"

    def control_recording_target(self, label: str) -> ControlRecordingTarget:
        recording_id = _stamped_id(label)
        return ControlRecordingTarget(
            recording_id=recording_id,
            video_path=self.records_dir / "camera" / f"{recording_id}.avi",
            fluidics_csv_path=self.records_dir / "fluidics" / f"{recording_id}.csv",
        )

    def append_control_recording(self, recording: dict[str, Any]) -> None:
        normalized = self._normalize_control_recording(recording)
        metadata_path = self.records_dir / "metadata.json"
        metadata = _read_metadata(metadata_path, default={"recordings": []})
        recordings = [item for item in metadata.get("recordings", []) if isinstance(item, dict)]
        recordings = _upsert_recording(recordings, normalized)
        metadata.update(
            {
                "updated": _local_timestamp(),
                "report_dir": "records",
                "recording_count": len(recordings),
                "recordings": recordings,
            }
        )
        _write_metadata(metadata_path, metadata)
        self._register_control_recording(normalized)
        self.save()

    def append_system_check(
        self,
        snapshot: dict[str, Any],
        *,
        summary: str = "",
        check_id: str = "",
    ) -> Path:
        """Store one system check as its own JSON record under records/checks.

        Checks accumulate rather than overwrite: what matters is comparing today's
        chip against the last one, so each run keeps its own stamped file and the
        manifest lists them all under a single item.

        A run writes twice -- once as it starts, carrying everything entered, and
        once as it ends, carrying what was measured. Passing the id returned by
        the first write lets the second replace it in place, so one run is one
        record whether or not it ever reached the end.
        """
        kind = str(snapshot.get("kind") or "check").strip() or "check"
        if check_id:
            path = self.records_dir / "checks" / f"{check_id}.json"
        else:
            # Stamps are one a second, and a run restarted straight away would
            # otherwise take the name of the one it was restarted from.
            check_id = _stamped_id(f"check-{kind}")
            path = self.records_dir / "checks" / f"{check_id}.json"
            attempt = 2
            while path.exists():
                path = self.records_dir / "checks" / f"{check_id}-{attempt}.json"
                attempt += 1
            check_id = path.stem
        _write_metadata(path, {**snapshot, "check_id": check_id})

        file = self.upsert_file_path(
            path,
            role="system_check",
            media_type="application/json",
            metadata={
                "check_id": check_id,
                "kind": kind,
                "recorded_at": str(snapshot.get("recorded_at") or ""),
                "summary": summary,
            },
            id_hint=check_id,
        )
        existing = _find_item(self.session.items, "system-checks")
        item_files = list(existing.files if existing else ())
        if file.id not in item_files:
            item_files.append(file.id)
        item = SessionItem(
            id="system-checks",
            project_type="system_check",
            engine="acquisition",
            files=tuple(item_files),
            metadata={
                "check_count": len(item_files),
                "report_dir": "records/checks",
                "latest": check_id,
                "latest_summary": summary,
            },
        )
        self.session = replace(
            self.session, items=tuple(_upsert_item(list(self.session.items), item))
        )
        self.save()
        return path

    def analysis_run_target(self, label: str = "analysis") -> AnalysisRunTarget:
        # A project keeps a single, stable analysis output: one raw.jsonl and one
        # run.json in the analysis dir. Runs are idempotent -- re-running rewrites
        # this output in place instead of stacking timestamped run directories.
        run_dir = self.analysis_dir
        return AnalysisRunTarget(
            run_id="analysis",
            run_dir=run_dir,
            raw_path=run_dir / "raw.jsonl",
            run_metadata_path=run_dir / "run.json",
        )

    def register_analysis_file(
        self,
        source_path: str | Path,
        *,
        engine: str,
        sample_id: str = "",
    ) -> SessionFile:
        stored_path = self._stored_path(Path(source_path).resolve())
        metadata: dict[str, Any] = {
            "engine": engine,
            "sample_id": sample_id or Path(source_path).stem,
            "source_path": stored_path,
        }
        file = self.upsert_file_path(
            source_path,
            role="analysis_video" if engine == "opencv" else "analysis_image_dir",
            media_type=_analysis_media_type(Path(source_path), engine),
            metadata=metadata,
            id_hint=f"analysis-{engine}-{Path(source_path).stem}",
        )
        return file

    def finish_analysis_run(
        self,
        target: AnalysisRunTarget,
        *,
        files: tuple[str, ...],
        settings: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        run_metadata = {
            "run_id": target.run_id,
            "updated": _local_timestamp(),
            "raw_path": self._stored_path(target.raw_path),
            "files": list(files),
            "settings": settings,
            "metadata": metadata,
        }
        _write_metadata(target.run_metadata_path, run_metadata)
        analysis_metadata_path = self.analysis_dir / "metadata.json"
        analysis_metadata = _read_metadata(analysis_metadata_path, default={"runs": []})
        runs = [item for item in analysis_metadata.get("runs", []) if isinstance(item, dict)]
        runs = _upsert_run(runs, run_metadata)
        analysis_metadata.update(
            {
                "updated": _local_timestamp(),
                "run_count": len(runs),
                "runs": runs,
            }
        )
        _write_metadata(analysis_metadata_path, analysis_metadata)
        item = SessionItem(
            id=target.run_id,
            project_type="analysis_run",
            engine="analyze",
            settings=settings,
            files=files,
            metadata={
                "run_id": target.run_id,
                "run_path": self._stored_path(target.run_dir),
                "raw_path": self._stored_path(target.raw_path),
                **metadata,
            },
        )
        items = _upsert_item(list(self.session.items), item)
        session_metadata = dict(self.session.metadata)
        if metadata.get("cache_root"):
            session_metadata["cache_root"] = str(metadata["cache_root"])
        self.session = replace(self.session, items=tuple(items), metadata=session_metadata)
        self.save()

    def _normalize_control_recording(self, recording: dict[str, Any]) -> dict[str, Any]:
        recording_id = str(recording.get("recording_id") or "").strip()
        if not recording_id:
            raise ValueError("control recording requires recording_id")
        video_path = Path(str(recording.get("video_path") or self.records_dir / "camera" / f"{recording_id}.avi"))
        fluidics_csv = Path(str(recording.get("fluidics_csv") or self.records_dir / "fluidics" / f"{recording_id}.csv"))
        if not video_path.is_absolute():
            video_path = self.path / video_path
        if not fluidics_csv.is_absolute():
            fluidics_csv = self.path / fluidics_csv
        # A path is only recorded for a file that is actually there. The
        # manifest already refuses to list one that is not, but leaving the
        # path here would still be a claim a reader could believe -- and a
        # fluidics-only run writes no video at all.
        wrote_video = video_path.is_file()
        normalized = dict(recording)
        normalized.update(
            {
                "recording_id": recording_id,
                "report_dir": "records",
                "output_dir": "records/camera",
                "video_path": self._stored_path(video_path) if wrote_video else "",
                "video_candidates": [self._stored_path(video_path)] if wrote_video else [],
                "fluidics_csv": self._stored_path(fluidics_csv),
            }
        )
        return normalized

    def _register_control_recording(self, recording: dict[str, Any]) -> None:
        """List what the recording actually produced.

        A recording without a camera writes a fluidics log and no video. Both
        paths are allocated either way, so registering both unconditionally puts
        a file in the manifest that was never written -- and a manifest that
        claims a file exists is worse than one that omits it, because everything
        downstream believes it.
        """
        files = list(self.session.files)
        recording_id = _safe_id(str(recording["recording_id"]))
        written: list[str] = []
        for key, prefix, role, media_type, describe in (
            ("video_path", "video-", "control_video", "video/avi", _video_metadata),
            ("fluidics_csv", "fluidics-", "control_fluidics_csv", "text/csv", _csv_metadata),
        ):
            stored = str(recording.get(key) or "")
            if not stored or not self.resolve_file_path(SessionFile("probe", stored, role)).is_file():
                continue
            file_id = prefix + recording_id
            written.append(file_id)
            files = _upsert_file(
                files,
                SessionFile(
                    id=file_id,
                    path=stored,
                    role=role,
                    media_type=media_type,
                    metadata=describe(recording),
                ),
            )
        if not written:
            return

        existing = _find_item(self.session.items, "acq-records")
        item_files = list(existing.files if existing else ())
        for file_id in written:
            if file_id not in item_files:
                item_files.append(file_id)
        item = SessionItem(
            id="acq-records",
            project_type="control_acquisition",
            engine="acquisition",
            files=tuple(item_files),
            metadata={
                "recording_count": len({name.split("-", 1)[1] for name in item_files}),
                "report_dir": "records",
            },
        )
        items = _upsert_item(list(self.session.items), item)
        self.session = replace(self.session, files=tuple(files), items=tuple(items))

    def _stored_path(self, path: Path) -> str:
        path = path.resolve()
        try:
            return path.relative_to(self.path.resolve()).as_posix()
        except ValueError:
            return str(path)


def _stamped_id(label: str) -> str:
    return f"{_safe_label(label)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _safe_label(value: str) -> str:
    label = "_".join(
        part for part in "".join(ch.lower() if ch.isalnum() else "_" for ch in value).split("_") if part
    )
    return label or "run"


def _safe_id(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-") or "item"


def _local_timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_metadata(path: Path, *, default: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return dict(default)
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else dict(default)


def _write_metadata(path: Path, data: dict[str, Any]) -> None:
    write_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _upsert_recording(recordings: list[dict[str, Any]], recording: dict[str, Any]) -> list[dict[str, Any]]:
    recording_id = recording["recording_id"]
    for index, existing in enumerate(recordings):
        if existing.get("recording_id") == recording_id:
            recordings[index] = recording
            return recordings
    recordings.append(recording)
    return recordings


def _upsert_run(runs: list[dict[str, Any]], run: dict[str, Any]) -> list[dict[str, Any]]:
    run_id = run["run_id"]
    for index, existing in enumerate(runs):
        if existing.get("run_id") == run_id:
            runs[index] = run
            return runs
    runs.append(run)
    return runs


def _upsert_file(files: list[SessionFile], stored: SessionFile) -> list[SessionFile]:
    for index, existing in enumerate(files):
        if existing.id == stored.id or existing.path == stored.path:
            files[index] = stored
            return files
    files.append(stored)
    return files


def _unique_file_id(base: str, files: tuple[SessionFile, ...]) -> str:
    existing = {file.id for file in files}
    candidate = base
    index = 2
    while candidate in existing:
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _analysis_media_type(path: Path, engine: str) -> str:
    if engine == "cellpose" or path.is_dir():
        return "inode/directory"
    if path.suffix.lower() == ".avi":
        return "video/avi"
    return "video"


def _upsert_item(items: list[SessionItem], stored: SessionItem) -> list[SessionItem]:
    for index, existing in enumerate(items):
        if existing.id == stored.id:
            items[index] = stored
            return items
    items.append(stored)
    return items


def _find_item(items: tuple[SessionItem, ...], item_id: str) -> SessionItem | None:
    for item in items:
        if item.id == item_id:
            return item
    return None


def _video_metadata(recording: dict[str, Any]) -> dict[str, Any]:
    width = int(float(recording.get("width") or 0))
    height = int(float(recording.get("height") or 0))
    return {
        "video_path": str(recording.get("video_path") or ""),
        "started_at": str(recording.get("started_at") or ""),
        "stopped_at": str(recording.get("stopped_at") or ""),
        "duration_s": float(recording.get("duration_s") or 0.0),
        "frames_recorded": recording.get("frames_recorded"),
        "frames_written": recording.get("frames_written"),
        "width": width,
        "height": height,
        "dimensions": f"{width}x{height}" if width and height else "",
        "acquisition_fps": float(recording.get("acquisition_fps") or 0.0),
        "converted_fps": float(recording.get("converted_fps") or 0.0),
        "fluidics_csv": str(recording.get("fluidics_csv") or ""),
    }


def _csv_metadata(recording: dict[str, Any]) -> dict[str, Any]:
    return {
        "fluidics_csv": str(recording.get("fluidics_csv") or ""),
        "recording_id": str(recording.get("recording_id") or ""),
        "started_at": str(recording.get("started_at") or ""),
        "stopped_at": str(recording.get("stopped_at") or ""),
        "duration_s": float(recording.get("duration_s") or 0.0),
    }
