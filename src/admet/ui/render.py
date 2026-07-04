from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from admet.core.session import SessionFile, SessionItem


@dataclass
class MatrixRow:
    uid: str
    project_path: str
    source_path: str
    engine: str
    sample_id: str
    cache_policy: str = "use"
    active: bool = True
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StoredRun:
    project_path: Path
    run_id: str
    raw_path: Path
    jobs: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class RawSummary:
    project: str
    run_id: str
    sample_id: str
    engine: str
    rows: int
    frames: int
    droplets: int
    mean_diameter: float
    cv_percent: float
    inclusions: int


@dataclass(frozen=True)
class FluidicsRun:
    project: str
    recording_id: str
    rows: tuple[dict[str, float], ...]
    metadata: dict[str, Any]


def load_stored_runs(project_paths: list[Path]) -> list[StoredRun]:
    runs = []
    for project_path in project_paths:
        metadata_path = project_path / "analysis" / "metadata.json"
        if not metadata_path.is_file():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for run in metadata.get("runs", []):
            if not isinstance(run, dict):
                continue
            raw_path = resolve_project_path(project_path, str(run.get("raw_path") or ""))
            run_metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
            jobs = tuple(job for job in run_metadata.get("jobs", ()) if isinstance(job, dict))
            runs.append(
                StoredRun(
                    project_path=project_path,
                    run_id=str(run.get("run_id") or raw_path.parent.name),
                    raw_path=raw_path,
                    jobs=jobs,
                )
            )
    return runs


def read_raw_rows(project_path: Path, raw_path: Path, *, limit: int = 50_000) -> list[dict[str, Any]]:
    path = raw_path if raw_path.is_absolute() else project_path / raw_path
    if not path.is_file():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if len(rows) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def summarize_raw_rows(run: StoredRun, rows: list[dict[str, Any]]) -> list[RawSummary]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        engine = str(row.get("engine") or "")
        sample_id = str(row.get("item_id") or row.get("sample_id") or row.get("file_id") or "sample")
        groups.setdefault((engine, sample_id), []).append(row)

    summaries = []
    for (engine, sample_id), group_rows in sorted(groups.items()):
        frames = {
            int(float(values.get("frame")))
            for values in (row_values(row) for row in group_rows)
            if is_number(values.get("frame"))
        }
        diameters = [
            numeric(values.get("diameter_um") or values.get("diameter"))
            for values in (row_values(row) for row in group_rows)
        ]
        diameters = [value for value in diameters if value is not None and math.isfinite(value)]
        droplets = sum(1 for row in group_rows if row.get("kind") in {"detection", "droplet", "track"})
        inclusions = sum(
            int(numeric(row_values(row).get("inclusions")) or 0)
            for row in group_rows
            if row.get("kind") == "droplet"
        )
        avg = mean(diameters) if diameters else 0.0
        cv = (pstdev(diameters) / avg * 100.0) if len(diameters) > 1 and avg else 0.0
        summaries.append(
            RawSummary(
                project=run.project_path.name,
                run_id=run.run_id,
                sample_id=sample_id,
                engine=engine,
                rows=len(group_rows),
                frames=len(frames),
                droplets=droplets,
                mean_diameter=avg,
                cv_percent=cv,
                inclusions=inclusions,
            )
        )
    for job in run.jobs:
        if any(
            summary.sample_id == str(job.get("sample_id")) and summary.engine == str(job.get("engine"))
            for summary in summaries
        ):
            continue
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        summaries.append(
            RawSummary(
                project=run.project_path.name,
                run_id=run.run_id,
                sample_id=str(job.get("sample_id") or "sample"),
                engine=str(job.get("engine") or ""),
                rows=int(numeric(metadata.get("row_count")) or 0),
                frames=int(numeric(metadata.get("frames_processed")) or 0),
                droplets=int(numeric(metadata.get("total_droplets") or metadata.get("total_detections")) or 0),
                mean_diameter=float(numeric(metadata.get("mean_diameter_um")) or 0.0),
                cv_percent=0.0,
                inclusions=0,
            )
        )
    return summaries


