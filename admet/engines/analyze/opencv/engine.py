from __future__ import annotations

from pathlib import Path
from typing import Any

from admet.core.engine import ActionSpec, EngineContext, EngineResult, RunJob, RunResult, validate_action_settings
from admet.core.schema import Param, ParamKind, ParamSchema, ResultRecord, ResultSet, SummaryStat

from .pipeline import DropletPipeline


class OpenCVAnalysisEngine:
    id = "opencv"
    name = "OpenCV Video Analysis"
    settings = ParamSchema(
        (
            Param("video_path", "Video Path", ParamKind.PATH, default="", required=True),
            Param("microns_per_pixel", "Microns Per Pixel", ParamKind.FLOAT, default=1.0),
            Param("fps", "FPS", ParamKind.FLOAT, default=0.0, minimum=0.0),
            Param("max_frames", "Max Frames", ParamKind.INTEGER, default=None),
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
        result, _row_count = self._process_video(
            normalized["video_path"],
            normalized,
            cache_dir=None,
            job=None,
        )
        return result

    def run(self, job: RunJob) -> RunResult:
        if job.action != "analyze":
            raise ValueError(f"unsupported action for {self.id}: {job.action}")
        video_path = _job_input(job, "video")
        settings = self.settings.defaults()
        settings.update(job.settings)
        settings["video_path"] = str(video_path)
        result, row_count = self._process_video(
            str(video_path),
            settings,
            cache_dir=job.cache_dir,
            job=job,
        )
        return RunResult(
            job_id=job.id,
            engine=self.id,
            action=job.action,
            metadata={
                "row_count": row_count,
                "cache_dir": result.artifacts.get("cache_dir"),
                **result.result_set.metadata,
            },
        )

    def _process_video(
        self,
        video_path: str,
        settings: dict[str, Any],
        *,
        cache_dir: Path | None,
        job: RunJob | None,
    ) -> tuple[EngineResult, int]:
        config = self._config_from_settings(settings, cache_dir=cache_dir)

        pipeline = DropletPipeline(config)
        pipeline.set_video(video_path)
        data = pipeline.run()
        if "error" in data:
            raise RuntimeError(data["error"])

        result = EngineResult(
            result_set=self._to_result_set(video_path, data),
            artifacts={
                "background": pipeline.background,
                "cache_dir": data.get("cache_dir"),
            },
        )
        row_count = self._write_raw_rows(job, data) if job is not None else 0
        return result, row_count

    def _config_from_settings(
        self,
        settings: dict[str, Any],
        *,
        cache_dir: Path | None,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {
            "background": {"sample_frames": 80},
            "threshold": {"method": "btvs", "sample_frames": 5},
            "segmentation": {
                "min_area": 50,
                "max_area": 10000,
                "min_circularity": 0.3,
                "max_aspect_ratio": 4.0,
            },
            "tracking": {"max_distance": 30, "min_count": 3, "max_miss": 2},
            "analysis": {"microns_per_pixel": settings["microns_per_pixel"]},
        }
        if cache_dir is not None:
            config["processing"] = {"cache_dir": str(cache_dir)}
        if settings["fps"]:
            config["analysis"]["fps"] = settings["fps"]
        if settings["max_frames"] is not None:
            config.setdefault("processing", {})["max_frames"] = settings["max_frames"]
        return config

    def _write_raw_rows(self, job: RunJob | None, result: dict[str, Any]) -> int:
        if job is None or job.sink is None:
            return 0
        row_count = 0
        for frame in result.get("frame_data", []):
            frame_index = frame.get("frame")
            job.sink.write(
                {
                    "job_id": job.id,
                    "item_id": job.metadata.get("item_id", job.id),
                    "file_id": job.metadata.get("file_id", ""),
                    "engine": self.id,
                    "kind": "frame",
                    "values": {
                        "frame": frame_index,
                        "count": frame.get("count", 0),
                        "properties": frame.get("properties", ()),
                    },
                }
            )
            row_count += 1
            for detection_index, detection in enumerate(frame.get("properties", ())):
                job.sink.write(
                    {
                        "job_id": job.id,
                        "item_id": job.metadata.get("item_id", job.id),
                        "file_id": job.metadata.get("file_id", ""),
                        "engine": self.id,
                        "kind": "detection",
                        "values": {
                            "frame": frame_index,
                            "detection_index": detection_index,
                            **detection,
                        },
                    }
                )
                row_count += 1
        for track in result.get("tracks", []):
            job.sink.write(
                {
                    "job_id": job.id,
                    "item_id": job.metadata.get("item_id", job.id),
                    "file_id": job.metadata.get("file_id", ""),
                    "engine": self.id,
                    "kind": "track",
                    "values": track,
                }
            )
            row_count += 1
        return row_count

    def _to_result_set(self, video_path: str, result: dict[str, Any]) -> ResultSet:
        sample_id = Path(video_path).stem
        records = tuple(
            ResultRecord(sample_id=sample_id, engine=self.id, values=track)
            for track in result.get("tracks", [])
        )
        stats = tuple(
            SummaryStat(name=name, value=value, unit=unit)
            for name, unit, value in (
                ("total_droplets", "", result.get("total_droplets", 0)),
                ("total_detections", "", result.get("total_detections", 0)),
                ("frames_processed", "", result.get("frames_processed", 0)),
                ("mean_diameter", "um", result.get("mean_diameter_um", 0.0)),
                ("std_diameter", "um", result.get("std_diameter_um", 0.0)),
                ("mean_speed", "mm/s", result.get("mean_speed_mm_s", 0.0)),
                ("frequency", "Hz", result.get("frequency_hz", 0.0)),
            )
        )
        metadata = {
            "sample_id": sample_id,
            "threshold": result.get("threshold"),
            "trajectory": result.get("trajectory", {}),
            "true_stats": result.get("true_stats", {}),
            "droplet_geometry": result.get("droplet_geometry", {}),
        }
        return ResultSet(records=records, stats=stats, metadata=metadata)


def create_engine() -> OpenCVAnalysisEngine:
    return OpenCVAnalysisEngine()


def _job_input(job: RunJob, key: str) -> Path:
    if key in job.inputs:
        return job.inputs[key]
    if job.inputs:
        return next(iter(job.inputs.values()))
    raise ValueError(f"{job.id} requires input {key!r}")
