from __future__ import annotations

from pathlib import Path
from typing import Any

from admet.core.engine import ActionSpec, RunJob, RunResult, validate_action_settings
from admet.core.schema import Param, ParamKind, ParamSchema
from admet.engines.analyze.stats import compute_sample_stats

from .config import load_config
from .detection import CellposeDetection


class CellposeAnalysisEngine:
    id = "cellpose"
    name = "Cellpose Image Analysis"
    settings = ParamSchema(
        (
            Param("input_dir", "Input Directory", ParamKind.PATH, default="", required=True),
            Param("config_path", "Config Path", ParamKind.PATH, default=""),
            Param("px_to_um", "Pixels To Microns", ParamKind.FLOAT, default=1.14, minimum=0.0),
            Param("frame_limit", "Frame Limit", ParamKind.INTEGER, default=None),
            Param("use_cache", "Use Cache", ParamKind.BOOLEAN, default=True),
            Param("detect_inclusions", "Detect Inclusions", ParamKind.BOOLEAN, default=True),
        )
    )
    actions = (
        ActionSpec(
            "analyze",
            "Analyze",
            "analysis",
            params=tuple(param.name for param in settings.params),
        ),
    )

    def run(self, job: RunJob) -> RunResult:
        if job.action != "analyze":
            raise ValueError(f"unsupported action for {self.id}: {job.action}")
        input_dir = _job_input(job, "input_dir", "input_dir")
        settings = dict(job.settings)
        settings["input_dir"] = str(input_dir)
        normalized = validate_action_settings(self.settings, self.actions, job.action, settings)
        rows, config, detector, row_count = self._process_input(
            str(input_dir),
            normalized,
            cache_dir=job.cache_dir,
            job=job,
        )
        stats = compute_sample_stats(
            Path(input_dir).name,
            rows,
            bead_count=config["settings"]["count"],
            dilution=config["settings"]["dilution"],
        )
        return RunResult(
            job_id=job.id,
            engine=self.id,
            action=job.action,
            metadata={
                "row_count": row_count,
                "sample_id": Path(input_dir).name,
                "frames_processed": len({row["frame"] for row in rows}),
                "detect_inclusions": detector.detect_inclusions,
                "px_to_um": config["px_to_um"],
                "cache_dir": str(detector.cache.cache_dir) if detector.cache else None,
                "cache_hits": getattr(detector, "cache_hits", 0),
                **stats,
            },
        )

    def _process_input(
        self,
        input_dir: str,
        settings: dict[str, Any],
        *,
        cache_dir: Path | None,
        job: RunJob | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], CellposeDetection, int]:
        config = self._config_from_settings(settings)
        detector = CellposeDetection(
            config,
            use_cache=settings["use_cache"],
            detect_inclusions=settings["detect_inclusions"],
            cache_dir=cache_dir,
        )
        rows = detector.run(
            input_dir,
            frame_limit=settings["frame_limit"],
        )
        row_count = self._write_raw_rows(job, rows) if job is not None else 0
        return rows, config, detector, row_count

    def _config_from_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        config = load_config(settings["config_path"] or None)
        config["px_to_um"] = settings["px_to_um"]
        config.setdefault("cache", {})["enabled"] = settings["use_cache"]
        return config

    def _write_raw_rows(self, job: RunJob | None, rows: list[dict[str, Any]]) -> int:
        if job is None or job.sink is None:
            return 0
        for row in rows:
            job.sink.write(
                {
                    "job_id": job.id,
                    "item_id": job.metadata.get("item_id", job.id),
                    "file_id": job.metadata.get("file_id", ""),
                    "engine": self.id,
                    "kind": "droplet",
                    "values": row,
                }
            )
        return len(rows)


def create_engine() -> CellposeAnalysisEngine:
    return CellposeAnalysisEngine()


def _job_input(job: RunJob, key: str, setting_key: str) -> Path:
    if key in job.inputs:
        return job.inputs[key]
    if setting_key in job.settings:
        return Path(str(job.settings[setting_key]))
    raise ValueError(f"{job.id} requires input {key!r} or setting {setting_key!r}")
