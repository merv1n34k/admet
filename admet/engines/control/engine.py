from __future__ import annotations

import logging
import time
from dataclasses import asdict
from pathlib import Path
from queue import Queue
from threading import Lock
from typing import Any, Callable

import numpy as np

from admet.core.engine import ActionSpec, EngineContext, EngineResult, validate_action_settings
from admet.core.schema import ResultSet, SummaryStat
from admet.engines.control.fluidics import (
    AcquisitionThread,
    ChannelManager,
    CsvLogger,
    FluigentConnectionError,
    FluigentSDK,
    HardwareManager,
    SDKAvailability,
)
from admet.engines.control.camera import Camera, CameraAcquisitionThread
from admet.engines.control.fluidics.config import (
    FLUIDIC_CHANNELS,
    PIPELINES,
    SENSOR_CALIBRATIONS,
    ProtocolStep,
)
from admet.engines.control.pipeline import (
    PipelineEngine,
    PipelineEvent,
    PipelineState,
    PipelineStep,
    create_trigger,
)
from admet.engines.control.session import (
    RecordingMetadata,
    RecordingSession,
    WriterFactory,
    create_recording_id,
    create_recording_report_dir,
    write_recording_metadata,
)
from admet.engines.control.settings import CONTROL_ENGINE_SETTINGS, CORRECTION_PARAM_NAMES


log = logging.getLogger(__name__)

CAMERA_CONFIGURATION_PARAMS = (
    "camera_width",
    "camera_height",
    "camera_offset_x",
    "camera_offset_y",
    "camera_binning_h",
    "camera_binning_v",
    "camera_exposure_us",
    "camera_gain",
    "camera_pixel_format",
    "camera_readout",
    "camera_framerate_enabled",
    "camera_framerate_hz",
    "camera_throughput_enabled",
    "camera_throughput_mbps",
    "camera_waterfall",
)

CONTROL_ACTIONS = (
    ActionSpec("connect_fluidics", "Connect Fluidics", "connection", params=("simulated", "start_polling")),
    ActionSpec("disconnect_fluidics", "Disconnect Fluidics", "connection"),
    ActionSpec("verify_backend", "Verify Backend", "diagnostics"),
    ActionSpec("verify_fluigent", "Verify Fluigent", "diagnostics", params=("simulated",)),
    ActionSpec("refresh_cameras", "Refresh Cameras", "diagnostics"),
    ActionSpec(
        "connect_camera",
        "Connect Camera",
        "connection",
        params=("camera_index", *CAMERA_CONFIGURATION_PARAMS),
    ),
    ActionSpec("disconnect_camera", "Disconnect Camera", "connection"),
    ActionSpec("apply_camera_settings", "Apply Camera Settings", "diagnostics", params=CAMERA_CONFIGURATION_PARAMS),
    ActionSpec("start_camera_live", "Start Camera Live", "diagnostics"),
    ActionSpec("stop_camera_live", "Stop Camera Live", "diagnostics"),
    ActionSpec("camera_status", "Camera Status", "diagnostics"),
    ActionSpec("start_polling", "Start Polling", "diagnostics"),
    ActionSpec("stop_polling", "Stop Polling", "diagnostics"),
    ActionSpec("apply_corrections", "Apply Corrections", "fluidics", params=CORRECTION_PARAM_NAMES),
    ActionSpec("set_channel_flow", "Set Channel Flow", "fluidics", params=("channel_index", "channel_flow_ul_min")),
    ActionSpec(
        "set_channel_pressure",
        "Set Channel Pressure",
        "fluidics",
        params=("channel_index", "channel_pressure_mbar"),
    ),
    ActionSpec("stop_channel", "Stop Channel", "fluidics", params=("channel_index",)),
    ActionSpec(
        "set_channel_response",
        "Set Channel Response",
        "fluidics",
        params=("channel_index", "channel_response_s"),
    ),
    ActionSpec(
        "start_recording",
        "Start Recording",
        "recording",
        params=(
            "recording_root",
            "recording_label",
            "camera_width",
            "camera_height",
            "camera_video_fps",
            "camera_preview_off_recording",
        ),
    ),
    ActionSpec("stop_recording", "Stop Recording", "recording"),
    ActionSpec(
        "run_protocol",
        "Run Protocol",
        "protocol",
        params=(
            "pipeline_name",
            "prime_oil_volume_ul",
            "prime_aqueous_volume_ul",
            "set_count",
            "replicate_count",
            "run_volume_ul",
            "run_aqueous_total_flow_ul_min",
            "wash_oil_flow_ul_min",
            "wash_aqueous_total_flow_ul_min",
            "wash_oil_volume_ul",
            "wash_pressure_mbar",
            "wash_pressure_duration_s",
            "tick_s",
        ),
    ),
    ActionSpec("pause_protocol", "Pause Protocol", "protocol"),
    ActionSpec("resume_protocol", "Resume Protocol", "protocol"),
    ActionSpec("stop_protocol", "Stop Protocol", "protocol"),
    ActionSpec("confirm_protocol", "Confirm Protocol Step", "protocol"),
    ActionSpec("skip_protocol", "Skip Protocol Step", "protocol"),
    ActionSpec("calibrate", "Calibrate", "calibration"),
    ActionSpec(
        "wash",
        "Wash",
        "protocol",
        params=(
            "wash_oil_flow_ul_min",
            "wash_aqueous_total_flow_ul_min",
            "wash_oil_volume_ul",
            "wash_pressure_mbar",
            "wash_pressure_duration_s",
            "tick_s",
        ),
    ),
)


