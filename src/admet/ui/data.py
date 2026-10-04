from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from admet.core.session import session_path


VIDEO_TABLE_COLUMNS = (
    ("video", "Video"),
    ("acquisition_fps", "Acq FPS"),
    ("dimensions", "Dimensions"),
    ("converted_fps", "Converted FPS"),
    ("frames", "Frames"),
    ("duration", "Duration"),
)


def video_metadata(recording: dict[str, Any]) -> dict[str, Any]:
    width = int(float(recording.get("width") or 0))
    height = int(float(recording.get("height") or 0))
    converted_fps = float(recording.get("converted_fps") or 0.0)
    acquisition_fps = float(recording.get("acquisition_fps") or 0.0)
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
        "acquisition_fps": acquisition_fps,
        "converted_fps": converted_fps,
        "fluidics_csv": str(recording.get("fluidics_csv") or ""),
    }


def video_row(recording: dict[str, Any]) -> dict[str, str]:
    metadata = video_metadata(recording)
    video_path = metadata["video_path"]
    frames = metadata["frames_recorded"]
    if frames is None:
        frames = metadata["frames_written"]
    return {
        "video": Path(video_path).name if video_path else str(recording.get("recording_id") or "recording"),
        "acquisition_fps": _format_number(metadata["acquisition_fps"], digits=2),
        "dimensions": metadata["dimensions"],
        "converted_fps": _format_number(metadata["converted_fps"], digits=2),
        "frames": "" if frames is None else str(frames),
        "duration": _format_duration(metadata["duration_s"]),
    }


def recording_video_key(recording: dict[str, Any], row: dict[str, str]) -> str:
    for value in (
        recording.get("recording_id"),
        Path(str(recording.get("video_path") or "")).stem,
        Path(str(row.get("video") or "")).stem,
        row.get("video"),
    ):
        text = str(value or "").strip()
        if text:
            stem = Path(text).stem
            return stem or text
    return "recording"


def prefer_video_row(existing: dict[str, str] | None, candidate: dict[str, str]) -> bool:
    if existing is None:
        return True
    return bool(Path(str(candidate.get("video") or "")).suffix) and not bool(
        Path(str(existing.get("video") or "")).suffix
    )


def _format_number(value: Any, *, digits: int) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return f"{number:.{digits}f}"


def _format_duration(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return f"{seconds:.2f} s"


def analysis_run_rows(project_paths: set[str]) -> list[dict[str, Any]]:
    rows = []
    for project_path in sorted(project_paths):
        path = session_path(project_path)
        metadata_path = path / "analysis" / "metadata.json"
        if not metadata_path.is_file():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for run in metadata.get("runs", []):
            if not isinstance(run, dict):
                continue
            raw = str(run.get("raw_path") or "")
            rows.append(
                {
                    "project": path.name,
                    "run_id": str(run.get("run_id") or ""),
                    "jobs": len((run.get("metadata") or {}).get("jobs", [])),
                    "raw": raw,
                }
            )
    return rows


def matrix_row(row: Any) -> dict[str, Any]:
    return {
        "uid": row.uid,
        "project": Path(row.project_path).name,
        "project_path": row.project_path,
        "source": Path(row.source_path).name or row.source_path,
        "source_path": row.source_path,
        "engine": row.engine,
        "sample_id": row.sample_id,
        "active": row.active,
    }
