from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from admet.core.engine import EngineResult

from .camera import VideoWorker


class RecordingCamera(Protocol):
    @property
    def recording(self) -> bool:
        ...

    def start_recording(self, writer, *, max_frames=None, max_time=None) -> bool:
        ...

    def stop_recording(self) -> int:
        ...

    def set_recording_complete_callback(self, callback) -> None:
        ...


class ControlBackend(Protocol):
    def run_action(self, action: str, settings: dict[str, Any], context=None) -> EngineResult:
        ...


WriterFactory = Callable[[Path, str, int, int, float], Any]


@dataclass
class RecordingMetadata:
    started_at: str
    started_monotonic_s: float
    output_dir: str
    video_prefix: str
    width: int
    height: int
    fps: float
    droplegen_csv: str = ""
    stopped_at: str = ""
    duration_s: float = 0.0
    frames_recorded: int | None = None
    frames_written: int | None = None
    video_path: str = ""
    video_candidates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data.pop("started_monotonic_s", None)
        return data


class RecordingSession:
    def __init__(
        self,
        report_root: str | Path,
        camera: RecordingCamera,
        control: ControlBackend,
        *,
        writer_factory: WriterFactory | None = None,
    ):
        self.report_root = Path(report_root)
        self.camera = camera
        self.control = control
        self.writer_factory = writer_factory or _default_writer_factory
        self.report_dir: Path | None = None
        self.recordings: list[dict[str, Any]] = []
        self.current: RecordingMetadata | None = None
        callback_setter = getattr(self.camera, "set_recording_complete_callback", None)
        if callable(callback_setter):
            callback_setter(self.stop_recording)

    def create_report_dir(self) -> Path:
        if self.report_dir is None:
            stamp = datetime.now().strftime("%y%m%d-%H%M%S")
            self.report_dir = self.report_root / stamp
            (self.report_dir / "video").mkdir(parents=True, exist_ok=True)
            (self.report_dir / "droplegen").mkdir(parents=True, exist_ok=True)
            self.write_summary()
        return self.report_dir

    def start_recording(
        self,
        prefix: str,
        *,
        width: int,
        height: int,
        fps: float,
        max_frames: int | None = None,
        max_time: float | None = None,
    ) -> RecordingMetadata:
        report_dir = self.create_report_dir()
        video_dir = report_dir / "video"
        droplegen_dir = report_dir / "droplegen"
        writer = self.writer_factory(video_dir, prefix, width, height, fps)
        if not self.camera.start_recording(writer, max_frames=max_frames, max_time=max_time):
            raise RuntimeError("Failed to start camera recording")

        result = self.control.run_action("start_recording", {"log_dir": str(droplegen_dir)})
        self.current = RecordingMetadata(
            started_at=datetime.now().isoformat(timespec="seconds"),
            started_monotonic_s=time.monotonic(),
            output_dir=str(video_dir),
            video_prefix=prefix,
            width=width,
            height=height,
            fps=fps,
            droplegen_csv=str(result.artifacts.get("csv_path", "")),
        )
        self.write_summary()
        return self.current

    def stop_recording(self) -> RecordingMetadata | None:
        if self.current is None:
            return None

        writer_frame_count = _writer_frame_count(self.camera)
        if self.camera.recording:
            frames_recorded = self.camera.stop_recording()
        else:
            frames_recorded = _last_recording_frames(self.camera)
        writer_frame_count = _writer_frame_count(self.camera) or writer_frame_count
        self.control.run_action("stop_recording", {})

        now = time.monotonic()
        output_dir = Path(self.current.output_dir)
        candidates = []
        if output_dir.exists() and self.current.video_prefix:
            candidates = sorted(
                output_dir.glob(f"{self.current.video_prefix}_*.avi"),
                key=lambda path: path.stat().st_mtime,
            )

        self.current.stopped_at = datetime.now().isoformat(timespec="seconds")
        self.current.duration_s = max(0.0, now - self.current.started_monotonic_s)
        self.current.frames_recorded = frames_recorded
        self.current.frames_written = writer_frame_count
        self.current.video_path = str(candidates[-1]) if candidates else ""
        self.current.video_candidates = [str(path) for path in candidates[-3:]]

        completed = self.current
        self.recordings.append(completed.to_dict())
        self.current = None
        self.write_summary()
        return completed

    def write_summary(self) -> None:
        if self.report_dir is None:
            return
        summary = {
            "updated": datetime.now().isoformat(timespec="seconds"),
            "report_dir": str(self.report_dir),
            "recording_count": len(self.recordings),
            "recordings": self.recordings,
        }
        (self.report_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def _default_writer_factory(video_dir: Path, prefix: str, width: int, height: int, fps: float):
    return VideoWorker(video_dir, prefix, width, height, fps)


def _writer_frame_count(camera: RecordingCamera) -> int | None:
    writer = getattr(camera, "writer", None)
    if writer is None:
        return getattr(camera, "last_writer_frame_count", None)
    return getattr(writer, "frame_count", None)


def _last_recording_frames(camera: RecordingCamera) -> int | None:
    return getattr(camera, "last_recording_frames", None)