class FluidicsControlEngine:
    id = "fluidics"
    name = "Fluigent Fluidics Control"
    settings = CONTROL_ENGINE_SETTINGS
    actions = CONTROL_ACTIONS

    def __init__(
        self,
        sdk: FluigentSDK | None = None,
        *,
        video_writer_factory: WriterFactory | None = None,
    ):
        self.sdk = sdk or FluigentSDK()
        self.hardware = HardwareManager(self.sdk)
        self.channel_manager = ChannelManager(self.sdk)
        self.data_queue: Queue = Queue(maxsize=50)
        self.pipeline_queue: Queue[PipelineEvent] = Queue(maxsize=50)
        self.csv_logger = CsvLogger()
        self.camera = Camera()
        self._camera_acquisition: CameraAcquisitionThread | None = None
        self._camera_preview_size = (0, 0)
        self._camera_last_frame: np.ndarray | None = None
        self._camera_stats: dict[str, Any] = {}
        self._camera_preflight_cache: dict[str, Any] = {}
        self._camera_frame_callbacks: list[Callable[[np.ndarray], None]] = []
        self._camera_lock = Lock()
        self._acquisition: AcquisitionThread | None = None
        self._pipeline: PipelineEngine | None = None
        self._recording = False
        self._recording_session: RecordingSession | None = None
        self._csv_recording: RecordingMetadata | None = None
        self._csv_recording_report_dir: Path | None = None
        self._csv_recordings: list[dict[str, Any]] = []
        self._last_recording: RecordingMetadata | None = None
        self._video_writer_factory = video_writer_factory
        self._corrected_sensors: set[int] = set()

    def run_action(
        self,
        action: str,
        settings: dict[str, Any],
        context: EngineContext | None = None,
    ) -> EngineResult:
        normalized = validate_action_settings(self.settings, self.actions, action, settings)
        if action == "connect_fluidics":
            return self._connect(normalized)
        if action == "verify_backend":
            return self._status_result(action, extra_metadata=self._backend_preflight())
        if action == "verify_fluigent":
            return self._verify_fluigent(normalized)
        if action == "refresh_cameras":
            return self._status_result(action, extra_metadata=self._camera_preflight())
        if action == "connect_camera":
            return self._connect_camera(normalized)
        if action == "disconnect_camera":
            self.stop_camera_live()
            self.camera.close()
            return self._status_result(action, extra_metadata=self._camera_preflight())
        if action == "apply_camera_settings":
            return self._apply_camera_settings(normalized)
        if action == "start_camera_live":
            self.start_camera_live()
            return self._status_result(action, extra_metadata=self._camera_status_metadata())
        if action == "stop_camera_live":
            self.stop_camera_live()
            return self._status_result(action, extra_metadata=self._camera_status_metadata())
        if action == "camera_status":
            return self._status_result(action, extra_metadata=self._camera_status_metadata())
        if action == "disconnect_fluidics":
            return self._disconnect(action)
        if action == "start_polling":
            self.start_polling()
            return self._status_result(action)
        if action == "stop_polling":
            self.stop_polling()
            return self._status_result(action)
        if action == "apply_corrections":
            self.apply_corrections(normalized)
            return self._status_result(action)
        if action == "set_channel_flow":
            self.set_channel_flow(normalized["channel_index"], normalized["channel_flow_ul_min"])
            return self._status_result(action)
        if action == "set_channel_pressure":
            self.set_channel_pressure(normalized["channel_index"], normalized["channel_pressure_mbar"])
            return self._status_result(action)
        if action == "stop_channel":
            self.stop_channel(normalized["channel_index"])
            return self._status_result(action)
        if action == "set_channel_response":
            self.set_channel_response(normalized["channel_index"], normalized["channel_response_s"])
            return self._status_result(action)
        if action == "start_recording":
            artifacts = self.start_recording(normalized, context)
            return self._status_result(action, artifacts=artifacts)
        if action == "stop_recording":
            artifacts = self.stop_recording()
            return self._status_result(action, artifacts=artifacts)
        if action == "run_protocol":
            self.start_pipeline(normalized["pipeline_name"], settings=normalized, tick_s=normalized["tick_s"])
            return self._status_result(action)
        if action == "pause_protocol":
            self.pause_pipeline()
            return self._status_result(action)
        if action == "resume_protocol":
            self.resume_pipeline()
            return self._status_result(action)
        if action == "stop_protocol":
            self.stop_pipeline()
            return self._status_result(action)
        if action == "confirm_protocol":
            self.confirm_pipeline_step()
            return self._status_result(action)
        if action == "skip_protocol":
            self.skip_pipeline_step()
            return self._status_result(action)
        if action == "calibrate":
            self.hardware.calibrate_all()
            return self._status_result(action)
        if action == "wash":
            self.start_pipeline("Wash", settings=normalized, tick_s=normalized["tick_s"])
            return self._status_result(action)
        raise ValueError(f"unsupported fluidics action: {action}")

    @property
    def polling_active(self) -> bool:
        return self._acquisition is not None and self._acquisition.is_alive()

    @property
    def recording_active(self) -> bool:
        return self._recording

    @property
    def pipeline_state(self) -> PipelineState:
        if self._pipeline:
            return self._pipeline.state
        return PipelineState.IDLE

    def start_polling(self) -> None:
        if self.polling_active:
            return
        if not self.hardware.connected:
            raise RuntimeError("Fluidics hardware is not connected")

        state = self.hardware.state
        self._acquisition = AcquisitionThread(
            self.sdk,
            pressure_count=len(state.pressure_channels),
            sensor_count=len(state.sensor_channels),
            data_queue=self.data_queue,
            csv_logger=self.csv_logger if self._recording else None,
        )
        self._acquisition.start()

    def stop_polling(self) -> None:
        if self._acquisition and self._acquisition.is_alive():
            self._acquisition.stop()
            self._acquisition.join(timeout=2.0)
        self._acquisition = None

    def apply_corrections(self, settings: dict[str, Any]) -> None:
        self._require_fluidics_connected()
        channels = self.channel_manager.channels
        for index, (prefix, _label, _calibration, _scale, _offset, _quadratic) in enumerate(FLUIDIC_CHANNELS):
            if index >= len(channels):
                break
            channel = channels[index]
            calibration_name = settings[f"{prefix}_calibration"]
            calibration = SENSOR_CALIBRATIONS.get(calibration_name, 0)
            self.sdk.set_sensor_calibration(channel.sensor_index, calibration)
            self.sdk.set_sensor_custom_scale(
                channel.sensor_index,
                settings[f"{prefix}_scale"],
                settings[f"{prefix}_offset"],
                settings[f"{prefix}_quadratic"],
            )

    def set_channel_flow(self, channel_index: int, flow_ul_min: float) -> None:
        self._require_fluidics_connected()
        self.channel_manager.user_set_flow_regulation(channel_index, flow_ul_min)

    def set_channel_pressure(self, channel_index: int, pressure_mbar: float) -> None:
        self._require_fluidics_connected()
        self.channel_manager.user_set_pressure(channel_index, pressure_mbar)

    def stop_channel(self, channel_index: int) -> None:
        self._require_fluidics_connected()
        self.channel_manager.user_stop_regulation(channel_index)

    def set_channel_response(self, channel_index: int, response_s: int) -> None:
        self._require_fluidics_connected()
        channels = self.channel_manager.channels
        channel = channels[channel_index]
        self.sdk.set_sensor_regulation_response(channel.sensor_index, response_s)

    def _require_fluidics_connected(self) -> None:
        if not self.hardware.connected:
            raise RuntimeError("Fluidics hardware is not connected")

    def start_recording(
        self,
        settings: dict[str, Any],
        context: EngineContext | None = None,
    ) -> dict[str, Any]:
        if self._recording:
            recording = self._active_recording_metadata()
            return {
                "csv_path": self.csv_logger.filepath or "",
                "report_dir": str(self._active_recording_report_dir() or ""),
                "recording": recording.to_dict() if recording is not None else {},
            }
        if not self.hardware.connected:
            raise RuntimeError("Fluidics hardware is not connected")

        recording_root_value = str(settings["recording_root"]).strip()
        if not recording_root_value:
            raise RuntimeError("Recording root is not configured")
        report_root = self._resolve_path(recording_root_value, context)
        recording_label = str(settings["recording_label"])
        camera_recorder = self._camera_acquisition if self.camera_live else None
        if camera_recorder is not None:
            session = self._recording_session
            if session is None or session.report_root != report_root:
                session = RecordingSession(
                    report_root,
                    camera_recorder,
                    _CsvRecordingBackend(self, context),
                    writer_factory=self._video_writer_factory,
                )
                self._recording_session = session
            camera_recorder.set_recording_complete_callback(self._on_recording_complete)
            if settings["camera_preview_off_recording"]:
                camera_recorder.set_preview_enabled(False)

            width, height = self._recording_frame_size(settings)
            metadata = session.start_recording(
                recording_label,
                width=width,
                height=height,
                fps=float(settings["camera_video_fps"]),
            )
            self._last_recording = metadata
            return {
                "csv_path": metadata.fluidics_csv,
                "report_dir": str(session.report_dir or ""),
                "recording": metadata.to_dict(),
            }

        report_dir = create_recording_report_dir(report_root)
        fluidics_dir = report_dir / "fluidics"
        fluidics_dir.mkdir(parents=True, exist_ok=True)
        if self._csv_recording_report_dir != report_dir:
            self._csv_recordings = []
        recording_id = create_recording_id(recording_label)
        csv_path = self._start_csv_recording(
            str(fluidics_dir),
            context,
            csv_filename=f"{recording_id}.csv",
        )
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
        write_recording_metadata(report_dir, self._csv_recordings, current=metadata)
        return {"csv_path": csv_path, "report_dir": str(report_dir), "recording": metadata.to_dict()}

    def _start_csv_recording(
        self,
        fluidics_dir_value: str,
        context: EngineContext | None = None,
        *,
        csv_prefix: str = "fluidics",
        csv_filename: str = "",
    ) -> str:
        fluidics_dir = Path(fluidics_dir_value)
        if context and context.workdir and not fluidics_dir.is_absolute():
            fluidics_dir = Path(context.workdir) / fluidics_dir
        self.csv_logger = CsvLogger(fluidics_dir, prefix=csv_prefix, filename=csv_filename)
        state = self.hardware.state
        filepath = self.csv_logger.start(
            len(state.pressure_channels),
            len(state.sensor_channels),
        )
        self._recording = True
        if self._acquisition:
            self._acquisition.set_csv_logger(self.csv_logger)
        return filepath

    def stop_recording(self) -> dict[str, Any]:
        if self._recording_session is not None and self._recording_session.current is not None:
            metadata = self._recording_session.stop_recording()
            self._restore_camera_preview_after_recording()
            if metadata is not None:
                self._last_recording = metadata
                recording = metadata.to_dict()
                return {
                    "csv_path": metadata.fluidics_csv,
                    "report_dir": str(self._recording_session.report_dir or ""),
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
            write_recording_metadata(self._csv_recording_report_dir, self._csv_recordings)
        self._csv_recording = None
        report_dir = str(self._csv_recording_report_dir or "")
        self._csv_recording_report_dir = None
        return {
            "csv_path": metadata.fluidics_csv,
            "report_dir": report_dir,
            "recording": metadata.to_dict(),
        }

    def _stop_csv_recording(self) -> None:
        self._recording = False
        if self._acquisition:
            self._acquisition.set_csv_logger(None)
        self.csv_logger.stop()

    def _on_recording_complete(self) -> None:
        self.stop_recording()

    def _restore_camera_preview_after_recording(self) -> None:
        if self._camera_acquisition is not None:
            self._camera_acquisition.set_preview_enabled(True)

    def _resolve_path(
        self,
        value: str,
        context: EngineContext | None = None,
    ) -> Path:
        path = Path(value)
        if context and context.workdir and not path.is_absolute():
            return Path(context.workdir) / path
        return path

    def _recording_frame_size(self, settings: dict[str, Any]) -> tuple[int, int]:
        with self._camera_lock:
            frame = self._camera_last_frame
            if frame is not None:
                return int(frame.shape[1]), int(frame.shape[0])
            preview_size = self._camera_preview_size
        if preview_size[0] and preview_size[1]:
            return preview_size
        return int(settings["camera_width"]), int(settings["camera_height"])

    def _active_recording_metadata(self) -> RecordingMetadata | None:
        if self._recording_session is not None and self._recording_session.current is not None:
            return self._recording_session.current
        return self._csv_recording

    def _active_recording_report_dir(self) -> Path | None:
        if self._recording_session is not None:
            return self._recording_session.report_dir
        return self._csv_recording_report_dir

    def start_pipeline(
        self,
        name: str,
        *,
        settings: dict[str, Any] | None = None,
        tick_s: float = 0.2,
    ) -> None:
        if self._pipeline and self._pipeline.is_alive():
            return
        if name == "Priming" and settings:
            steps = self.build_pipeline_from_steps(self._priming_protocol(settings))
        elif name == "Drop-Seq" and settings:
            steps = self.build_pipeline_from_steps(self._dropseq_run_protocol(settings))
        elif name == "Wash" and settings:
            steps = self.build_pipeline_from_steps(self._wash_protocol(settings))
        else:
            steps = self.build_pipeline(name)
        sensor_to_channel = {
            channel.sensor_index: channel_index
            for channel_index, channel in enumerate(self.channel_manager.channels)
        }
        self._pipeline = PipelineEngine(
            steps,
            self.channel_manager,
            self._acquisition,
            self.pipeline_queue,
            sensor_to_channel,
            tick_s=tick_s,
        )
        self._pipeline.start()

    def stop_pipeline(self) -> None:
        if self._pipeline and self._pipeline.is_alive():
            self._pipeline.stop()
            self._pipeline.join(timeout=3.0)
        self._pipeline = None

    def pause_pipeline(self) -> None:
        if self._pipeline:
            self._pipeline.pause()

    def resume_pipeline(self) -> None:
        if self._pipeline:
            self._pipeline.resume()

    def skip_pipeline_step(self) -> None:
        if self._pipeline:
            self._pipeline.skip_step()

    def confirm_pipeline_step(self) -> None:
        if self._pipeline:
            self._pipeline.confirm_pending()

    def build_pipeline(self, name: str) -> list[PipelineStep]:
        protocol = PIPELINES.get(name)
        if protocol is None:
            raise ValueError(f"Unknown pipeline: {name}")
        return self.build_pipeline_from_steps(protocol)

    def build_pipeline_from_steps(self, steps: list[ProtocolStep]) -> list[PipelineStep]:
        return [
            PipelineStep(
                name=step.name,
                sensor_setpoints=dict(step.sensor_setpoints),
                trigger=create_trigger(step.trigger_type, step.trigger_params),
                pressure_setpoints=dict(step.pressure_setpoints),
                on_complete=step.on_complete,
                confirm_message=step.confirm_message,
            )
            for step in _expand_steps(steps)
        ]

    def _priming_protocol(self, settings: dict[str, Any]) -> list[ProtocolStep]:
        oil_volume = float(settings["prime_oil_volume_ul"])
        aqueous_volume = float(settings["prime_aqueous_volume_ul"])
        return [
            ProtocolStep(
                name="Prime Oil L",
                sensor_setpoints={0: 250.0},
                trigger_type="volume",
                trigger_params={"sensor_index": 0, "target_volume_ul": oil_volume},
                on_complete="zero",
                confirm_message=f"Prime Oil L at 250 uL/min for {oil_volume:g} uL. Proceed?",
            ),
            ProtocolStep(
                name="Prime Cells M",
                sensor_setpoints={1: 67.0},
                trigger_type="volume",
                trigger_params={"sensor_index": 1, "target_volume_ul": aqueous_volume},
                on_complete="zero",
                confirm_message=f"Prime Cells M at 67 uL/min for {aqueous_volume:g} uL. Proceed?",
            ),
            ProtocolStep(
                name="Prime Beads M",
                sensor_setpoints={2: 67.0},
                trigger_type="volume",
                trigger_params={"sensor_index": 2, "target_volume_ul": aqueous_volume},
                on_complete="zero",
                confirm_message=f"Prime Beads M at 67 uL/min for {aqueous_volume:g} uL. Proceed?",
            ),
        ]

    def _dropseq_run_protocol(self, settings: dict[str, Any]) -> list[ProtocolStep]:
        steps: list[ProtocolStep] = []
        set_count = int(settings["set_count"])
        replicate_count = int(settings["replicate_count"])
        run_volume_ul = float(settings["run_volume_ul"])
        aqueous_total = float(settings["run_aqueous_total_flow_ul_min"])
        aqueous_channel = aqueous_total / 2.0
        for set_index in range(1, set_count + 1):
            for replicate_index in range(1, replicate_count + 1):
                label = f"set{set_index:02d}_rep{replicate_index:02d}"
                steps.extend(
                    (
                        ProtocolStep(
                            name=f"Run {label}",
                            sensor_setpoints={0: 300.0, 1: aqueous_channel, 2: aqueous_channel},
                            trigger_type="volume",
                            trigger_params={"sensor_index": 0, "target_volume_ul": run_volume_ul},
                            on_complete="zero",
                            confirm_message=(
                                f"Start {label}: Oil L 300 uL/min, "
                                f"Cells M/Beads M {aqueous_channel:g} uL/min?"
                            ),
                        ),
                        ProtocolStep(
                            name=f"Confirm {label}",
                            sensor_setpoints={},
                            trigger_type="time",
                            trigger_params={"duration_s": 0.0},
                            confirm_message=f"{label} complete. Confirm before continuing.",
                        ),
                    )
                )
        return steps

    def _wash_protocol(self, settings: dict[str, Any]) -> list[ProtocolStep]:
        oil_flow = float(settings["wash_oil_flow_ul_min"])
        aqueous_channel = float(settings["wash_aqueous_total_flow_ul_min"]) / 2.0
        oil_volume = float(settings["wash_oil_volume_ul"])
        pressure = float(settings["wash_pressure_mbar"])
        duration_s = float(settings["wash_pressure_duration_s"])
        return [
            ProtocolStep(
                name="Wash flow phase",
                sensor_setpoints={0: oil_flow, 1: aqueous_channel, 2: aqueous_channel},
                trigger_type="volume",
                trigger_params={"sensor_index": 0, "target_volume_ul": oil_volume},
                on_complete="zero",
                confirm_message=(
                    f"Start wash phase 1: {oil_flow:g}/{aqueous_channel:g}/{aqueous_channel:g} "
                    f"uL/min until Oil L dispenses {oil_volume:g} uL?"
                ),
            ),
            ProtocolStep(
                name="Wash pressure phase",
                sensor_setpoints={},
                pressure_setpoints={0: pressure, 1: pressure, 2: pressure},
                trigger_type="time",
                trigger_params={"duration_s": duration_s},
                on_complete="zero",
                confirm_message=(
                    f"Set all pressure channels to {pressure:g} mbar for {duration_s:g} seconds?"
                ),
            ),
            ProtocolStep(
                name="Confirm wash complete",
                sensor_setpoints={},
                trigger_type="time",
                trigger_params={"duration_s": 0.0},
                confirm_message="Pressure wash complete. Confirm pipeline close.",
            ),
        ]

    def _connect(self, settings: dict[str, Any]) -> EngineResult:
        try:
            state = self.hardware.connect(simulated=settings["simulated"])
        except FluigentConnectionError as exc:
            return self._status_result(
                "connect_fluidics",
                extra_metadata={
                    "fluigent_connect_ok": False,
                    "fluigent_connect_message": str(exc),
                    "fluigent_connect_error": str(exc),
                    "fluigent_connect_error_type": type(exc).__name__,
                },
            )
        pairs = [
            (sensor.index, pressure.index)
            for sensor, pressure in zip(state.sensor_channels, state.pressure_channels, strict=False)
        ]
        self.channel_manager.configure_channels(pairs)
        if settings["simulated"]:
            self._corrected_sensors = {channel.index for channel in state.sensor_channels}
        else:
            self._corrected_sensors = set()
        if settings["start_polling"]:
            self.start_polling()
        return self._status_result(
            "connect_fluidics",
            extra_metadata={
                "fluigent_connect_ok": True,
                "fluigent_connect_message": "Fluigent connected.",
            },
        )

    def _disconnect(self, action: str) -> EngineResult:
        self.stop_recording()
        self.stop_polling()
        self.stop_pipeline()
        self.hardware.disconnect()
        self.channel_manager.configure_channels([])
        return self._status_result(action)

    def _connect_camera(self, settings: dict[str, Any]) -> EngineResult:
        metadata = self._camera_preflight()
        if not metadata["pypylon_available"]:
            metadata.update(
                {
                    "camera_connect_ok": False,
                    "camera_connect_message": metadata["camera_message"],
                }
            )
            return self._status_result("connect_camera", extra_metadata=metadata)
        if metadata["camera_count"] <= settings["camera_index"]:
            metadata.update(
                {
                    "camera_connect_ok": False,
                    "camera_connect_message": "Camera is not currently available; refresh after attaching it.",
                }
            )
            return self._status_result("connect_camera", extra_metadata=metadata)

        ok = self.camera.open(settings["camera_index"])
        metadata = self._camera_preflight()
        metadata.update(
            {
                "camera_connect_ok": ok,
                "camera_connect_message": "Camera connected." if ok else "Camera open failed.",
                "camera_connected": self.camera.connected,
            }
        )
        if ok:
            try:
                self._apply_camera_configuration(settings)
                metadata["camera_settings_ok"] = True
                metadata["camera_settings_message"] = "Camera defaults applied."
            except Exception as exc:
                metadata["camera_settings_ok"] = False
                metadata["camera_settings_message"] = str(exc)
            metadata.update(self._read_camera_parameters())
        return self._status_result("connect_camera", extra_metadata=metadata)

    def _apply_camera_settings(self, settings: dict[str, Any]) -> EngineResult:
        if not self.camera.connected:
            metadata = self._camera_status_metadata()
            metadata["camera_settings_ok"] = False
            metadata["camera_settings_message"] = "Camera is not connected."
            return self._status_result("apply_camera_settings", extra_metadata=metadata)

        was_live = self.camera_live
        if was_live:
            self.stop_camera_live()

        try:
            self._apply_camera_configuration(settings)
            ok = True
            message = "Camera settings applied."
        except Exception as exc:
            ok = False
            message = str(exc)
        finally:
            if was_live:
                time.sleep(0.05)
                self.start_camera_live()

        metadata = self._camera_status_metadata()
        metadata.update(self._read_camera_parameters())
        metadata["camera_settings_ok"] = ok
        metadata["camera_settings_message"] = message
        return self._status_result("apply_camera_settings", extra_metadata=metadata)

    @property
    def camera_live(self) -> bool:
        return self._camera_acquisition is not None and self._camera_acquisition.is_alive()

    def start_camera_live(self) -> None:
        if self.camera_live:
            return
        if not self.camera.connected:
            raise RuntimeError("Camera is not connected")
        self._camera_acquisition = CameraAcquisitionThread(
            self.camera,
            preview_callback=self._on_camera_frame,
            stats_callback=self._on_camera_stats,
        )
        self._camera_acquisition.start()

    def stop_camera_live(self) -> None:
        if self._camera_acquisition:
            self._camera_acquisition.stop()
        self._camera_acquisition = None

    def _apply_camera_configuration(self, settings: dict[str, Any]) -> None:
        offset_x = _snap_camera_offset(settings["camera_offset_x"])
        offset_y = _snap_camera_offset(settings["camera_offset_y"])
        if offset_x != 0 or offset_y != 0:
            self.camera.set_parameter("OffsetX", 0)
            self.camera.set_parameter("OffsetY", 0)

        height = 1 if settings["camera_waterfall"] else settings["camera_height"]
        cam_settings: dict[str, Any] = {
            "Width": settings["camera_width"],
            "Height": height,
            "OffsetX": offset_x,
            "OffsetY": offset_y,
            "BinningHorizontal": settings["camera_binning_h"],
            "BinningVertical": settings["camera_binning_v"],
            "ExposureTime": settings["camera_exposure_us"],
            "Gain": settings["camera_gain"],
            "PixelFormat": settings["camera_pixel_format"],
            "SensorReadoutMode": settings["camera_readout"],
        }
        if settings["camera_framerate_enabled"]:
            cam_settings["AcquisitionFrameRateEnable"] = True
            cam_settings["AcquisitionFrameRate"] = settings["camera_framerate_hz"]
        else:
            cam_settings["AcquisitionFrameRateEnable"] = False
        if settings["camera_throughput_enabled"]:
            cam_settings["DeviceLinkThroughputLimitMode"] = "On"
            cam_settings["DeviceLinkThroughputLimit"] = int(settings["camera_throughput_mbps"] * 1_000_000)
        else:
            cam_settings["DeviceLinkThroughputLimitMode"] = "Off"
        self.camera.apply_settings(cam_settings)

    def _verify_fluigent(self, settings: dict[str, Any]) -> EngineResult:
        metadata: dict[str, Any] = {
            "fluigent_connect_ok": False,
            "fluigent_connect_error": "",
            "fluigent_connect_error_type": "",
        }
        if self.hardware.connected:
            metadata["fluigent_connect_ok"] = True
            metadata["fluigent_connect_message"] = "Fluigent is already connected."
            return self._status_result("verify_fluigent", extra_metadata=metadata)

        try:
            state = self.hardware.connect(simulated=settings["simulated"])
            pairs = [
                (sensor.index, pressure.index)
                for sensor, pressure in zip(
                    state.sensor_channels,
                    state.pressure_channels,
                    strict=False,
                )
            ]
            self.channel_manager.configure_channels(pairs)
            metadata.update(
                {
                    "fluigent_connect_ok": True,
                    "fluigent_connect_message": "Fluigent connection check succeeded.",
                    "fluigent_verified_simulated": state.simulated,
                    "fluigent_verified_pressure_channels": len(state.pressure_channels),
                    "fluigent_verified_sensor_channels": len(state.sensor_channels),
                }
            )
        except Exception as exc:
            metadata.update(
                {
                    "fluigent_connect_error": str(exc),
                    "fluigent_connect_error_type": type(exc).__name__,
                    "fluigent_connect_message": "Fluigent connection check failed.",
                }
            )
        finally:
            if self.hardware.connected:
                self.hardware.disconnect()
            self.channel_manager.configure_channels([])
        return self._status_result("verify_fluigent", extra_metadata=metadata)

    def _backend_preflight(self) -> dict[str, Any]:
        sdk_status = self._sdk_preflight()
        return {
            "fluigent_sdk": asdict(sdk_status),
            "fluigent_sdk_available": sdk_status.available,
            "fluigent_sdk_message": sdk_status.message,
            **self._fluigent_availability_metadata(sdk_status.available),
            **self._camera_preflight(),
        }

    def _fluigent_availability_metadata(self, sdk_available: bool) -> dict[str, Any]:
        if not sdk_available:
            return {
                "fluigent_detect_ok": False,
                "fluigent_instrument_count": 0,
                "fluigent_instruments": [],
                "fluigent_device_message": "Fluigent SDK is not available.",
            }
        try:
            instruments = self.hardware.detect_instruments()
        except Exception as exc:
            return {
                "fluigent_detect_ok": False,
                "fluigent_instrument_count": 0,
                "fluigent_instruments": [],
                "fluigent_device_message": str(exc),
            }
        count = len(instruments)
        message = (
            f"{count} Fluigent instrument(s) detected."
            if count
            else "No Fluigent instruments detected."
        )
        return {
            "fluigent_detect_ok": True,
            "fluigent_instrument_count": count,
            "fluigent_instruments": instruments,
            "fluigent_device_message": message,
        }

    def _camera_preflight(self) -> dict[str, Any]:
        camera_status = self.camera.preflight()
        camera_status_metadata = asdict(camera_status)
        metadata = {
            "camera": camera_status_metadata,
            "pypylon_available": camera_status.pypylon_available,
            "camera_refresh_ok": camera_status.refresh_ok,
            "camera_count": camera_status.camera_count,
            "cameras": list(camera_status.cameras),
            "camera_transport_layers": list(camera_status.transport_layers),
            "camera_connected": self.camera.connected,
            "pylon_module_loaded": camera_status.pylon_module_loaded,
            "camera_message": camera_status.message,
        }
        self._camera_preflight_cache = metadata
        metadata.update(self._camera_status_metadata())
        return metadata

    def _camera_status_metadata(self) -> dict[str, Any]:
        with self._camera_lock:
            preview_size = self._camera_preview_size
            stats = dict(self._camera_stats)
            frame_shape = list(self._camera_last_frame.shape) if self._camera_last_frame is not None else []
        metadata = {
            **self._camera_preflight_cache,
            "camera_connected": self.camera.connected,
            "camera_live": self.camera_live,
            "camera_preview_width": preview_size[0],
            "camera_preview_height": preview_size[1],
            "camera_frame_shape": frame_shape,
            "camera_fps": self.camera.get_resulting_framerate() if self.camera.connected else 0.0,
            "camera_recording": bool(stats.get("recording", False)),
            "camera_recorded_frames": int(stats.get("frames", 0)),
            "camera_record_elapsed": float(stats.get("elapsed", 0.0)),
        }
        metadata.update(self._read_camera_parameters())
        return metadata

    def _read_camera_parameters(self) -> dict[str, Any]:
        if not self.camera.connected:
            return {"camera_parameters": {}}
        names = [
            "Width",
            "Height",
            "OffsetX",
            "OffsetY",
            "ExposureTime",
            "Gain",
            "PixelFormat",
            "SensorReadoutMode",
            "BinningHorizontal",
            "BinningVertical",
            "AcquisitionFrameRate",
            "DeviceLinkThroughputLimit",
            "ResultingFrameRate",
        ]
        parameters = self.camera.get_settings(names)
        return {"camera_parameters": parameters}

    def _on_camera_frame(self, frame: np.ndarray) -> None:
        frame = frame.copy()
        with self._camera_lock:
            self._camera_last_frame = frame
            self._camera_preview_size = (frame.shape[1], frame.shape[0])
            callbacks = tuple(self._camera_frame_callbacks)
        if not callbacks:
            self.acknowledge_camera_frame()
            return
        delivered = False
        for callback in callbacks:
            try:
                callback(frame)
                delivered = True
            except Exception:
                log.exception("camera frame callback failed")
        if not delivered:
            self.acknowledge_camera_frame()

    def _on_camera_stats(self, stats: dict[str, Any]) -> None:
        with self._camera_lock:
            self._camera_stats = dict(stats)

    def latest_camera_frame(self) -> np.ndarray | None:
        with self._camera_lock:
            return self._camera_last_frame

    def subscribe_camera_frames(
        self,
        callback: Callable[[np.ndarray], None],
    ) -> Callable[[], None]:
        with self._camera_lock:
            self._camera_frame_callbacks.append(callback)

        def unsubscribe() -> None:
            with self._camera_lock:
                try:
                    self._camera_frame_callbacks.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def acknowledge_camera_frame(self) -> None:
        if self._camera_acquisition:
            self._camera_acquisition.frame_processed()

    def _sdk_preflight(self) -> SDKAvailability:
        if hasattr(self.sdk, "preflight"):
            return self.sdk.preflight()
        return SDKAvailability(
            available=True,
            source="injected",
            message="Fluigent SDK object is injected.",
        )

    def _status_result(
        self,
        action: str,
        artifacts: dict[str, Any] | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> EngineResult:
        state = self.hardware.state
        metadata = {
            "action": action,
            "connected": state.connected,
            "simulated": state.simulated,
            "pressure_channels": len(state.pressure_channels),
            "sensor_channels": len(state.sensor_channels),
            "polling_active": self.polling_active,
            "recording_active": self.recording_active,
            "pipeline_state": self.pipeline_state.value,
            "queued_snapshots": self.data_queue.qsize(),
            "queued_pipeline_events": self.pipeline_queue.qsize(),
        }
        if self._recording_session is not None:
            metadata["recordings"] = list(self._recording_session.recordings)
            if self._recording_session.current is not None:
                metadata["current_recording"] = self._recording_session.current.to_dict()
        elif self._csv_recordings:
            metadata["recordings"] = list(self._csv_recordings)
        if self._csv_recording is not None:
            metadata["current_recording"] = self._csv_recording.to_dict()
        if self._last_recording is not None:
            metadata["last_recording"] = self._last_recording.to_dict()
        if extra_metadata:
            metadata.update(extra_metadata)
        stats = (
            SummaryStat("pressure_channels", metadata["pressure_channels"]),
            SummaryStat("sensor_channels", metadata["sensor_channels"]),
        )
        return EngineResult(
            result_set=ResultSet(records=(), stats=stats, metadata=metadata),
            artifacts=artifacts or {},
        )


def create_engine() -> FluidicsControlEngine:
    return FluidicsControlEngine()


def _snap_camera_offset(value: int) -> int:
    return round(int(value) / 16) * 16


def _expand_steps(steps: list[ProtocolStep]) -> list[ProtocolStep]:
    expanded = []
    index = 0
    while index < len(steps):
        step = steps[index]
        if step.group:
            group_steps = []
            group_repeat = 1
            while index < len(steps) and steps[index].group == step.group:
                group_steps.append(steps[index])
                group_repeat = max(group_repeat, steps[index].repeat)
                index += 1
            for _ in range(group_repeat):
                expanded.extend(group_steps)
        else:
            for _ in range(max(1, step.repeat)):
                expanded.append(step)
            index += 1
    return expanded


class _CsvRecordingBackend:
    def __init__(
        self,
        engine: FluidicsControlEngine,
        context: EngineContext | None,
    ) -> None:
        self.engine = engine
        self.context = context

    def run_action(
        self,
        action: str,
        settings: dict[str, Any],
        context: EngineContext | None = None,
    ) -> EngineResult:
        if action == "start_recording":
            csv_path = self.engine._start_csv_recording(
                settings["fluidics_dir"],
                context or self.context,
                csv_prefix=str(settings.get("csv_prefix") or "fluidics"),
                csv_filename=str(settings.get("csv_filename") or ""),
            )
            return EngineResult(ResultSet(), artifacts={"csv_path": csv_path})
        if action == "stop_recording":
            self.engine._stop_csv_recording()
            return EngineResult(ResultSet())
        raise ValueError(f"unsupported recording action: {action}")
