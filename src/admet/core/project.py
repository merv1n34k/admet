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
        project_type: str = "combined",
    ) -> ProjectStore:
        path = save_session(project_path, new_session(project_id, project_type))
        return cls(path)

    def save(self) -> None:
        self.path = save_session(self.path, self.session)

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

    def analysis_run_target(self, label: str = "analysis") -> AnalysisRunTarget:
        run_id = _stamped_id(label)
        run_dir = self.analysis_dir / "runs" / run_id
        return AnalysisRunTarget(
            run_id=run_id,
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
        resolved = Path(source_path).resolve()
        stored_path = self._stored_path(resolved)
        for file in self.session.files:
            if file.path == stored_path:
                return file

        metadata: dict[str, Any] = {
            "engine": engine,
            "sample_id": sample_id or resolved.stem,
            "source_path": stored_path,
        }
        if Path(stored_path).is_absolute():
            metadata["external"] = True

        file_id = _unique_file_id(
            f"analysis-{engine}-{_safe_id(resolved.stem)}",
            self.session.files,
        )
        file = SessionFile(
            id=file_id,
            path=stored_path,
            role="analysis_video" if engine == "opencv" else "analysis_image_dir",
            media_type=_analysis_media_type(resolved, engine),
            metadata=metadata,
        )
        files = _upsert_file(list(self.session.files), file)
        self.session = replace(self.session, files=tuple(files))
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
        self.session = replace(self.session, items=tuple(items))
        self.save()

    def _normalize_control_recording(self, recording: dict[str, Any]) -> dict[str, Any]:
        recording_id = str(recording.get("recording_id") or recording.get("video_prefix") or "").strip()
        if not recording_id:
            raise ValueError("control recording requires recording_id")
        video_path = Path(str(recording.get("video_path") or self.records_dir / "camera" / f"{recording_id}.avi"))
        fluidics_csv = Path(str(recording.get("fluidics_csv") or self.records_dir / "fluidics" / f"{recording_id}.csv"))
        if not video_path.is_absolute():
            video_path = self.path / video_path
        if not fluidics_csv.is_absolute():
            fluidics_csv = self.path / fluidics_csv
        normalized = dict(recording)
        normalized.update(
            {
                "recording_id": recording_id,
                "video_prefix": str(recording.get("video_prefix") or recording_id),
                "report_dir": "records",
                "output_dir": "records/camera",
                "video_path": self._stored_path(video_path),
                "video_candidates": [self._stored_path(video_path)],
                "fluidics_csv": self._stored_path(fluidics_csv),
            }
        )
        return normalized

    def _register_control_recording(self, recording: dict[str, Any]) -> None:
        files = list(self.session.files)
        video_id = "video-" + _safe_id(str(recording["recording_id"]))
        csv_id = "fluidics-" + _safe_id(str(recording["recording_id"]))
        files = _upsert_file(
            files,
            SessionFile(
                id=video_id,
                path=str(recording["video_path"]),
                role="control_video",
                media_type="video/avi",
                metadata=_video_metadata(recording),
            ),
        )
        files = _upsert_file(
            files,
            SessionFile(
                id=csv_id,
                path=str(recording["fluidics_csv"]),
                role="control_fluidics_csv",
                media_type="text/csv",
                metadata=_csv_metadata(recording),
            ),
        )
        existing = _find_item(self.session.items, "acq-records")
        item_files = list(existing.files if existing else ())
        for file_id in (video_id, csv_id):
            if file_id not in item_files:
                item_files.append(file_id)
        item = SessionItem(
            id="acq-records",
            project_type="control_acquisition",
            engine="fluidics",
            files=tuple(item_files),
            metadata={"recording_count": len(item_files) // 2, "report_dir": "records"},
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
        "video_prefix": str(recording.get("video_prefix") or ""),
        "started_at": str(recording.get("started_at") or ""),
        "stopped_at": str(recording.get("stopped_at") or ""),
        "duration_s": float(recording.get("duration_s") or 0.0),
        "frames_recorded": recording.get("frames_recorded"),
        "frames_written": recording.get("frames_written"),
        "width": width,
        "height": height,
        "dimensions": f"{width}x{height}" if width and height else "",
        "acquisition_fps": float(recording.get("acquisition_fps") or 0.0),
        "converted_fps": float(recording.get("converted_fps") or recording.get("fps") or 0.0),
        "fluidics_csv": str(recording.get("fluidics_csv") or ""),
    }


def _csv_metadata(recording: dict[str, Any]) -> dict[str, Any]:
    return {
        "fluidics_csv": str(recording.get("fluidics_csv") or ""),
        "recording_id": str(recording.get("recording_id") or recording.get("video_prefix") or ""),
        "started_at": str(recording.get("started_at") or ""),
        "stopped_at": str(recording.get("stopped_at") or ""),
        "duration_s": float(recording.get("duration_s") or 0.0),
    }
