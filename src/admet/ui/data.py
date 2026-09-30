from __future__ import annotations

from pathlib import Path
from typing import Any



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
    converted_fps = float(recording.get("converted_fps") or recording.get("fps") or 0.0)
    acquisition_fps = float(recording.get("acquisition_fps") or 0.0)
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
        "video": Path(video_path).name if video_path else str(recording.get("video_prefix") or "recording"),
        "acquisition_fps": _format_number(metadata["acquisition_fps"], digits=2),
        "dimensions": metadata["dimensions"],
        "converted_fps": _format_number(metadata["converted_fps"], digits=2),
        "frames": "" if frames is None else str(frames),
        "duration": _format_duration(metadata["duration_s"]),
    }


def recording_video_key(recording: dict[str, Any], row: dict[str, str]) -> str:
    for value in (
        recording.get("recording_id"),
        recording.get("video_prefix"),
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
