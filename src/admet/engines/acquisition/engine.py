from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque
from dataclasses import asdict
from queue import Queue
from statistics import mean, stdev
from typing import Any, Callable

from admet.core.engine import READ, ActionSpec, ParamSchema, validate_action_settings
from admet.core.run import RunJob, RunResult
from admet.engines.acquisition.camera import CameraController
from admet.engines.acquisition.fluidics import (
    AcquisitionThread,
    ChannelManager,
    CsvLogger,
    FluigentConnectionError,
    FluigentSDK,
    HardwareManager,
    SDKAvailability,
)
from admet.engines.acquisition.fluidics.config import (
    FLUIDIC_CHANNEL_LABELS,
    FLUIDIC_CHANNELS,
    SENSOR_CALIBRATIONS,
    STABILITY_TOLERANCE_UL_MIN,
    STABILITY_WINDOW_SAMPLES,
)
from admet.engines.acquisition.recording import (
    RecordingCoordinator,
    WriterFactory,
)
from admet.engines.acquisition.pipeline import PipelineEngine, ProtocolStep, build_pipeline_steps
from admet.engines.acquisition.triggers import ConditionTrigger, TimeTrigger, VolumeTrigger
from admet.engines.acquisition.settings import CONTROL_ENGINE_SETTINGS, CORRECTION_PARAM_NAMES, MANUAL_STOPS

log = logging.getLogger(__name__)

# How long a flow unit may take to switch calibration tables.
TABLE_SWITCH_S = 3.0


class TailTracker:
    """How many seconds of flow each channel delivers after a stop, learned.

    Each stop that ends a volume step at zero is measured: the volume that
    arrives after the stop, divided by the flow at the stop, is k in seconds.
    The median of the most recent stops is what adaptive volume steps use.
    """

    def __init__(self, keep: int = 10):
        self._keep = keep
        self._samples: dict[int, deque] = {}
        self._errors: dict[int, deque] = {}
        self._lock = threading.Lock()

    def k(self, sensor: int) -> float | None:
        with self._lock:
            samples = self._samples.get(sensor)
            return float(statistics.median(samples)) if samples else None

    def record(self, sensor: int, k: float) -> None:
        with self._lock:
            self._samples.setdefault(sensor, deque(maxlen=self._keep)).append(k)

    def record_error(self, sensor: int, error_pct: float) -> None:
        """How far a settled dose landed from its target, in % of the target."""
        with self._lock:
            self._errors.setdefault(sensor, deque(maxlen=self._keep)).append(error_pct)

    def describe(self, sensor: int) -> dict:
        with self._lock:
            samples = list(self._samples.get(sensor, ()))
            errors = list(self._errors.get(sensor, ()))
        return {"tail_s": float(statistics.median(samples)) if samples else None,
                "tail_samples": len(samples),
                "error_pct": float(statistics.mean(errors)) if errors else None,
                "error_last_pct": errors[-1] if errors else None,
                "error_doses": len(errors)}

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()
            self._errors.clear()

ActionHandler = Callable[[dict[str, Any]], dict[str, Any]]
ActionSettingsPreparer = Callable[[RunJob, dict[str, Any]], dict[str, Any]]
ActionPrivateSettings = Callable[[RunJob], dict[str, Any]]
PipelineStepBuilder = Callable[[list[Any]], list[Any]]
PipelineEngineFactory = Callable[..., Any]

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
)

ACQUISITION_ACTIONS = (
    ActionSpec("connect_fluidics", "Connect Fluidics", "connection", params=("simulated", "start_polling")),
    ActionSpec("disconnect_fluidics", "Disconnect Fluidics", "connection"),
    ActionSpec("verify_backend", "Verify Backend", "diagnostics", kind=READ),
    ActionSpec("verify_fluigent", "Verify Fluigent", "diagnostics", kind=READ, params=("simulated",)),
    ActionSpec("list_cameras", "List Cameras", "diagnostics", kind=READ),
    ActionSpec(
        "connect_camera",
        "Connect Camera",
        "connection",
        params=("camera_index", *CAMERA_CONFIGURATION_PARAMS),
    ),
    ActionSpec("disconnect_camera", "Disconnect Camera", "connection"),
    ActionSpec("set_camera_settings", "Set Camera Settings", "diagnostics", params=CAMERA_CONFIGURATION_PARAMS),
    ActionSpec("start_camera_live", "Start Camera Live", "diagnostics"),
    ActionSpec("stop_camera_live", "Stop Camera Live", "diagnostics"),
    ActionSpec("read_status", "Read Status", "diagnostics", kind=READ),
    ActionSpec("read_observation", "Read Observation", "diagnostics", kind=READ),
    ActionSpec("emergency_stop", "Emergency Stop", "control"),
    ActionSpec("start_polling", "Start Polling", "diagnostics"),
    ActionSpec("stop_polling", "Stop Polling", "diagnostics"),
    ActionSpec("apply_corrections", "Apply Corrections", "fluidics", params=CORRECTION_PARAM_NAMES),
    ActionSpec("set_channel_flow", "Set Channel Flow", "fluidics",
               params=("channel_index", "channel_flow_ul_min", "channel_stop_after", "channel_stop_value")),
    ActionSpec(
        "set_channel_pressure",
        "Set Channel Pressure",
        "fluidics",
        params=("channel_index", "channel_pressure_mbar", "channel_stop_after", "channel_stop_value"),
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
            "include_video",
            "recording_max_frames",
            "recording_max_seconds",
            "camera_width",
            "camera_height",
            "camera_video_fps",
            "camera_preview_off_recording",
        ),
        outputs=("video", "fluidics_csv"),
    ),
    ActionSpec("stop_recording", "Stop Recording", "recording", artifact="control_recording"),
    ActionSpec("pause_protocol", "Pause Protocol", "protocol"),
    ActionSpec("resume_protocol", "Resume Protocol", "protocol"),
    ActionSpec("stop_protocol", "Stop Protocol", "protocol"),
    ActionSpec("confirm_protocol", "Confirm Protocol Step", "protocol"),
    ActionSpec("skip_protocol", "Skip Protocol Step", "protocol"),
    ActionSpec("calibrate_channels", "Calibrate Channels", "calibration"),
    ActionSpec("shutdown_instrument", "Shut Down Instrument", "connection"),
)


