from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .camera import VideoWorker
from .fluidics import CsvLogger


def _positive_or_none(value: Any) -> float | None:
    """Recording limits use 0 (or a missing setting) to mean "no limit"."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _frame_limit(value: Any) -> int | None:
    number = _positive_or_none(value)
    return int(number) if number is not None else None


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

    def set_preview_enabled(self, enabled: bool) -> None:
        ...


class ControlBackend(Protocol):
    def start_recording(self, settings: dict[str, Any]) -> str:
        ...

    def stop_recording(self) -> None:
        ...


WriterFactory = Callable[[Path, str, int, int, float], Any]


@dataclass
class RecordingMetadata:
    started_at: str
    started_monotonic_s: float
    recording_id: str
    report_dir: str
    output_dir: str
    video_prefix: str
    width: int
    height: int
    fps: float
    converted_fps: float = 0.0
    acquisition_fps: float = 0.0
    fluidics_csv: str = ""
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


class RecordingRun:
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
            self.report_dir = create_recording_report_dir(self.report_root)
            (self.report_dir / "camera").mkdir(parents=True, exist_ok=True)
            (self.report_dir / "fluidics").mkdir(parents=True, exist_ok=True)
        return self.report_dir

    def start_recording(
        self,
        recording_label: str,
        *,
        width: int,
        height: int,
        fps: float,
        max_frames: int | None = None,
        max_time: float | None = None,
        video_path: str | Path | None = None,
        fluidics_csv_path: str | Path | None = None,
    ) -> RecordingMetadata:
        if video_path is not None and fluidics_csv_path is not None:
            video_file = Path(video_path)
            csv_file = Path(fluidics_csv_path)
            report_dir = video_file.parent.parent
            video_dir = video_file.parent
            fluidics_dir = csv_file.parent
            recording_id = video_file.stem
            video_dir.mkdir(parents=True, exist_ok=True)
            fluidics_dir.mkdir(parents=True, exist_ok=True)
            self.report_dir = report_dir
        else:
            report_dir = self.create_report_dir()
            video_dir = report_dir / "camera"
            fluidics_dir = report_dir / "fluidics"
            recording_id = create_recording_id(recording_label)
            csv_file = fluidics_dir / f"{recording_id}.csv"
        writer = self.writer_factory(video_dir, recording_id, width, height, fps)
        if not self.camera.start_recording(writer, max_frames=max_frames, max_time=max_time):
            raise RuntimeError("Failed to start camera recording")

        csv_path = self.control.start_recording(
            {
                "fluidics_dir": str(fluidics_dir),
                "csv_filename": csv_file.name,
            },
        )
        self.current = RecordingMetadata(
            started_at=datetime.now().isoformat(timespec="seconds"),
            started_monotonic_s=time.monotonic(),
            recording_id=recording_id,
            report_dir=str(report_dir),
            output_dir=str(video_dir),
            video_prefix=recording_id,
            width=width,
            height=height,
            fps=fps,
            converted_fps=fps,
            fluidics_csv=str(csv_path),
        )
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
        self.control.stop_recording()

        now = time.monotonic()
        output_dir = Path(self.current.output_dir)
        candidates = []
        if output_dir.exists() and self.current.video_prefix:
            candidates = sorted(
                output_dir.glob(f"{self.current.video_prefix}.avi"),
                key=lambda path: path.stat().st_mtime,
            )

        self.current.stopped_at = datetime.now().isoformat(timespec="seconds")
        self.current.duration_s = max(0.0, now - self.current.started_monotonic_s)
        self.current.frames_recorded = frames_recorded
        self.current.frames_written = writer_frame_count
        frame_count = frames_recorded if frames_recorded is not None else writer_frame_count
        if frame_count is not None and self.current.duration_s > 0:
            self.current.acquisition_fps = float(frame_count) / self.current.duration_s
        self.current.video_path = str(candidates[-1]) if candidates else ""
        self.current.video_candidates = [str(path) for path in candidates[-3:]]

        completed = self.current
        self.recordings.append(completed.to_dict())
        self.current = None
        return completed


class RecordingCoordinator:
    def __init__(
        self,
        *,
        csv_logger: CsvLogger,
        hardware_state: Callable[[], Any],
        acquisition: Callable[[], Any | None],
        writer_factory: WriterFactory | None = None,
    ) -> None:
        self.csv_logger = csv_logger
        self._hardware_state = hardware_state
        self._acquisition = acquisition
        self._writer_factory = writer_factory
        self._recording = False
        self._recording_run: RecordingRun | None = None
        self._csv_recording: RecordingMetadata | None = None
        self._csv_recording_report_dir: Path | None = None
        self._csv_recordings: list[dict[str, Any]] = []
        self._last_recording: RecordingMetadata | None = None

    @property
    def recording_active(self) -> bool:
        return self._recording

    def start_recording(
        self,
        settings: dict[str, Any],
        *,
        camera_recorder: RecordingCamera | None = None,
        frame_size: tuple[int, int] = (0, 0),
    ) -> dict[str, Any]:
        if self._recording:
            recording = self.active_recording_metadata()
            return {
                "csv_path": self.csv_logger.filepath or "",
                "report_dir": str(self.active_recording_report_dir() or ""),
                "recording": recording.to_dict() if recording is not None else {},
            }

        report_root = self._recording_root(settings)
        recording_label = str(settings["recording_label"])
        if camera_recorder is not None:
            return self._start_camera_recording(
                settings,
                camera_recorder,
                report_root,
                recording_label,
                frame_size,
            )
        return self._start_csv_only_recording(settings, report_root, recording_label)

    def stop_recording(self) -> dict[str, Any]:
        if self._recording_run is not None and self._recording_run.current is not None:
            metadata = self._recording_run.stop_recording()
            self._restore_camera_preview_after_recording()
            if metadata is not None:
                self._last_recording = metadata
                recording = metadata.to_dict()
                return {
                    "csv_path": metadata.fluidics_csv,
                    "report_dir": str(self._recording_run.report_dir or ""),
                    "video_path": metadata.video_path,
                    "recording": recording,
                }
        if not self._recording:
            self._restore_camera_preview_after_recording()
            return {}
        self._stop_csv_recording()
        self._restore_camera_preview_after_recording()
        metadata = self._csv_recording
        if metadata is None:
            return {"csv_path": self.csv_logger.filepath or ""}
        metadata.stopped_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        metadata.duration_s = max(0.0, time.monotonic() - metadata.started_monotonic_s)
        self._last_recording = metadata
        if self._csv_recording_report_dir is not None:
            self._csv_recordings.append(metadata.to_dict())
        self._csv_recording = None
        report_dir = str(self._csv_recording_report_dir or "")
        self._csv_recording_report_dir = None
        return {
            "csv_path": metadata.fluidics_csv,
            "report_dir": report_dir,
            "recording": metadata.to_dict(),
        }

    def status_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if self._recording_run is not None:
            metadata["recordings"] = list(self._recording_run.recordings)
            if self._recording_run.current is not None:
                metadata["current_recording"] = self._recording_run.current.to_dict()
        elif self._csv_recordings:
            metadata["recordings"] = list(self._csv_recordings)
        if self._csv_recording is not None:
            metadata["current_recording"] = self._csv_recording.to_dict()
        if self._last_recording is not None:
            metadata["last_recording"] = self._last_recording.to_dict()
        return metadata

    def metadata_sources(self) -> list[dict[str, Any]]:
        recordings: list[dict[str, Any]] = []
        if self._recording_run is not None:
            if self._recording_run.current is not None:
                recordings.append(self._recording_run.current.to_dict())
            recordings.extend(self._recording_run.recordings)
        elif self._csv_recordings:
            recordings.extend(self._csv_recordings)
        if self._csv_recording is not None:
            recordings.append(self._csv_recording.to_dict())
        if self._last_recording is not None:
            recordings.append(self._last_recording.to_dict())
        return recordings

    def active_recording_metadata(self) -> RecordingMetadata | None:
        if self._recording_run is not None and self._recording_run.current is not None:
            return self._recording_run.current
        return self._csv_recording

    def active_recording_report_dir(self) -> Path | None:
        if self._recording_run is not None:
            return self._recording_run.report_dir
        return self._csv_recording_report_dir

    def _start_camera_recording(
        self,
        settings: dict[str, Any],
        camera_recorder: RecordingCamera,
        report_root: Path,
        recording_label: str,
        frame_size: tuple[int, int],
    ) -> dict[str, Any]:
        recording_run = self._recording_run
        if recording_run is None or recording_run.report_root != report_root or recording_run.camera is not camera_recorder:
            recording_run = RecordingRun(
                report_root,
                camera_recorder,
                _CsvRecordingBackend(self),
                writer_factory=self._writer_factory,
            )
            self._recording_run = recording_run
        camera_recorder.set_recording_complete_callback(self.stop_recording)
        if settings["camera_preview_off_recording"]:
            preview_setter = getattr(camera_recorder, "set_preview_enabled", None)
            if callable(preview_setter):
                preview_setter(False)

        width, height = frame_size
        metadata = recording_run.start_recording(
            recording_label,
            width=width,
            height=height,
            fps=float(settings["camera_video_fps"]),
            max_frames=_frame_limit(settings.get("recording_max_frames")),
            max_time=_positive_or_none(settings.get("recording_max_seconds")),
            video_path=settings.get("video_path"),
            fluidics_csv_path=settings.get("fluidics_csv_path"),
        )
        self._last_recording = metadata
        return {
            "csv_path": metadata.fluidics_csv,
            "report_dir": str(recording_run.report_dir or ""),
            "recording": metadata.to_dict(),
        }

    def _start_csv_only_recording(
        self,
        settings: dict[str, Any],
        report_root: Path,
        recording_label: str,
    ) -> dict[str, Any]:
        report_dir = create_recording_report_dir(report_root)
        fluidics_dir = report_dir / "fluidics"
        fluidics_dir.mkdir(parents=True, exist_ok=True)
        recording_id = create_recording_id(recording_label)
        explicit_csv = settings.get("fluidics_csv_path")
        if explicit_csv:
            csv_file = Path(str(explicit_csv))
            fluidics_dir = csv_file.parent
            csv_filename = csv_file.name
        else:
            csv_filename = f"{recording_id}.csv"
        csv_path = self._start_csv_recording(str(fluidics_dir), csv_filename=csv_filename)
        metadata = RecordingMetadata(
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            started_monotonic_s=time.monotonic(),
            recording_id=recording_id,
            report_dir=str(report_dir),
            output_dir="",
            video_prefix=recording_id,
            width=0,
            height=0,
            fps=0.0,
            fluidics_csv=csv_path,
        )
        self._csv_recording = metadata
        self._csv_recording_report_dir = report_dir
        self._last_recording = metadata
        return {"csv_path": csv_path, "report_dir": str(report_dir), "recording": metadata.to_dict()}

    def _start_csv_recording(
        self,
        fluidics_dir_value: str,
        *,
        csv_prefix: str = "fluidics",
        csv_filename: str = "",
    ) -> str:
        fluidics_dir = Path(fluidics_dir_value)
        self.csv_logger = CsvLogger(fluidics_dir, prefix=csv_prefix, filename=csv_filename)
        state = self._hardware_state()
        filepath = self.csv_logger.start(
            len(state.pressure_channels),
            len(state.sensor_channels),
        )
        self._recording = True
        acquisition = self._acquisition()
        if acquisition:
            acquisition.set_csv_logger(self.csv_logger)
        return filepath

    def _stop_csv_recording(self) -> None:
        self._recording = False
        acquisition = self._acquisition()
        if acquisition:
            acquisition.set_csv_logger(None)
        self.csv_logger.stop()

    def _restore_camera_preview_after_recording(self) -> None:
        if self._recording_run is not None:
            camera = getattr(self._recording_run, "camera", None)
            preview_setter = getattr(camera, "set_preview_enabled", None)
            if callable(preview_setter):
                preview_setter(True)

    def _recording_root(self, settings: dict[str, Any]) -> Path:
        if settings.get("video_path"):
            return Path(str(settings["video_path"])).parent.parent
        if settings.get("fluidics_csv_path"):
            return Path(str(settings["fluidics_csv_path"])).parent.parent
        recording_root_value = str(settings["recording_root"]).strip()
        if not recording_root_value:
            raise RuntimeError("Recording root is not configured")
        path = Path(recording_root_value)
        return path


class _CsvRecordingBackend:
    def __init__(
        self,
        coordinator: RecordingCoordinator,
    ) -> None:
        self.coordinator = coordinator

    def start_recording(self, settings: dict[str, Any]) -> str:
        return self.coordinator._start_csv_recording(
            settings["fluidics_dir"],
            csv_prefix=str(settings.get("csv_prefix") or "fluidics"),
            csv_filename=str(settings.get("csv_filename") or ""),
        )

    def stop_recording(self) -> None:
        self.coordinator._stop_csv_recording()


def _default_writer_factory(video_dir: Path, prefix: str, width: int, height: int, fps: float):
    return VideoWorker(video_dir, prefix, width, height, fps)


def create_recording_report_dir(report_root: str | Path) -> Path:
    root = Path(report_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def create_recording_id(recording_label: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{_safe_recording_label(recording_label)}_{stamp}"


def _safe_recording_label(value: str) -> str:
    label = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value).strip())
    label = "_".join(part for part in label.split("_") if part)
    return label or "recording"


def _writer_frame_count(camera: RecordingCamera) -> int | None:
    writer = getattr(camera, "writer", None)
    if writer is None:
        return getattr(camera, "last_writer_frame_count", None)
    return getattr(writer, "frame_count", None)


def _last_recording_frames(camera: RecordingCamera) -> int | None:
    return getattr(camera, "last_recording_frames", None)