def load_fluidics_runs(project_paths: list[Path]) -> list[FluidicsRun]:
    runs = []
    for project_path in project_paths:
        metadata_path = project_path / "records" / "metadata.json"
        if not metadata_path.is_file():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for recording in metadata.get("recordings", []):
            if not isinstance(recording, dict):
                continue
            csv_path = resolve_project_path(project_path, str(recording.get("fluidics_csv") or ""))
            rows = tuple(read_fluidics_csv(csv_path))
            runs.append(
                FluidicsRun(
                    project=project_path.name,
                    recording_id=str(recording.get("recording_id") or recording.get("video_prefix") or csv_path.stem),
                    rows=rows,
                    metadata=recording,
                )
            )
    return runs


def read_fluidics_csv(csv_path: Path, *, limit: int = 25_000) -> list[dict[str, float]]:
    if not csv_path.is_file():
        return []
    rows = []
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if len(rows) >= limit:
                break
            rows.append(
                {
                    "elapsed_s": float_or_zero(row.get("elapsed_s")),
                    "pressure": mean_columns(row, "pressure_"),
                    "flow": mean_columns(row, "flow_"),
                }
            )
    return rows


def summary_table_rows(summaries: list[RawSummary]) -> list[dict[str, Any]]:
    return [
        {
            "sample": summary.sample_id,
            "rows": summary.rows,
            "frames": summary.frames,
            "droplets": summary.droplets,
            "mean_diameter": f"{summary.mean_diameter:.2f}" if summary.mean_diameter else "",
            "cv_percent": f"{summary.cv_percent:.2f}" if summary.cv_percent else "",
        }
        for summary in summaries
    ]


def fluidics_rows(runs: list[FluidicsRun]) -> list[dict[str, Any]]:
    rows = []
    for run in runs:
        pressures = [row["pressure"] for row in run.rows if math.isfinite(row["pressure"])]
        flows = [row["flow"] for row in run.rows if math.isfinite(row["flow"])]
        rows.append(
            {
                "project": run.project,
                "recording": run.recording_id,
                "rows": len(run.rows),
                "duration_s": f"{fluidics_duration(run):.1f}",
                "mean_pressure": f"{mean(pressures):.2f}" if pressures else "",
                "mean_flow": f"{mean(flows):.2f}" if flows else "",
            }
        )
    return rows


def diameter_chart(summaries: list[RawSummary]) -> dict[str, Any]:
    return bar_chart(
        "Mean diameter",
        [summary.sample_id for summary in summaries],
        [round(summary.mean_diameter, 3) for summary in summaries],
        "#225d82",
    )


def count_chart(summaries: list[RawSummary]) -> dict[str, Any]:
    return bar_chart(
        "Droplet rows",
        [summary.sample_id for summary in summaries],
        [summary.droplets for summary in summaries],
        "#185e49",
    )


def cv_chart(summaries: list[RawSummary]) -> dict[str, Any]:
    return bar_chart(
        "CV %",
        [summary.sample_id for summary in summaries],
        [round(summary.cv_percent, 3) for summary in summaries],
        "#742323",
    )


def fluidics_chart(runs: list[FluidicsRun], field: str) -> dict[str, Any]:
    series = []
    for run in runs[:8]:
        points = [
            [round(row["elapsed_s"], 3), round(row[field], 3)]
            for row in run.rows
            if math.isfinite(row["elapsed_s"]) and math.isfinite(row[field])
        ]
        if len(points) > 600:
            step = max(1, len(points) // 600)
            points = points[::step]
        series.append({"name": run.recording_id, "type": "line", "showSymbol": False, "data": points})
    return {
        "title": {"text": field.title(), "left": 8, "top": 4, "textStyle": {"fontSize": 13}},
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 44, "right": 12, "top": 36, "bottom": 34},
        "xAxis": {"type": "value", "name": "s"},
        "yAxis": {"type": "value"},
        "series": series,
    }


