from __future__ import annotations

from pathlib import Path
from typing import Any

from admet.core.engine import ActionSpec, EngineContext, EngineResult, validate_action_settings
from admet.core.schema import Param, ParamKind, ParamSchema, ResultRecord, ResultSet, SummaryStat
from admet.engines.analyze.stats import compute_sample_stats

from .config import load_config
from .detection import CellposeDetection


class CellposeAnalysisEngine:
    id = "cellpose"
    name = "Cellpose Image Analysis"
    settings = ParamSchema(
        (
            Param("input_dir", "Input Directory", ParamKind.PATH, default="", required=True),
            Param("output_dir", "Output Directory", ParamKind.PATH, default="output"),
            Param("config_path", "Config Path", ParamKind.PATH, default=""),
            Param("px_to_um", "Pixels To Microns", ParamKind.FLOAT, default=1.14, minimum=0.0),
            Param("frame_limit", "Frame Limit", ParamKind.INTEGER, default=None),
            Param("use_cache", "Use Cache", ParamKind.BOOLEAN, default=True),
            Param("detect_inclusions", "Detect Inclusions", ParamKind.BOOLEAN, default=True),
            Param("write_artifacts", "Write Artifacts", ParamKind.BOOLEAN, default=True),
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

    def run_action(
        self,
        action: str,
        settings: dict[str, Any],
        context: EngineContext | None = None,
    ) -> EngineResult:
        normalized = validate_action_settings(self.settings, self.actions, action, settings)
        input_dir = normalized["input_dir"]
        output_dir = self._resolve_output_dir(normalized["output_dir"], context)
        config = self._config_from_settings(normalized)
        detector = CellposeDetection(
            config,
            use_cache=normalized["use_cache"],
            detect_inclusions=normalized["detect_inclusions"],
        )
        rows = detector.run(
            input_dir,
            output_dir,
            frame_limit=normalized["frame_limit"],
            write_artifacts=normalized["write_artifacts"],
        )

        return EngineResult(
            result_set=self._to_result_set(input_dir, rows, config, detector),
            artifacts={
                "output_dir": str(output_dir),
                "cache_dir": str(detector.cache.cache_dir) if detector.cache else None,
                "cache_hits": getattr(detector, "cache_hits", 0),
            },
        )

    def _config_from_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        config = load_config(settings["config_path"] or None)
        config["px_to_um"] = settings["px_to_um"]
        config.setdefault("cache", {})["enabled"] = settings["use_cache"]
        return config

    def _resolve_output_dir(self, output_dir: str, context: EngineContext | None) -> Path:
        path = Path(output_dir)
        if context and context.workdir and not path.is_absolute():
            path = Path(context.workdir) / path
        return path

    def _to_result_set(
        self,
        input_dir: str,
        rows: list[dict[str, Any]],
        config: dict[str, Any],
        detector: CellposeDetection,
    ) -> ResultSet:
        sample_id = Path(input_dir).name
        records = tuple(
            ResultRecord(sample_id=sample_id, engine=self.id, values=row)
            for row in rows
        )
        stats_data = compute_sample_stats(
            sample_id,
            rows,
            bead_count=config["settings"]["count"],
            dilution=config["settings"]["dilution"],
        )
        stats = tuple(
            SummaryStat(name=name, value=value, unit=unit)
            for name, unit, value in (
                ("total_droplets", "", stats_data["total_droplets"]),
                ("total_inclusions", "", stats_data["total_inclusions"]),
                ("with_inclusions", "", stats_data["with_inclusions"]),
                ("mean_diameter", "um", stats_data["mean_d"]),
                ("median_diameter", "um", stats_data["median_d"]),
                ("std_diameter", "um", stats_data["std_d"]),
                ("cv", "%", stats_data["cv"]),
            )
        )
        metadata = {
            "sample_id": sample_id,
            "frames_processed": len({row["frame"] for row in rows}),
            "detect_inclusions": detector.detect_inclusions,
            "px_to_um": config["px_to_um"],
        }
        return ResultSet(records=records, stats=stats, metadata=metadata)


def create_engine() -> CellposeAnalysisEngine:
    return CellposeAnalysisEngine()
