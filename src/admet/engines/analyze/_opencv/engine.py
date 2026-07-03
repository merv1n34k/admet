from __future__ import annotations

from pathlib import Path
from typing import Any

from admet.core.engine import ActionSpec, Param, ParamKind, ParamSchema, validate_action_settings
from admet.core.run import RunJob, RunResult

from .pipeline import DropletPipeline


class OpenCVAnalysisEngine:
    id = "opencv"
    name = "OpenCV Video Analysis"
    settings = ParamSchema(
        (
            Param("video_path", "Video Path", ParamKind.PATH, default="", required=True),
            Param("microns_per_pixel", "Microns Per Pixel", ParamKind.FLOAT, default=1.0),
            Param("fps", "FPS", ParamKind.FLOAT, default=0.0, minimum=0.0),
            Param("start_frame", "Start Frame", ParamKind.INTEGER, default=0, minimum=0),
            Param("end_frame", "End Frame", ParamKind.INTEGER, default=None, minimum=0),
            Param("roi_x", "ROI X", ParamKind.INTEGER, default=0, minimum=0),
            Param("roi_y", "ROI Y", ParamKind.INTEGER, default=0, minimum=0),
            Param("roi_width", "ROI Width", ParamKind.INTEGER, default=0, minimum=0),
            Param("roi_height", "ROI Height", ParamKind.INTEGER, default=0, minimum=0),
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
        video_path = _job_input(job, "video", "video_path")
        settings = dict(job.settings)
        settings["video_path"] = str(video_path)
        normalized = validate_action_settings(self.settings, self.actions, job.action, settings)
        data, row_count = self._process_video(
            str(video_path),
            normalized,
            cache_dir=job.cache_dir,
            job=job,
        )
        return RunResult(
            job_id=job.id,
            engine=self.id,
            action=job.action,
            metadata={
                "row_count": row_count,
                "sample_id": Path(video_path).stem,
                "cache_dir": data.get("cache_dir"),
                "frames_processed": data.get("frames_processed", 0),
                "total_detections": data.get("total_detections", 0),
                "total_droplets": data.get("total_droplets", 0),
                "mean_diameter_um": data.get("mean_diameter_um", 0.0),
                "std_diameter_um": data.get("std_diameter_um", 0.0),
                "mean_speed_mm_s": data.get("mean_speed_mm_s", 0.0),
                "frequency_hz": data.get("frequency_hz", 0.0),
                "threshold": data.get("threshold"),
                "trajectory": data.get("trajectory", {}),
                "true_stats": data.get("true_stats", {}),
                "droplet_geometry": data.get("droplet_geometry", {}),
            },
        )

    def _process_video(
        self,
        video_path: str,
        settings: dict[str, Any],
        *,
        cache_dir: Path | None,
        job: RunJob | None,
    ) -> tuple[dict[str, Any], int]:
        config = self._config_from_settings(settings, cache_dir=cache_dir)

        pipeline = DropletPipeline(config)
        pipeline.set_video(video_path)
        data = pipeline.run()
        if "error" in data:
            raise RuntimeError(data["error"])

        row_count = self._write_raw_rows(job, data) if job is not None else 0
        return data, row_count

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
        video_config: dict[str, Any] = {}
        if settings["start_frame"]:
            video_config["start_frame"] = settings["start_frame"]
        if settings["end_frame"] is not None:
            video_config["end_frame"] = settings["end_frame"]
        roi_width = int(settings["roi_width"] or 0)
        roi_height = int(settings["roi_height"] or 0)
        if roi_width or roi_height:
            if not roi_width or not roi_height:
                video_width, video_height = _video_dimensions(Path(str(settings["video_path"])))
                roi_width = roi_width or video_width
                roi_height = roi_height or video_height
            video_config["roi"] = (
                settings["roi_x"],
                settings["roi_y"],
                roi_width,
                roi_height,
            )
        if video_config:
            config["video"] = video_config
        if settings["fps"]:
            config["analysis"]["fps"] = settings["fps"]
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

def create_engine() -> OpenCVAnalysisEngine:
    return OpenCVAnalysisEngine()


def _job_input(job: RunJob, key: str, setting_key: str) -> Path:
    if key in job.inputs:
        return job.inputs[key]
    if setting_key in job.settings:
        return Path(str(job.settings[setting_key]))
    raise ValueError(f"{job.id} requires input {key!r} or setting {setting_key!r}")


def _video_dimensions(path: Path) -> tuple[int, int]:
    try:
        import cv2
    except Exception:
        return 0, 0
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return 0, 0
        return (
            max(int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0), 0),
            max(int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0), 0),
        )
    finally:
        capture.release()
