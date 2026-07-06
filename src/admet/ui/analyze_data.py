from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from admet.core.session import session_path


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


def result_rows(report: Any) -> list[dict[str, Any]]:
    if report is None:
        return []
    rows = []
    for job in report.jobs:
        metadata = job.metadata
        true_stats = metadata.get("true_stats") if isinstance(metadata.get("true_stats"), dict) else {}
        mean_d = _numeric(metadata.get("mean_diameter_um"))
        std_d = _numeric(metadata.get("std_diameter_um"))
        cv = (std_d / mean_d * 100.0) if mean_d and std_d else None
        rows.append(
            {
                "sample": job.sample_id,
                "engine": job.engine,
                "status": job.status,
                "droplets": metadata.get("total_droplets", ""),
                "mean_um": _fmt(mean_d),
                "cv": _fmt(cv),
                "speed": _fmt(metadata.get("mean_speed_mm_s")),
                "freq": _fmt(metadata.get("frequency_hz")),
                "volume_nl": _fmt(true_stats.get("droplet_volume_nl"), 3),
                "threshold": metadata.get("threshold", ""),
            }
        )
    return rows


def result_columns() -> list[dict[str, Any]]:
    return [
        {"name": "sample", "label": "Sample", "field": "sample", "align": "left"},
        {"name": "engine", "label": "Engine", "field": "engine", "align": "left"},
        {"name": "status", "label": "Status", "field": "status", "align": "left"},
        {"name": "droplets", "label": "Droplets", "field": "droplets", "align": "right"},
        {"name": "mean_um", "label": "Mean Ø µm", "field": "mean_um", "align": "right"},
        {"name": "cv", "label": "CV %", "field": "cv", "align": "right"},
        {"name": "speed", "label": "Speed mm/s", "field": "speed", "align": "right"},
        {"name": "freq", "label": "Freq Hz", "field": "freq", "align": "right"},
        {"name": "volume_nl", "label": "Vol nL", "field": "volume_nl", "align": "right"},
        {"name": "threshold", "label": "Thr", "field": "threshold", "align": "right"},
    ]


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


def _numeric(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any, digits: int = 2) -> str:
    number = _numeric(value)
    if number is None or not math.isfinite(number) or number == 0:
        return ""
    return f"{number:.{digits}f}"