def bar_chart(title: str, labels: list[str], values: list[float | int], color: str) -> dict[str, Any]:
    return {
        "title": {"text": title, "left": 8, "top": 4, "textStyle": {"fontSize": 13}},
        "tooltip": {},
        "grid": {"left": 48, "right": 12, "top": 36, "bottom": 54},
        "xAxis": {"type": "category", "data": labels, "axisLabel": {"rotate": 25}},
        "yAxis": {"type": "value"},
        "series": [{"type": "bar", "data": values, "itemStyle": {"color": color}}],
    }


def preview_frame_index(row: MatrixRow) -> int:
    return max(0, row_int(row, "start_frame", 0) + row_int(row, "preview_frame", 0))


def resolve_project_path(project_path: Path, stored_path: str) -> Path:
    path = Path(stored_path)
    return path if path.is_absolute() else project_path / path


def row_values(row: dict[str, Any]) -> dict[str, Any]:
    values = row.get("values")
    return values if isinstance(values, dict) else {}


def row_setting(row: MatrixRow, key: str, default: Any) -> Any:
    value = row.settings.get(key, default)
    return default if value in {None, ""} else value


def row_int(row: MatrixRow, key: str, default: Any) -> int:
    return int(numeric(row_setting(row, key, default)) or 0)


def row_float(row: MatrixRow, key: str, default: Any) -> float:
    return float(numeric(row_setting(row, key, default)) or 0.0)


