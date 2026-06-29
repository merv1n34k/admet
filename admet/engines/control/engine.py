from __future__ import annotations

import base64
import time
from dataclasses import asdict
from pathlib import Path
from queue import Queue
from threading import Lock
from typing import Any

import numpy as np

from admet.core.engine import ActionSpec, EngineContext, EngineResult, validate_action_settings
from admet.core.schema import ResultSet, SummaryStat
from admet.engines.control.fluidics import (
    AcquisitionThread,
    ChannelManager,
    CsvLogger,
    FluigentSDK,
    HardwareManager,
    SDKAvailability,
)
from admet.engines.control.camera import Camera, CameraAcquisitionThread
from admet.engines.control.fluidics.config import PIPELINES, ProtocolStep
from admet.engines.control.pipeline import (
    PipelineEngine,
    PipelineEvent,
    PipelineState,
    PipelineStep,
    create_trigger,
)
from admet.engines.control.settings import CONTROL_ENGINE_SETTINGS


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
    ActionSpec("verify_backend", "Verify Backend", "diagnostics", params=("pylon_camemu",)),
    ActionSpec("verify_fluigent", "Verify Fluigent", "diagnostics", params=("simulated",)),
    ActionSpec("refresh_cameras", "Refresh Cameras", "diagnostics", params=("pylon_camemu",)),
    ActionSpec("connect_camera", "Connect Camera", "connection", params=("pylon_camemu", "camera_index")),
    ActionSpec("disconnect_camera", "Disconnect Camera", "connection", params=("pylon_camemu",)),
    ActionSpec("apply_camera_settings", "Apply Camera Settings", "diagnostics", params=CAMERA_CONFIGURATION_PARAMS),
    ActionSpec("start_camera_live", "Start Camera Live", "diagnostics"),
    ActionSpec("stop_camera_live", "Stop Camera Live", "diagnostics"),
    ActionSpec("camera_status", "Camera Status", "diagnostics"),
    ActionSpec("start_polling", "Start Polling", "diagnostics"),
    ActionSpec("stop_polling", "Stop Polling", "diagnostics"),
    ActionSpec("start_recording", "Start Recording", "recording", params=("log_dir",)),
    ActionSpec("stop_recording", "Stop Recording", "recording"),
    ActionSpec("run_protocol", "Run Protocol", "protocol", params=("pipeline_name", "tick_s")),
    ActionSpec("pause_protocol", "Pause Protocol", "protocol"),
    ActionSpec("resume_protocol", "Resume Protocol", "protocol"),
    ActionSpec("stop_protocol", "Stop Protocol", "protocol"),
    ActionSpec("confirm_protocol", "Confirm Protocol Step", "protocol"),
    ActionSpec("skip_protocol", "Skip Protocol Step", "protocol"),
    ActionSpec("calibrate", "Calibrate", "calibration"),
    ActionSpec("wash", "Wash", "protocol", params=("tick_s",)),
)


