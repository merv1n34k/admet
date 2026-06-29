from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from queue import Queue
from typing import Any

from admet.core.engine import EngineContext, EngineResult
from admet.core.schema import (
    Param,
    ParamKind,
    ParamOption,
    ParamSchema,
    ResultRecord,
    ResultSet,
    SummaryStat,
)
from admet.engines.control.fluidics import (
    AcquisitionThread,
    ChannelManager,
    CsvLogger,
    FluigentSDK,
    HardwareManager,
    SDKAvailability,
)
from admet.engines.control.camera import Camera
from admet.engines.control.fluidics.config import PIPELINES, ProtocolStep
from admet.engines.control.pipeline import (
    PipelineEngine,
    PipelineEvent,
    PipelineState,
    PipelineStep,
    create_trigger,
)


def _pipeline_options() -> tuple[ParamOption, ...]:
    return tuple(ParamOption(name, name) for name in sorted(PIPELINES))


class FluidicsControlEngine:
    id = "fluidics"
    name = "Fluigent Fluidics Control"
    settings = ParamSchema(
        (
            Param("simulated", "Simulated Hardware", ParamKind.BOOLEAN, default=False),
            Param("start_polling", "Start Polling", ParamKind.BOOLEAN, default=True),
            Param(
                "pipeline_name",
                "Pipeline",
                ParamKind.CHOICE,
                default="Priming",
                options=_pipeline_options(),
            ),
            Param("log_dir", "Log Directory", ParamKind.PATH, default="logs"),
            Param("tick_s", "Pipeline Tick", ParamKind.FLOAT, default=0.2, minimum=0.001),
            Param("camera_index", "Camera Index", ParamKind.INTEGER, default=0, minimum=0),
        )
    )

    def __init__(self, sdk: FluigentSDK | None = None):
        self.sdk = sdk or FluigentSDK()
        self.hardware = HardwareManager(self.sdk)
        self.channel_manager = ChannelManager(self.sdk)
        self.data_queue: Queue = Queue(maxsize=50)
        self.pipeline_queue: Queue[PipelineEvent] = Queue(maxsize=50)
        self.csv_logger = CsvLogger()
        self.camera = Camera()
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
        normalized = self.settings.validate(settings)
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
            self.camera.close()
            return self._status_result(action, extra_metadata=self._camera_preflight())
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
        return self._status_result("connect_camera", extra_metadata=metadata)

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
            **self._camera_preflight(),
        }

    def _camera_preflight(self) -> dict[str, Any]:
        camera_status = self.camera.preflight()
        return {
            "camera": asdict(camera_status),
            "pypylon_available": camera_status.pypylon_available,
            "camera_refresh_ok": camera_status.refresh_ok,
            "camera_count": camera_status.camera_count,
            "cameras": list(camera_status.cameras),
            "camera_connected": self.camera.connected,
            "pylon_camemu": camera_status.pylon_camemu,
            "camera_message": camera_status.message,
        }

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
        record = ResultRecord(
            sample_id="control",
            engine=self.id,
            values={"action": action, **metadata},
        )
        stats = (
            SummaryStat("pressure_channels", metadata["pressure_channels"]),
            SummaryStat("sensor_channels", metadata["sensor_channels"]),
        )
        return EngineResult(
            result_set=ResultSet(records=(record,), stats=stats, metadata=metadata),
            artifacts=artifacts or {},
        )


def create_engine() -> FluidicsControlEngine:
    return FluidicsControlEngine()

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