def row_bool(row: MatrixRow, key: str, default: Any) -> bool:
    value = row_setting(row, key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def numeric(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_slider_value(value: Any, step: float) -> str:
    number = numeric(value) or 0.0
    if step >= 1:
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def is_number(value: Any) -> bool:
    number = numeric(value)
    return number is not None and math.isfinite(number)


def float_or_zero(value: Any) -> float:
    number = numeric(value)
    return number if number is not None and math.isfinite(number) else 0.0


def mean_columns(row: dict[str, Any], prefix: str) -> float:
    values = [
        float_or_zero(value)
        for key, value in row.items()
        if key.startswith(prefix) and value not in {None, ""}
    ]
    return mean(values) if values else 0.0


def fluidics_duration(run: FluidicsRun) -> float:
    if run.rows:
        return max(row["elapsed_s"] for row in run.rows)
    return float_or_zero(run.metadata.get("duration_s"))


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


def fluidics_csv_metadata(recording: dict[str, Any]) -> dict[str, Any]:
    return {
        "fluidics_csv": str(recording.get("fluidics_csv") or ""),
        "recording_id": str(recording.get("recording_id") or recording.get("video_prefix") or ""),
        "started_at": str(recording.get("started_at") or ""),
        "stopped_at": str(recording.get("stopped_at") or ""),
        "duration_s": float(recording.get("duration_s") or 0.0),
    }


def recording_item_metadata(recording: dict[str, Any]) -> dict[str, Any]:
    metadata = video_metadata(recording)
    metadata.update(fluidics_csv_metadata(recording))
    metadata["report_dir"] = str(recording.get("report_dir") or "")
    return metadata


def recordings_from_metadata(metadata: Any) -> list[dict[str, Any]]:
    if not isinstance(metadata, dict):
        return []
    recordings = metadata.get("recordings")
    if isinstance(recordings, list):
        return [recording for recording in recordings if isinstance(recording, dict)]
    current = metadata.get("current_recording")
    if isinstance(current, dict):
        return [current]
    return []


def normalize_recording_metadata(recording: dict[str, Any], report_dir: Path) -> dict[str, Any]:
    normalized = dict(recording)
    normalized["report_dir"] = str(report_dir)
    video_prefix = str(
        normalized.get("video_prefix")
        or normalized.get("recording_id")
        or Path(str(normalized.get("video_path") or "")).stem
    )
    if video_prefix:
        normalized["video_prefix"] = video_prefix
        normalized.setdefault("recording_id", video_prefix)
    video_path = recording_member_path(
        normalized.get("video_path"),
        report_dir,
        "camera",
        video_prefix,
        ".avi",
    )
    fluidics_csv = recording_member_path(
        normalized.get("fluidics_csv"),
        report_dir,
        "fluidics",
        video_prefix,
        ".csv",
    )
    if video_path is not None:
        normalized["video_path"] = str(video_path)
        normalized["output_dir"] = str(video_path.parent)
    if fluidics_csv is not None:
        normalized["fluidics_csv"] = str(fluidics_csv)
    return normalized


def recording_member_path(
    raw_path: Any,
    report_dir: Path,
    subdir: str,
    stem: str,
    suffix: str,
) -> Path | None:
    raw_text = str(raw_path or "").strip()
    if raw_text:
        path = Path(raw_text)
        candidates = [path] if path.is_absolute() else [report_dir / path, report_dir.parent / path]
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
    if stem:
        candidate = report_dir / subdir / f"{stem}{suffix}"
        if candidate.exists():
            return candidate.resolve()
    return None


def session_stored_path(path_value: str, project_path: Path | None) -> tuple[str, bool]:
    path = Path(path_value)
    if project_path is None:
        return str(path), path.is_absolute()
    root = project_path.resolve()
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        return path.relative_to(root).as_posix(), False
    except ValueError:
        return str(path), True


def video_row(recording: dict[str, Any]) -> dict[str, str]:
    metadata = video_metadata(recording)
    video_path = metadata["video_path"]
    frames = metadata["frames_recorded"]
    if frames is None:
        frames = metadata["frames_written"]
    return {
        "video": Path(video_path).name if video_path else str(recording.get("video_prefix") or "recording"),
        "acquisition_fps": format_number(metadata["acquisition_fps"], digits=2),
        "dimensions": metadata["dimensions"],
        "converted_fps": format_number(metadata["converted_fps"], digits=2),
        "frames": "" if frames is None else str(frames),
        "duration": format_duration(metadata["duration_s"]),
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


def video_file_id(video_path: str, files: list[SessionFile]) -> str:
    return session_file_id("video", video_path, files)


def session_file_id(prefix: str, path: str, files: list[SessionFile]) -> str:
    for file in files:
        if file.path == path or file.metadata.get("video_path") == path or file.metadata.get("fluidics_csv") == path:
            return file.id
    stem = Path(path).stem or prefix
    base = prefix + "-" + "".join(ch.lower() if ch.isalnum() else "-" for ch in stem).strip("-")
    existing = {file.id for file in files}
    candidate = base or prefix
    index = 2
    while candidate in existing:
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def recording_item_id(recording: dict[str, Any]) -> str:
    source = str(
        Path(str(recording.get("report_dir") or "")).name
        or recording.get("recording_id")
        or recording.get("video_prefix")
        or Path(str(recording.get("video_path") or "")).stem
    )
    base = "acq-" + "".join(ch.lower() if ch.isalnum() else "-" for ch in source).strip("-")
    return base or "acq-recording"


def find_session_item(items: tuple[SessionItem, ...], item_id: str) -> SessionItem | None:
    for item in items:
        if item.id == item_id:
            return item
    return None


def upsert_session_file(files: list[SessionFile], stored: SessionFile) -> list[SessionFile]:
    for index, file in enumerate(files):
        if (
            file.id == stored.id
            or file.path == stored.path
            or file.metadata.get("video_path") == stored.path
            or file.metadata.get("fluidics_csv") == stored.path
        ):
            files[index] = stored
            return files
    files.append(stored)
    return files


def upsert_session_item(items: list[SessionItem], stored: SessionItem) -> list[SessionItem]:
    for index, item in enumerate(items):
        if item.id == stored.id:
            items[index] = stored
            return items
    items.append(stored)
    return items


def format_number(value: Any, *, digits: int) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return f"{number:.{digits}f}"


def format_duration(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return f"{seconds:.2f} s"