class AcquisitionEngine:
    id = "acquisition"
    name = "Synchronized Acquisition"
    actions = ACQUISITION_ACTIONS

    def __init__(
        self,
        settings: ParamSchema,
        *,
        pipeline_step_builder: PipelineStepBuilder,
        pipeline_engine_factory: PipelineEngineFactory,
        sdk: FluigentSDK | None = None,
        video_writer_factory: WriterFactory | None = None,
    ):
        self.settings = settings
        self._build_pipeline_steps = pipeline_step_builder
        self._pipeline_engine_factory = pipeline_engine_factory
        self.sdk = sdk or FluigentSDK()
        self.hardware = HardwareManager(self.sdk)
        self.channel_manager = ChannelManager(self.sdk)
        # Event numbering and history belong to the session, not to one
        # protocol. A client's cursor has to keep meaning what it meant after
        # the next protocol starts.
        self._event_sequence = 0
        self._event_history: deque = deque(maxlen=2000)
        self._event_lock = threading.Lock()
        self.tails = TailTracker()
        # Rules ending commands set by hand, by channel; each is watched in its own thread.
        self._manual_stops: dict[int, dict[str, Any]] = {}
        self._manual_lock = threading.Lock()
        self._event_condition = threading.Condition(self._event_lock)
        self.data_queue: Queue = Queue(maxsize=50)
        self.pipeline_queue: Queue = Queue(maxsize=50)
        self._camera = CameraController()
        self._acquisition: AcquisitionThread | None = None
        self._pipeline: Any | None = None
        self._recordings = RecordingCoordinator(
            csv_logger=CsvLogger(),
            hardware_state=lambda: self.hardware.state,
            acquisition=lambda: self._acquisition,
            writer_factory=video_writer_factory,
        )
        self._corrected_sensors: set[int] = set()
        self._action_preparers = self._build_action_preparers()
        self._private_action_settings = self._build_private_action_settings()
        self._action_handlers = self._build_action_handlers()
        self._validate_action_handlers()

    @property
    def camera(self) -> Any:
        return self._camera.camera

    @camera.setter
    def camera(self, camera: Any) -> None:
        self._camera.camera = camera

    def _build_action_handlers(self) -> dict[str, ActionHandler]:
        return {
            "connect_fluidics": lambda settings: self._connect(settings),
            "disconnect_fluidics": lambda _settings: self._disconnect("disconnect_fluidics"),
            "verify_backend": lambda _settings: self._status_result(
                "verify_backend",
                extra_metadata=self._backend_preflight(),
            ),
            "verify_fluigent": lambda settings: self._verify_fluigent(settings),
            "list_cameras": lambda _settings: self._status_result(
                "list_cameras",
                extra_metadata=self._camera_preflight(),
            ),
            "connect_camera": lambda settings: self._connect_camera(settings),
            "disconnect_camera": lambda _settings: self._disconnect_camera(),
            "set_camera_settings": lambda settings: self._apply_camera_settings(settings),
            "start_camera_live": lambda _settings: self._camera_live_status_action(
                "start_camera_live",
                self.start_camera_live,
            ),
            "stop_camera_live": lambda _settings: self._camera_live_status_action(
                "stop_camera_live",
                self.stop_camera_live,
            ),
            "read_status": lambda _settings: self._status_result(
                "read_status",
                extra_metadata=self._camera_status_metadata(),
            ),
            "read_observation": lambda _settings: self.observation(),
            "emergency_stop": lambda settings: self.emergency_stop(
                str(settings.get("stop_reason") or "asked for by the operator")
            ),
            "start_polling": lambda _settings: self._status_after("start_polling", self.start_polling),
            "stop_polling": lambda _settings: self._status_after("stop_polling", self.stop_polling),
            "apply_corrections": lambda settings: self._status_after(
                "apply_corrections",
                lambda: self.apply_corrections(settings),
            ),
            "set_channel_flow": lambda settings: self._status_after(
                "set_channel_flow",
                lambda: self.set_channel_flow(settings["channel_index"], settings["channel_flow_ul_min"],
                                              stop_after=settings.get("channel_stop_after") or "none",
                                              stop_value=settings.get("channel_stop_value") or 0.0),
            ),
            "set_channel_pressure": lambda settings: self._status_after(
                "set_channel_pressure",
                lambda: self.set_channel_pressure(settings["channel_index"], settings["channel_pressure_mbar"],
                                                  stop_after=settings.get("channel_stop_after") or "none",
                                                  stop_value=settings.get("channel_stop_value") or 0.0),
            ),
            "stop_channel": lambda settings: self._status_after(
                "stop_channel",
                lambda: self.stop_channel(settings["channel_index"]),
            ),
            "set_channel_response": lambda settings: self._status_after(
                "set_channel_response",
                lambda: self.set_channel_response(settings["channel_index"], settings["channel_response_s"]),
            ),
            "start_recording": lambda settings: self._status_result(
                "start_recording",
                extra_metadata=self.start_recording(settings),
            ),
            "stop_recording": lambda _settings: self._status_result(
                "stop_recording",
                extra_metadata=self.stop_recording(),
            ),
            "pause_protocol": lambda _settings: self._status_after("pause_protocol", self.pause_pipeline),
            "resume_protocol": lambda _settings: self._status_after("resume_protocol", self.resume_pipeline),
            "stop_protocol": lambda _settings: self._status_after("stop_protocol", self.stop_pipeline),
            "confirm_protocol": lambda _settings: self._status_after(
                "confirm_protocol",
                self.confirm_pipeline_step,
            ),
            "skip_protocol": lambda _settings: self._status_after("skip_protocol", self.skip_pipeline_step),
            "calibrate_channels": lambda _settings: self._status_after("calibrate_channels", self.hardware.calibrate_all),
            "shutdown_instrument": lambda _settings: self._cleanup_shutdown(),
        }

    def _build_action_preparers(self) -> dict[str, ActionSettingsPreparer]:
        return {"start_recording": self._prepare_start_recording_settings}

    def _build_private_action_settings(self) -> dict[str, ActionPrivateSettings]:
        return {"start_recording": self._start_recording_private_settings}

    def _validate_action_handlers(self) -> None:
        declared = {action.id for action in self.actions}
        handled = set(self._action_handlers)
        missing = sorted(declared - handled)
        extra = sorted(handled - declared)
        if missing or extra:
            raise RuntimeError(f"acquisition action handler mismatch: missing={missing}, extra={extra}")

    def run(self, job: RunJob) -> RunResult:
        settings = self._prepare_action_settings(job)
        normalized = validate_action_settings(self.settings, self.actions, job.action, settings)
        normalized.update(self._private_settings_for_job(job))
        handler = self._action_handlers.get(job.action)
        if handler is None:
            raise ValueError(f"unsupported acquisition action: {job.action}")
        metadata = handler(normalized)
        return RunResult(
            job_id=job.id,
            engine=self.id,
            action=job.action,
            metadata=metadata,
        )

    def _prepare_action_settings(self, job: RunJob) -> dict[str, Any]:
        settings = dict(job.settings)
        preparer = self._action_preparers.get(job.action)
        return preparer(job, settings) if preparer else settings

    def _private_settings_for_job(self, job: RunJob) -> dict[str, Any]:
        private_settings = self._private_action_settings.get(job.action)
        return private_settings(job) if private_settings else {}

    def _prepare_start_recording_settings(
        self,
        job: RunJob,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        settings.setdefault("recording_label", job.metadata.get("recording_label", job.id))
        settings.setdefault("camera_width", 640)
        settings.setdefault("camera_height", 480)
        width, height = self._camera.recording_frame_size(settings)
        settings["camera_width"] = width
        settings["camera_height"] = height
        settings.setdefault("camera_video_fps", 24.0)
        settings.setdefault("camera_preview_off_recording", False)
        return settings

    def _start_recording_private_settings(self, job: RunJob) -> dict[str, Any]:
        private_settings: dict[str, Any] = {}
        for output_key, setting_key in (
            ("video", "video_path"),
            ("fluidics_csv", "fluidics_csv_path"),
        ):
            path = job.outputs.get(output_key)
            if path is not None:
                private_settings[setting_key] = str(path)
        return private_settings

    def _status_after(self, action: str, operation: Callable[[], None]) -> dict[str, Any]:
        operation()
        return self._status_result(action)

    def _camera_live_status_action(self, action: str, operation: Callable[[], None]) -> dict[str, Any]:
        operation()
        return self._status_result(action, extra_metadata=self._camera_status_metadata())

    def _disconnect_camera(self) -> dict[str, Any]:
        return self._status_result("disconnect_camera", extra_metadata=self._camera.disconnect())

    @property
    def polling_active(self) -> bool:
        return self._acquisition is not None and self._acquisition.is_alive()

    @property
    def recording_active(self) -> bool:
        return self._recordings.recording_active

    @property
    def csv_logger(self) -> CsvLogger:
        return self._recordings.csv_logger

    @property
    def pipeline_state(self) -> str:
        if not self._pipeline:
            return "idle"
        state = str(getattr(self._pipeline.state, "value", self._pipeline.state))
        if state in {"running", "paused", "stopping"} and not self._pipeline.is_alive():
            # The thread is gone, so whatever it last wrote is not what it is
            # doing -- reporting it as busy would leave every gated action shut.
            return "idle"
        return state

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
            csv_logger=self.csv_logger if self.recording_active else None,
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
        for index, (prefix, label, _calibration, _scale, _offset, _quadratic) in enumerate(FLUIDIC_CHANNELS):
            if index >= len(channels):
                break
            channel = channels[index]
            calibration_name = settings[f"{prefix}_calibration"]
            calibration = SENSOR_CALIBRATIONS.get(calibration_name, 0)
            self.sdk.set_sensor_calibration(channel.sensor_index, calibration)
            self._await_table(channel.sensor_index, calibration, label, calibration_name)
            # The unit reports scale·x + square·x² + cube·x³ (stored as offset and quadratic).
            terms = (settings[f"{prefix}_scale"], settings[f"{prefix}_offset"], settings[f"{prefix}_quadratic"])
            smax = None
            if terms[1] or terms[2]:
                # A curve would push the reported range far past the table's own, and regulation
                # is tuned to that range, so it is capped at the table's.
                self.sdk.set_sensor_custom_scale(channel.sensor_index, 1.0, 0.0, 0.0)
                smax = self.sdk.get_sensor_range(channel.sensor_index)[1]
            self.sdk.set_sensor_custom_scale(channel.sensor_index, *terms, smax=smax)
        try:
            updated = {sensor.index: sensor for sensor in self.sdk.get_sensor_channels_info()}
        except Exception:
            for sensor in self.hardware.state.sensor_channels:
                sensor.smin = sensor.smax = None
            raise
        for sensor in self.hardware.state.sensor_channels:
            detected = updated.get(sensor.index)
            sensor.smin = getattr(detected, "smin", None)
            sensor.smax = getattr(detected, "smax", None)
        # New corrections change what the sensors report, so learned tails restart.
        self.tails.clear()

    def _await_table(self, sensor_index: int, calibration: int, label: str, name: str) -> None:
        """A unit takes a moment to switch tables and reports its old range meanwhile."""
        if not calibration:
            return
        deadline = time.monotonic() + TABLE_SWITCH_S
        while self.sdk.get_sensor_calibration(sensor_index) != calibration:
            if time.monotonic() > deadline:
                kept = {value: key for key, value in SENSOR_CALIBRATIONS.items()}.get(
                    self.sdk.get_sensor_calibration(sensor_index), "an unknown")
                raise RuntimeError(f"{label} did not switch to its {name} table and stays on {kept}; "
                                   "the unit may not have that table")
            time.sleep(0.05)

    def set_channel_flow(self, channel_index: int, flow_ul_min: float, *,
                         stop_after: str = "none", stop_value: float = 0.0) -> None:
        self._require_fluidics_connected()
        self._manual_stop_rule(stop_after, stop_value)
        self._cancel_manual_stop(channel_index)
        self.channel_manager.user_set_flow_regulation(channel_index, flow_ul_min)
        self._arm_manual_stop(channel_index, stop_after, stop_value)

    def set_channel_pressure(self, channel_index: int, pressure_mbar: float, *,
                             stop_after: str = "none", stop_value: float = 0.0) -> None:
        self._require_fluidics_connected()
        self._manual_stop_rule(stop_after, stop_value)
        self._cancel_manual_stop(channel_index)
        self.channel_manager.user_set_pressure(channel_index, pressure_mbar)
        self._arm_manual_stop(channel_index, stop_after, stop_value)

    @staticmethod
    def _manual_stop_rule(kind: str, value: float) -> None:
        if kind not in MANUAL_STOPS:
            raise ValueError(f"stop after must be one of: {', '.join(MANUAL_STOPS)}")
        if kind in {"volume", "time"} and not value > 0:
            raise ValueError(f"stop after {kind} needs a positive value")

    def manual_stop_status(self, channel_index: int) -> str:
        """What a hand-set command on this channel is waiting for, or how it ended."""
        with self._manual_lock:
            stop = self._manual_stops.get(channel_index)
            return stop["status"] if stop else ""

    def _cancel_manual_stop(self, channel_index: int | None = None) -> None:
        with self._manual_lock:
            for index, stop in list(self._manual_stops.items()):
                if channel_index is None or index == channel_index:
                    stop["cancel"].set()
                    del self._manual_stops[index]

    def _arm_manual_stop(self, channel_index: int, kind: str, value: float) -> None:
        """Watch a hand-set command and take its channel to zero once its rule is met."""
        if kind == "none":
            return
        channel = self.channel_manager.channels[channel_index]
        sensor, pressure = channel.sensor_index, channel.pressure_index
        trigger = {
            "volume": lambda: VolumeTrigger(sensor, value),
            "time": lambda: TimeTrigger(value),
            "flow_above": lambda: ConditionTrigger(sensor, min_value=value),
            "flow_below": lambda: ConditionTrigger(sensor, max_value=value),
        }.get(kind, lambda: None)()
        if isinstance(trigger, VolumeTrigger):
            trigger.tail_source = lambda: self.tails.k(sensor)   # stop early by the learned tail
        if trigger is not None:
            trigger.reset()
        unit = {"volume": "µL", "time": "s"}.get(kind, "mbar" if kind.startswith("pressure") else "µL/min")
        rule = {"volume": f"{value:g} µL", "time": f"{value:g} s", "flow_above": f"flow ≥ {value:g} µL/min",
                "flow_below": f"flow ≤ {value:g} µL/min", "pressure_above": f"pressure ≥ {value:g} mbar",
                "pressure_below": f"pressure ≤ {value:g} mbar"}[kind]
        stop = {"cancel": threading.Event(), "status": f"stops at {rule}"}
        with self._manual_lock:
            self._manual_stops[channel_index] = stop
        started = time.monotonic()

        def reading(values: list, index: int) -> float:
            snapshot = self._acquisition.latest_snapshot() if self._acquisition else None
            items = getattr(snapshot, values, None) or []
            return float(items[index]) if index < len(items) else 0.0

        def watch() -> None:
            start_volume = self._acquisition.get_volume(sensor) if self._acquisition else 0.0
            while not stop["cancel"].wait(0.05):
                if kind.startswith("pressure"):
                    current = reading("pressures", pressure)
                    done = current >= value if kind == "pressure_above" else current <= value
                else:
                    done = trigger.check(lambda index: reading("flows", index),
                                         lambda index: self._acquisition.get_volume(index) if self._acquisition else 0.0)
                    current = {"volume": (self._acquisition.get_volume(sensor) if self._acquisition else 0.0)
                               - start_volume, "time": time.monotonic() - started}.get(kind, reading("flows", sensor))
                with self._manual_lock:
                    if self._manual_stops.get(channel_index) is not stop:
                        return
                    stop["status"] = f"stops at {rule} · now {current:.2f} {unit}"
                    if not done:
                        continue
                    del self._manual_stops[channel_index]
                try:
                    flow_controlled = self.channel_manager.channels[channel_index].mode == "flow"
                    self.channel_manager.user_zero(channel_index)
                    stop["status"] = f"stopped at {rule} after {time.monotonic() - started:.1f} s"
                except Exception as exc:
                    stop["status"] = f"could not stop: {exc}"
                    return
                if isinstance(trigger, VolumeTrigger) and flow_controlled and self._acquisition is not None:
                    # A flow command ends at flow 0, as a protocol step does, so its tail is the
                    # same kind and teaches the same k. A pressure stop's tail is not, so it does not.
                    flows = trigger._flows
                    flow_at_stop = statistics.median(flows) if flows else 0.0
                    if flow_at_stop >= 1.0:
                        threading.Thread(
                            target=self._watch_tail,
                            args=(sensor, channel_index, flow_at_stop, self._acquisition.get_volume(sensor),
                                  trigger._start_volume, trigger._target_ul),
                            name="TailWatch", daemon=True,
                        ).start()
                with self._manual_lock:
                    self._manual_stops.setdefault(channel_index, stop)   # keep the result visible
                return

        threading.Thread(target=watch, name=f"ManualStop{channel_index}", daemon=True).start()

    def stop_channel(self, channel_index: int) -> None:
        self._require_fluidics_connected()
        self._cancel_manual_stop(channel_index)
        self.channel_manager.user_zero(channel_index)

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
    ) -> dict[str, Any]:
        if not self.hardware.connected:
            raise RuntimeError("Fluidics hardware is not connected")
        # Asked for, not inferred. A camera being live is not a request to
        # record it, and a video appearing because of what happened to be
        # switched on is a file nobody chose.
        wants_video = bool(settings.get("include_video", False))
        camera_recorder = self._camera.acquisition if (wants_video and self.camera_live) else None
        if wants_video and not self.camera_live:
            raise RuntimeError("include_video was asked for, but no camera is live")
        return self._recordings.start_recording(
            settings,
            camera_recorder=camera_recorder,
            frame_size=self._camera.recording_frame_size(settings),
        )

    def stop_recording(self) -> dict[str, Any]:
        return self._recordings.stop_recording()

    def start_pipeline(self, steps: list[ProtocolStep], *, tick_s: float = 0.2, on_event=None) -> None:
        """Run the steps handed to it.

        The engine is not told which experiment this is and does not ask. What
        the steps mean belongs to the workflow that built them.
        """
        if self._pipeline and self._pipeline.is_alive():
            return
        self._cancel_manual_stop()   # a protocol takes over from anything set by hand
        if not self.hardware.state.connected:
            raise RuntimeError("Fluidics hardware is not connected")
        steps = self.build_pipeline_from_steps(steps)
        sensor_to_channel = {
            channel.sensor_index: channel_index
            for channel_index, channel in enumerate(self.channel_manager.channels)
        }
        for step in steps:
            volume = _volume_trigger(step)
            if volume is not None:
                volume.tail_source = lambda sensor=volume.sensor_index: self.tails.k(sensor)

        def publish(event):
            try:
                if on_event is not None:
                    on_event(event)
            finally:
                self.record_event(event)
                self._learn_tail(event, steps, sensor_to_channel)

        self._pipeline = self._pipeline_engine_factory(
            steps,
            self.channel_manager,
            self._acquisition,
            self.pipeline_queue,
            sensor_to_channel,
            tick_s=tick_s,
            next_sequence=self.next_event_sequence,
            on_event=publish,
        )
        self._pipeline.start()

    def _learn_tail(self, event: Any, steps: list, sensor_to_channel: dict[int, int]) -> None:
        """After a volume step stops at zero, measure the flow that still arrives."""
        if str(event.outcome) != "completed" or not event.step_name:
            return
        if not 0 <= event.current_step < len(steps):
            return
        step = steps[event.current_step]
        volume = _volume_trigger(step)
        if volume is None or step.on_complete != "zero" or self._acquisition is None:
            return
        flows = volume._flows
        flow_at_stop = statistics.median(flows) if flows else 0.0
        channel_index = sensor_to_channel.get(volume.sensor_index)
        if flow_at_stop < 1.0 or channel_index is None:
            return
        threading.Thread(
            target=self._watch_tail,
            args=(volume.sensor_index, channel_index, flow_at_stop,
                  self._acquisition.get_volume(volume.sensor_index),
                  volume._start_volume, volume._target_ul),
            name="TailWatch", daemon=True,
        ).start()

    def _watch_tail(self, sensor: int, channel_index: int, flow_at_stop: float,
                    volume_at_stop: float, volume_at_start: float | None = None, target_ul: float = 0.0,
                    *, hold_s: float = 0.5, limit_s: float = 15.0) -> None:
        band = max(0.5, 0.05 * flow_at_stop)
        started = time.monotonic()
        quiet_since = quiet_volume = None
        while time.monotonic() - started < limit_s:
            time.sleep(0.05)
            acquisition = self._acquisition
            channels = self.channel_manager.channels
            if acquisition is None or channel_index >= len(channels):
                return
            channel = channels[channel_index]
            if channel.active_setpoint or channel.pressure_setpoint:
                return  # commanded again: the tail is now mixed with new flow
            recent = [s.flows[sensor] for s in acquisition.recent_snapshots(3) if sensor < len(s.flows)]
            if len(recent) < 3:
                continue
            if abs(statistics.median(recent)) <= band:
                if quiet_since is None:
                    quiet_since, quiet_volume = time.monotonic(), acquisition.get_volume(sensor)
                if time.monotonic() - quiet_since >= hold_s:
                    k = (quiet_volume - volume_at_stop) * 60.0 / flow_at_stop
                    if 0.0 < k <= 10.0:
                        self.tails.record(sensor, k)
                    if volume_at_start is not None and target_ul > 0:
                        delivered = quiet_volume - volume_at_start
                        self.tails.record_error(sensor, (delivered - target_ul) / target_ul * 100.0)
                    return
            else:
                quiet_since = quiet_volume = None

    def stop_pipeline(self) -> None:
        if self._pipeline and self._pipeline.is_alive():
            self._pipeline.stop()
            self._pipeline.join(timeout=3.0)
        # A protocol still finishing (closing a long video) stays known, so a
        # second one cannot start on top of it.
        if self._pipeline is not None and not self._pipeline.is_alive():
            self._pipeline = None

    def pause_pipeline(self) -> None:
        if self._pipeline:
            self._pipeline.pause()
        # A paused protocol is not producing sample, so recording it only burns disk
        # -- and a pause waiting on an operator confirmation is open ended.
        self._set_recording_paused(True)

    def resume_pipeline(self) -> None:
        if self._pipeline:
            self._pipeline.resume()
        self._set_recording_paused(False)

    def _set_recording_paused(self, paused: bool) -> None:
        acquisition = self._camera.acquisition
        if acquisition is None or not acquisition.recording:
            return
        if paused:
            acquisition.pause_recording()
        else:
            acquisition.resume_recording()

    def skip_pipeline_step(self) -> None:
        if self._pipeline:
            self._pipeline.skip_step()

    def confirm_pipeline_step(self) -> None:
        if self._pipeline:
            self._pipeline.confirm_pending()

    def build_pipeline_from_steps(self, steps: list[Any]) -> list[Any]:
        return self._build_pipeline_steps(steps)

    def _connect(self, settings: dict[str, Any]) -> dict[str, Any]:
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
        self._configure_channels_from_state(state)
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

    def _disconnect(self, action: str) -> dict[str, Any]:
        self.stop_recording()
        self.stop_polling()
        self.stop_pipeline()
        self.hardware.disconnect()
        self.channel_manager.configure_channels([])
        return self._status_result(action)

    def _cleanup_shutdown(self) -> dict[str, Any]:
        errors: list[str] = []
        for operation in (
            self.stop_recording,
            self.stop_polling,
            self.stop_pipeline,
            self.stop_camera_live,
            self._camera.disconnect,
            self.hardware.disconnect,
        ):
            try:
                operation()
            except Exception as exc:
                errors.append(f"{getattr(operation, '__name__', 'operation')}: {exc}")
        self.channel_manager.configure_channels([])
        return self._status_result(
            "shutdown_instrument",
            extra_metadata={
                "cleanup_ok": not errors,
                "cleanup_errors": errors,
                "camera_connected": self._camera.camera.connected,
                "camera_live": self.camera_live,
            },
        )

    def _configure_channels_from_state(self, state: Any) -> None:
        self.channel_manager.configure_channels(_pair_channels(state))

    def _connect_camera(self, settings: dict[str, Any]) -> dict[str, Any]:
        return self._status_result("connect_camera", extra_metadata=self._camera.connect(settings))

    def _apply_camera_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        return self._status_result("set_camera_settings", extra_metadata=self._camera.apply_settings(settings))

    @property
    def camera_live(self) -> bool:
        return self._camera.live

    def start_camera_live(self) -> None:
        self._camera.start_live()

    def stop_camera_live(self) -> None:
        self._camera.stop_live()

    def _verify_fluigent(self, settings: dict[str, Any]) -> dict[str, Any]:
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
            self._configure_channels_from_state(state)
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
        return self._camera.preflight()

    def _camera_status_metadata(self) -> dict[str, Any]:
        return self._camera.status_metadata()

    def subscribe_camera_frames(
        self,
        callback: Callable[[Any], None],
    ) -> Callable[[], None]:
        return self._camera.subscribe_frames(callback)

    def acknowledge_camera_frame(self) -> None:
        self._camera.acknowledge_frame()

    def recording_metadata_sources(self) -> list[dict[str, Any]]:
        return self._recordings.metadata_sources()

    def _sdk_preflight(self) -> SDKAvailability:
        if hasattr(self.sdk, "preflight"):
            return self.sdk.preflight()
        return SDKAvailability(
            available=True,
            source="injected",
            message="Fluigent SDK object is injected.",
        )

    def emergency_stop(self, reason: str = "") -> dict[str, Any]:
        """Take every channel to zero, stop the protocol, close the recording.

        A manual stop that leaves nothing set afterwards. Each step is attempted
        independently, so one failing does not skip the rest.
        """
        stopped: dict[str, Any] = {"channels_zeroed": False, "protocol": None, "recording": None}
        errors: list[str] = []

        # Channels first, and before anything that can block. Everything else
        # here is bookkeeping; this is the part that makes the rig safe.
        try:
            self._cancel_manual_stop()
            self.channel_manager.emergency_stop_all()
            stopped["channels_zeroed"] = True
        except Exception as exc:
            errors.append(f"zeroing channels: {exc}")

        try:
            was_running = self.pipeline_state in {"running", "paused", "stopping"}
            self.stop_pipeline()
            stopped["protocol"] = "stopped" if was_running else "not running"
        except Exception as exc:
            errors.append(f"stopping the protocol: {exc}")

        try:
            if self.recording_active:
                closed = self.stop_recording()
                stopped["recording"] = "closed"
                stopped["closed_recording"] = closed.get("recording")
            else:
                stopped["recording"] = "not recording"
        except Exception as exc:
            errors.append(f"closing the recording: {exc}")

        return {**stopped, "errors": errors, "reason": reason}

    def observation(self) -> dict[str, Any]:
        """What the instrument is doing, measured rather than assumed.

        Nothing here is invented. A channel that has never been read reports
        null for its measurements, because a fabricated zero is a reading
        somebody will believe.
        """
        state = self.hardware.state
        snapshot = self._acquisition.latest_snapshot() if self._acquisition else None
        recent = self._acquisition.recent_snapshots() if self._acquisition else []
        recording = self._recording_observation()
        camera = self._camera.observation()
        camera["recording"] = recording["active"] and bool(recording["video"])
        camera["recording_path"] = recording["video"]
        return {
            "connection": {
                "fluidics": bool(state.connected),
                "simulated": bool(state.simulated),
            },
            "polling": self.polling_active,
            "recording": recording,
            "channels": self._channel_observations(snapshot, recent),
            "camera": camera,
            "protocol": self._protocol_observation(),
        }

    def _recording_observation(self) -> dict[str, Any]:
        metadata = self._recordings.status_metadata()
        current = metadata.get("current_recording") or {}
        return {
            "active": self.recording_active,
            "id": current.get("recording_id"),
            "fluidics_csv": current.get("fluidics_csv"),
            "video": current.get("video_path"),
        }

    def _channel_observations(self, snapshot: Any, recent: list[Any]) -> list[dict[str, Any]]:
        state = self.hardware.state
        channels = self.channel_manager.channels
        observations = []
        for index, channel in enumerate(channels):
            label = FLUIDIC_CHANNEL_LABELS[index] if index < len(FLUIDIC_CHANNEL_LABELS) else ""
            sensor = channel.sensor_index
            observations.append(
                {
                    "index": index,
                    "label": label,
                    "detected": _detected_channel(state, channel),
                    **self.tails.describe(channel.sensor_index),
                    "mode": channel.mode,
                    "requested_flow_ul_min": channel.active_setpoint
                    if channel.mode == "flow"
                    else None,
                    "requested_pressure_mbar": channel.pressure_setpoint
                    if channel.mode != "flow"
                    else None,
                    **_measured(
                        snapshot,
                        recent,
                        sensor,
                        channel.pressure_index,
                        requested_flow=(channel.active_setpoint if channel.mode == "flow" else None),
                        pressure_limit=_pressure_limit(state, channel.pressure_index),
                        polling_started=(
                            self._acquisition.started_monotonic if self._acquisition else 0.0
                        ),
                    ),
                }
            )
        return observations

    def _protocol_observation(self) -> dict[str, Any]:
        event = self._pipeline.latest_event() if self._pipeline else None
        return {
            "state": self.pipeline_state,
            "event_sequence": event.sequence if event else 0,
            "step_index": event.current_step if event else None,
            "total_steps": event.total_steps if event else None,
            "step_name": event.step_name if event else "",
            "progress": event.progress if event else None,
            "outcome": str(event.outcome) if event else "",
            "confirmation_message": event.confirmation_message if event else "",
            "error": event.error_msg if event else "",
        }

    def next_event_sequence(self) -> int:
        with self._event_lock:
            self._event_sequence += 1
            return self._event_sequence

    def record_event(self, event: Any) -> None:
        with self._event_condition:
            self._event_history.append(event)
            self._event_condition.notify_all()

    def latest_event(self) -> Any:
        with self._event_lock:
            return self._event_history[-1] if self._event_history else None

    def events_after(self, sequence: int = 0, limit: int = 100) -> list[Any]:
        """Events newer than a cursor, across every protocol this session ran."""
        with self._event_lock:
            events = list(self._event_history)
        newer = [event for event in events if event.sequence > sequence]
        return newer[: max(0, limit)] if limit else newer

    def wait_for_event(
        self,
        sequence: int,
        predicate: Any,
        timeout_s: float,
    ) -> tuple[Any | None, int]:
        """Wait for a matching event while advancing over non-milestone events."""
        deadline = time.monotonic() + timeout_s
        cursor = sequence
        with self._event_condition:
            while True:
                newer = [event for event in self._event_history if event.sequence > cursor]
                for event in newer:
                    cursor = event.sequence
                    if predicate(event):
                        return event, cursor
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, cursor
                self._event_condition.wait(remaining)

    def rejected_readings(self, sensor_index: int) -> list[tuple[float, float]]:
        """Readings left out of the volume, as (elapsed_s, flow)."""
        return self._acquisition.rejected_readings(sensor_index) if self._acquisition else []

    def channel_tails(self) -> list[dict[str, Any]]:
        """Each channel's learned tail k, in channel order."""
        return [self.tails.describe(channel.sensor_index) for channel in self.channel_manager.channels]

    def polling_started_monotonic(self) -> float:
        return self._acquisition.started_monotonic if self._acquisition else 0.0

    def latest_snapshot(self) -> Any:
        return self._acquisition.latest_snapshot() if self._acquisition else None

    def recent_snapshots(self, limit: int = 0) -> list[Any]:
        return self._acquisition.recent_snapshots(limit) if self._acquisition else []

    def _status_result(
        self,
        action: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.hardware.state
        metadata = {
            "action": action,
            "connected": state.connected,
            "simulated": state.simulated,
            "pressure_channels": len(state.pressure_channels),
            "sensor_channels": len(state.sensor_channels),
            "polling_active": self.polling_active,
            "recording_active": self.recording_active,
            "pipeline_state": self.pipeline_state,
            "queued_snapshots": self.data_queue.qsize(),
            "queued_pipeline_events": self.pipeline_queue.qsize(),
        }
        metadata.update(self._recordings.status_metadata())
        if extra_metadata:
            metadata.update(extra_metadata)
        return metadata


def create_engine(
    settings: ParamSchema | None = None,
    *,
    pipeline_step_builder: PipelineStepBuilder = build_pipeline_steps,
    pipeline_engine_factory: PipelineEngineFactory = PipelineEngine,
    **kwargs: Any,
) -> AcquisitionEngine:
    return AcquisitionEngine(
        settings or CONTROL_ENGINE_SETTINGS,
        pipeline_step_builder=pipeline_step_builder,
        pipeline_engine_factory=pipeline_engine_factory,
        **kwargs,
    )


def _volume_trigger(step: Any) -> VolumeTrigger | None:
    trigger = getattr(step.trigger, "inner", step.trigger)
    return trigger if isinstance(trigger, VolumeTrigger) else None


def _detected_channel(state: Any, channel: Any) -> dict[str, Any]:
    """What the instrument says this channel physically is.

    The operator confirms the mapping against this, so it is reported as the
    instrument reports it rather than assumed from the configured order.
    """
    pressure = next(
        (info for info in state.pressure_channels if info.index == channel.pressure_index), None
    )
    sensor = next(
        (info for info in state.sensor_channels if info.index == channel.sensor_index), None
    )
    return {
        "pressure_index": channel.pressure_index,
        "sensor_index": channel.sensor_index,
        "controller_sn": getattr(pressure, "controller_sn", None),
        "pressure_device_sn": getattr(pressure, "device_sn", None),
        "pressure_max_mbar": getattr(pressure, "pmax", None),
        "sensor_device_sn": getattr(sensor, "device_sn", None),
        "sensor_type": getattr(sensor, "sensor_type", None),
        "sensor_max_ul_min": getattr(sensor, "smax", None),
    }


def _pressure_limit(state: Any, pressure_index: int) -> float | None:
    info = next((item for item in state.pressure_channels if item.index == pressure_index), None)
    return getattr(info, "pmax", None)


def _stability_rule() -> str:
    return (
        f"stable when the last {STABILITY_WINDOW_SAMPLES} flow samples span no more than "
        f"{2 * STABILITY_TOLERANCE_UL_MIN:g} uL/min"
    )


def _measured(
    snapshot: Any,
    recent: list[Any],
    sensor_index: int,
    pressure_index: int,
    *,
    requested_flow: float | None,
    pressure_limit: float | None,
    polling_started: float,
) -> dict[str, Any]:
    """The readings for one channel, or nulls where there has been no reading."""
    empty = {
        "pressure_mbar": None,
        "flow_ul_min": None,
        "volume_ul": None,
        "stable": None,
        "pressure_mean_mbar": None,
        "pressure_std_mbar": None,
        "flow_mean_ul_min": None,
        "flow_std_ul_min": None,
        "flow_min_ul_min": None,
        "flow_max_ul_min": None,
        "pressure_min_mbar": None,
        "pressure_max_mbar": None,
        "sample_count": 0,
        "window_duration_s": None,
        "flow_error_ul_min": None,
        "flow_error_percent": None,
        "pressure_limit_mbar": pressure_limit,
        "pressure_margin_mbar": None,
        "pressure_percent_of_limit": None,
        "measurement_age_s": None,
        "stability_rule": _stability_rule(),
    }
    if snapshot is None:
        return empty

    def at(values: Any, index: int) -> Any:
        return values[index] if 0 <= index < len(values) else None

    pressure_stats = _window_stats(recent, "pressures", pressure_index)
    flow_stats = _window_stats(recent, "flows", sensor_index)
    flow = at(snapshot.flows, sensor_index)
    pressure = at(snapshot.pressures, pressure_index)
    flow_error = (
        abs(flow - requested_flow) if flow is not None and requested_flow is not None else None
    )
    return {
        "pressure_mbar": pressure,
        "flow_ul_min": flow,
        "volume_ul": at(snapshot.volumes_ul, sensor_index),
        "stable": at(snapshot.stability, sensor_index),
        "pressure_mean_mbar": pressure_stats["mean"],
        "pressure_std_mbar": pressure_stats["std"],
        "flow_mean_ul_min": flow_stats["mean"],
        "flow_std_ul_min": flow_stats["std"],
        "flow_min_ul_min": flow_stats["min"],
        "flow_max_ul_min": flow_stats["max"],
        "pressure_min_mbar": pressure_stats["min"],
        "pressure_max_mbar": pressure_stats["max"],
        "sample_count": len(recent),
        "window_duration_s": (
            recent[-1].elapsed_s - recent[0].elapsed_s if len(recent) > 1 else 0.0
        ),
        "flow_error_ul_min": flow_error,
        "flow_error_percent": (
            flow_error / abs(requested_flow) * 100.0
            if flow_error is not None and requested_flow not in (None, 0)
            else None
        ),
        "pressure_limit_mbar": pressure_limit,
        "pressure_margin_mbar": (
            pressure_limit - pressure
            if pressure is not None and pressure_limit is not None
            else None
        ),
        "pressure_percent_of_limit": (
            pressure / pressure_limit * 100.0
            if pressure is not None and pressure_limit not in (None, 0)
            else None
        ),
        "measurement_age_s": (
            max(0.0, time.monotonic() - polling_started - snapshot.elapsed_s)
            if polling_started
            else None
        ),
        "stability_rule": _stability_rule(),
    }


def _window_stats(snapshots: list[Any], field: str, index: int) -> dict[str, float | None]:
    values = [
        values[index]
        for snapshot in snapshots
        if (values := getattr(snapshot, field, ())) is not None and 0 <= index < len(values)
    ]
    if not values:
        return {"mean": None, "std": None, "min": None, "max": None}
    return {
        "mean": mean(values),
        "std": stdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
    }


def _pair_channels(state: Any) -> list[tuple[int, int]]:
    return [
        (sensor.index, pressure.index)
        for sensor, pressure in zip(state.sensor_channels, state.pressure_channels, strict=False)
    ]