class FluidicsControlEngine:
    id = "fluidics"
    name = "Fluigent Fluidics Control"
    settings = CONTROL_ENGINE_SETTINGS
    actions = CONTROL_ACTIONS

    def __init__(self, sdk: FluigentSDK | None = None):
        self.sdk = sdk or FluigentSDK()
        self.hardware = HardwareManager(self.sdk)
        self.channel_manager = ChannelManager(self.sdk)
        self.data_queue: Queue = Queue(maxsize=50)
        self.pipeline_queue: Queue[PipelineEvent] = Queue(maxsize=50)
        self.csv_logger = CsvLogger()
        self.camera = Camera()
        self._camera_acquisition: CameraAcquisitionThread | None = None
        self._camera_preview_src = ""
        self._camera_preview_size = (0, 0)
        self._camera_preview_at = 0.0
        self._camera_last_frame: np.ndarray | None = None
        self._camera_stats: dict[str, Any] = {}
        self._camera_preflight_cache: dict[str, Any] = {}
        self._camera_lock = Lock()
        self._acquisition: AcquisitionThread | None = None
        self._pipeline: PipelineEngine | None = None
        self._recording = False
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
            return self._status_result(action, extra_metadata=self._backend_preflight(normalized))
        if action == "verify_fluigent":
            return self._verify_fluigent(normalized)
        if action == "refresh_cameras":
            return self._status_result(action, extra_metadata=self._camera_preflight(normalized))
        if action == "connect_camera":
            return self._connect_camera(normalized)
        if action == "disconnect_camera":
            self.stop_camera_live()
            self.camera.close()
            return self._status_result(action, extra_metadata=self._camera_preflight(normalized))
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
        if action == "start_recording":
            path = self.start_recording(normalized, context)
            return self._status_result(action, artifacts={"csv_path": path})
        if action == "stop_recording":
            self.stop_recording()
            return self._status_result(action)
        if action == "run_protocol":
            self.start_pipeline(normalized["pipeline_name"], tick_s=normalized["tick_s"])
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
            self.start_pipeline("Priming", tick_s=normalized["tick_s"])
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

    def start_recording(
        self,
        settings: dict[str, Any],
        context: EngineContext | None = None,
    ) -> str:
        if self._recording:
            return self.csv_logger.filepath or ""
        if not self.hardware.connected:
            raise RuntimeError("Fluidics hardware is not connected")

        log_dir = Path(settings["log_dir"])
        if context and context.workdir and not log_dir.is_absolute():
            log_dir = Path(context.workdir) / log_dir
        self.csv_logger = CsvLogger(log_dir)
        state = self.hardware.state
        filepath = self.csv_logger.start(
            len(state.pressure_channels),
            len(state.sensor_channels),
        )
        self._recording = True
        if self._acquisition:
            self._acquisition.set_csv_logger(self.csv_logger)
        return filepath

    def stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        if self._acquisition:
            self._acquisition.set_csv_logger(None)
        self.csv_logger.stop()

    def start_pipeline(self, name: str, *, tick_s: float = 0.2) -> None:
        if self._pipeline and self._pipeline.is_alive():
            return
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
                on_complete=step.on_complete,
                confirm_message=step.confirm_message,
            )
            for step in _expand_steps(steps)
        ]

    def _connect(self, settings: dict[str, Any]) -> EngineResult:
        state = self.hardware.connect(simulated=settings["simulated"])
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
        return self._status_result("connect_fluidics")

    def _disconnect(self, action: str) -> EngineResult:
        self.stop_recording()
        self.stop_polling()
        self.stop_pipeline()
        self.hardware.disconnect()
        self.channel_manager.configure_channels([])
        return self._status_result(action)

    def _connect_camera(self, settings: dict[str, Any]) -> EngineResult:
        metadata = self._camera_preflight(settings)
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
        metadata = self._camera_preflight(settings)
        metadata.update(
            {
                "camera_connect_ok": ok,
                "camera_connect_message": "Camera connected." if ok else "Camera open failed.",
                "camera_connected": self.camera.connected,
            }
        )
        if ok:
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

    def _backend_preflight(self, settings: dict[str, Any]) -> dict[str, Any]:
        sdk_status = self._sdk_preflight()
        return {
            "fluigent_sdk": asdict(sdk_status),
            "fluigent_sdk_available": sdk_status.available,
            "fluigent_sdk_message": sdk_status.message,
            **self._camera_preflight(settings),
        }

    def _camera_preflight(self, settings: dict[str, Any]) -> dict[str, Any]:
        camera_status = self.camera.preflight(settings.get("pylon_camemu") or None)
        camera_status_metadata = asdict(camera_status)
        camera_status_metadata.pop("pylon_camemu", None)
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
            preview_src = self._camera_preview_src
            preview_size = self._camera_preview_size
            stats = dict(self._camera_stats)
            frame_shape = list(self._camera_last_frame.shape) if self._camera_last_frame is not None else []
        metadata = {
            **self._camera_preflight_cache,
            "camera_connected": self.camera.connected,
            "camera_live": self.camera_live,
            "camera_preview_src": preview_src,
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
        now = time.time()
        with self._camera_lock:
            self._camera_last_frame = frame
            if now - self._camera_preview_at >= 0.5:
                self._camera_preview_src = _frame_to_data_uri(frame)
                self._camera_preview_size = (frame.shape[1], frame.shape[0])
                self._camera_preview_at = now
        if self._camera_acquisition:
            self._camera_acquisition.frame_processed()

    def _on_camera_stats(self, stats: dict[str, Any]) -> None:
        with self._camera_lock:
            self._camera_stats = dict(stats)

    def latest_camera_frame(self) -> np.ndarray | None:
        with self._camera_lock:
            return self._camera_last_frame

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


def _frame_to_data_uri(frame: np.ndarray) -> str:
    try:
        import cv2
    except ImportError:
        return ""

    display = frame
    if display.dtype == np.uint16:
        display = (display >> 8).astype(np.uint8)
    if not display.flags["C_CONTIGUOUS"]:
        display = np.ascontiguousarray(display)
    max_width = 640
    if display.ndim >= 2 and display.shape[1] > max_width:
        scale = max_width / display.shape[1]
        display = cv2.resize(
            display,
            (max_width, max(1, int(display.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        )
    ok, buffer = cv2.imencode(".png", display)
    if not ok:
        return ""
    encoded = base64.b64encode(buffer).decode("ascii")
    return f"data:image/png;base64,{encoded}"


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
