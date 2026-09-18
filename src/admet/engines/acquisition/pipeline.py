"""Running a protocol against the hardware.

The pipeline takes declared steps, turns each one's trigger into an object that
can be asked whether it is finished, applies the setpoints, and waits. It emits
an event on every tick so a caller can follow progress without polling the
instrument.

It knows nothing about which experiment it is running. Steps arrive as data.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from queue import Queue
from typing import Protocol

from admet.engines.acquisition.protocols import ProtocolStep, expand_protocol_steps
from admet.engines.acquisition.triggers import Trigger, create_trigger

log = logging.getLogger(__name__)

class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    ERROR = "error"

@dataclass
class PipelineStep:
    name: str
    sensor_setpoints: dict[int, float]
    trigger: Trigger
    pressure_setpoints: dict[int, float] = field(default_factory=dict)
    on_complete: str = "hold"
    confirm_message: str = ""
    status: StepStatus = StepStatus.PENDING
    error_msg: str = ""

class ChannelController(Protocol):
    def pipeline_zero_all(self) -> None:
        ...

    def pipeline_resume_all(self) -> None:
        ...

    def pipeline_release_all(self) -> None:
        ...

    def pipeline_set_setpoint(self, channel_index: int, value: float) -> None:
        ...

    def pipeline_set_pressure(self, channel_index: int, pressure_mbar: float) -> None:
        ...

    def pipeline_release_channel(self, channel_index: int) -> None:
        ...

class AcquisitionSource(Protocol):
    def get_volume(self, sensor_index: int) -> float:
        ...

class PipelineState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    COMPLETED = "completed"
    ERROR = "error"

@dataclass(frozen=True)
class PipelineEvent:
    state: PipelineState
    current_step: int
    total_steps: int
    step_name: str = ""
    progress: float = 0.0
    error_msg: str = ""
    step_volumes: dict[int, float] = field(default_factory=dict)
    confirmation_message: str = ""

class PipelineEngine(threading.Thread):
    def __init__(
        self,
        steps: list[PipelineStep],
        channel_manager: ChannelController,
        acquisition: AcquisitionSource | None,
        event_queue: Queue[PipelineEvent] | None,
        sensor_to_channel: dict[int, int],
        *,
        tick_s: float = 0.1,
    ):
        super().__init__(daemon=True, name="PipelineEngine")
        self._steps = steps
        self._channel_manager = channel_manager
        self._acquisition = acquisition
        self._event_queue = event_queue or Queue()
        self._sensor_to_channel = sensor_to_channel
        self._tick_s = tick_s

        self._state = PipelineState.IDLE
        self._current_step_idx = 0
        self._step_start_volumes: dict[int, float] = {}

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._skip_event = threading.Event()
        self._confirm_event = threading.Event()

    @property
    def state(self) -> PipelineState:
        return self._state

    @property
    def steps(self) -> list[PipelineStep]:
        return self._steps

    @property
    def event_queue(self) -> Queue[PipelineEvent]:
        return self._event_queue

    def pause(self) -> None:
        if self._state == PipelineState.RUNNING:
            self._pause_event.clear()
            self._state = PipelineState.PAUSED
            self._channel_manager.pipeline_zero_all()
            self._emit_event()
            log.info("Pipeline paused")

    def resume(self) -> None:
        if self._state == PipelineState.PAUSED:
            self._channel_manager.pipeline_resume_all()
            self._pause_event.set()
            self._state = PipelineState.RUNNING
            self._emit_event()
            log.info("Pipeline resumed")

    def confirm_pending(self) -> None:
        self._confirm_event.set()

    def stop(self) -> None:
        log.info("Pipeline stop requested")
        self._state = PipelineState.STOPPING
        self._stop_event.set()
        self._pause_event.set()
        self._confirm_event.set()

    def skip_step(self) -> None:
        self._skip_event.set()
        self._confirm_event.set()
        log.info("Skip requested for step %d", self._current_step_idx)

    def run(self) -> None:
        self._state = PipelineState.RUNNING
        self._emit_event()
        log.info("Pipeline started with %d steps", len(self._steps))

        try:
            for index, step in enumerate(self._steps):
                if self._stop_event.is_set():
                    break
                self._current_step_idx = index
                self._execute_step(step)
                if self._stop_event.is_set():
                    break
            if not self._stop_event.is_set():
                self._state = PipelineState.COMPLETED
                log.info("Pipeline completed")
        except Exception as exc:
            self._state = PipelineState.ERROR
            log.exception("Pipeline error")
            self._emit_event(error_msg=str(exc))
        finally:
            self._channel_manager.pipeline_release_all()
            if self._state is PipelineState.STOPPING:
                # Stopping is what the pipeline is doing, not where it ends up.
                # Left as the final state it reads as a pipeline that is still
                # going, and everything gated on that stays shut for good.
                self._state = PipelineState.IDLE
                log.info("Pipeline stopped")
            self._emit_event()

    def _execute_step(self, step: PipelineStep) -> None:
        step.status = StepStatus.RUNNING

        if step.confirm_message:
            self._confirm_event.clear()
            self._emit_event(confirmation_message=step.confirm_message)
            while not self._stop_event.is_set() and not self._skip_event.is_set():
                if self._confirm_event.wait(timeout=self._tick_s):
                    break
            if self._stop_event.is_set():
                step.status = StepStatus.SKIPPED
                self._emit_event()
                return
            if self._skip_event.is_set():
                self._skip_event.clear()
                step.status = StepStatus.SKIPPED
                self._emit_event()
                return

        self._step_start_volumes.clear()
        if self._acquisition:
            for sensor_index in step.sensor_setpoints:
                self._step_start_volumes[sensor_index] = self._acquisition.get_volume(sensor_index)

        self._emit_event()
        for sensor_index, setpoint in step.sensor_setpoints.items():
            channel_index = self._sensor_to_channel.get(sensor_index)
            if channel_index is not None:
                self._channel_manager.pipeline_set_setpoint(channel_index, setpoint)
        for channel_index, pressure_mbar in step.pressure_setpoints.items():
            self._channel_manager.pipeline_set_pressure(channel_index, pressure_mbar)

        step.trigger.reset()
        while not self._stop_event.is_set() and not self._skip_event.is_set():
            self._pause_event.wait()
            if self._stop_event.is_set():
                break

            triggered = step.trigger.check(
                get_flow=self._get_flow,
                get_volume=self._get_volume,
            )
            step_volumes = self._compute_step_volumes()
            self._emit_event(progress=step.trigger.progress(), step_volumes=step_volumes)

            if triggered:
                step.status = StepStatus.COMPLETED
                self._apply_on_complete(step)
                self._emit_event(step_volumes=step_volumes)
                return

            self._stop_event.wait(self._tick_s)

        if self._skip_event.is_set():
            self._skip_event.clear()
        step.status = StepStatus.SKIPPED
        self._emit_event()

    def _apply_on_complete(self, step: PipelineStep) -> None:
        if step.on_complete == "hold":
            return
        for sensor_index in step.sensor_setpoints:
            channel_index = self._sensor_to_channel.get(sensor_index)
            if channel_index is None:
                continue
            if step.on_complete == "zero":
                self._channel_manager.pipeline_set_setpoint(channel_index, 0.0)
            elif step.on_complete == "revert":
                self._channel_manager.pipeline_release_channel(channel_index)
        for channel_index in step.pressure_setpoints:
            if step.on_complete == "zero":
                self._channel_manager.pipeline_set_pressure(channel_index, 0.0)
            elif step.on_complete == "revert":
                self._channel_manager.pipeline_release_channel(channel_index)

    def _get_flow(self, sensor_index: int) -> float:
        if self._acquisition is None:
            return 0.0
        get_flow = getattr(self._acquisition, "get_flow", None)
        if get_flow is not None:
            return float(get_flow(sensor_index))
        sdk = getattr(self._acquisition, "_sdk", None)
        if sdk is not None:
            return float(sdk.get_sensor_value(sensor_index))
        return 0.0

    def _get_volume(self, sensor_index: int) -> float:
        if self._acquisition is None:
            return 0.0
        return float(self._acquisition.get_volume(sensor_index))

    def _compute_step_volumes(self) -> dict[int, float]:
        if self._acquisition is None:
            return {}
        return {
            sensor_index: self._acquisition.get_volume(sensor_index) - start_volume
            for sensor_index, start_volume in self._step_start_volumes.items()
        }

    def _emit_event(
        self,
        progress: float = 0.0,
        error_msg: str = "",
        step_volumes: dict[int, float] | None = None,
        confirmation_message: str = "",
    ) -> None:
        step_name = ""
        if 0 <= self._current_step_idx < len(self._steps):
            step_name = self._steps[self._current_step_idx].name
            if progress == 0.0:
                progress = self._steps[self._current_step_idx].trigger.progress()

        event = PipelineEvent(
            state=self._state,
            current_step=self._current_step_idx,
            total_steps=len(self._steps),
            step_name=step_name,
            progress=progress,
            error_msg=error_msg,
            step_volumes=step_volumes or {},
            confirmation_message=confirmation_message,
        )
        if not self._event_queue.full():
            self._event_queue.put(event)

def build_pipeline_steps(steps: list[ProtocolStep]) -> list[PipelineStep]:
    return [
        PipelineStep(
            name=step.name,
            sensor_setpoints=dict(step.sensor_setpoints),
            trigger=create_trigger(step.trigger_type, step.trigger_params),
            pressure_setpoints=dict(step.pressure_setpoints),
            on_complete=step.on_complete,
            confirm_message=step.confirm_message,
        )
        for step in expand_protocol_steps(steps)
    ]
