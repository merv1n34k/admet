"""Running steps against the hardware.

The vocabulary of a step lives here, because this is what executes one. What the
steps mean -- which experiment they belong to -- is not the engine's business: it
is handed a list and runs it. Protocols live in the workflow layer.

Each step's trigger becomes an object that can be asked whether it is finished.
The pipeline applies the setpoints, waits, and emits an event on every tick, so
a caller can follow progress without polling the instrument.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from queue import Queue
from typing import Any, Protocol

from admet.core.clock import now_iso
from admet.engines.acquisition.triggers import (
    ConfirmationTrigger, Trigger, BoundedTrigger, create_trigger,
)

log = logging.getLogger(__name__)

# What to do with a step's setpoints once it ends: leave them, take them to
# zero, or put back whatever was there before.
ON_COMPLETE = ("hold", "zero", "revert")


@dataclass(frozen=True)
class ProtocolStep:
    name: str
    sensor_setpoints: dict[int, float]
    trigger_type: str
    trigger_params: dict
    pressure_setpoints: dict[int, float] = field(default_factory=dict)
    on_complete: str = "hold"
    confirm_message: str = ""
    repeat: int = 1
    group: str = ""
    timeout_s: float | None = None


def expand_protocol_steps(steps: list[ProtocolStep]) -> list[ProtocolStep]:
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


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    ERROR = "error"


class StepOutcome(StrEnum):
    """How a step ended, which "the trigger fired" does not say.

    A stability trigger fires when the flow holds still and also when it gives
    up waiting. Both used to be recorded as completed, so a settle that never
    happened read exactly like one that did.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
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
    # Set by the engine as it emits. The sequence is what lets a reader ask for
    # what it has not seen without removing anything.
    sequence: int = 0
    at: str = ""
    monotonic: float = 0.0
    outcome: StepOutcome = StepOutcome.RUNNING

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
        recent_events: int = 500,
        next_sequence: Any = None,
        on_event: Any = None,
    ):
        super().__init__(daemon=True, name="PipelineEngine")
        self._steps = steps
        self._channel_manager = channel_manager
        self._acquisition = acquisition
        self._event_queue = event_queue or Queue()
        self._sensor_to_channel = sensor_to_channel
        self._tick_s = tick_s

        self._state = PipelineState.IDLE

        # Observation, kept apart from the queue for the same reason as the
        # acquisition snapshots: a full queue must not cost a reader an event.
        # A protocol is one of several a session runs, so the numbering cannot
        # belong to it: a reader holding a cursor from the last protocol would
        # be handed a new one starting at 1 and see nothing. Whoever owns the
        # session supplies the counter; on its own it keeps its own.
        self._sequence = 0
        self._next_sequence = next_sequence or self._own_sequence
        self._on_event = on_event
        self._latest_event: PipelineEvent | None = None
        self._recent_events: deque[PipelineEvent] = deque(maxlen=recent_events)
        self._observation_lock = threading.Lock()
        # -1 makes the first emitted event unambiguously about protocol start,
        # before the first step becomes current.
        self._current_step_idx = -1
        self._step_start_volumes: dict[int, float] = {}

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._skip_event = threading.Event()
        self._confirm_event = threading.Event()
        # The prompt of a gate still waiting; every event carries it, so a pause or resume
        # at the gate does not hide it.
        self._gate_message = ""
        self._flowing_step: PipelineStep | None = None
        self._flowing_lock = threading.Lock()

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
            # Pausing set the channels to zero; the step resumes where it was.
            with self._flowing_lock:
                if self._flowing_step is not None:
                    self._apply_setpoints(self._flowing_step)
            self._pause_event.set()
            self._state = PipelineState.RUNNING
            self._emit_event()
            log.info("Pipeline resumed")

    def confirm_pending(self) -> None:
        self._confirm_event.set()

    def stop(self) -> None:
        log.info("Pipeline stop requested")
        self._channel_manager.pipeline_zero_all()
        self._state = PipelineState.STOPPING
        self._stop_event.set()
        self._pause_event.set()
        self._confirm_event.set()

    def skip_step(self) -> None:
        self._channel_manager.pipeline_zero_all()
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
            self._emit_event(error_msg=str(exc), outcome=StepOutcome.ERROR)
        finally:
            self._channel_manager.pipeline_release_all()
            if self._state is PipelineState.STOPPING:
                # Stopping is what the pipeline is doing, not where it ends up.
                # Left as the final state it reads as a pipeline that is still
                # going, and everything gated on that stays shut for good.
                self._state = PipelineState.IDLE
                log.info("Pipeline stopped")
                self._emit_event(outcome=StepOutcome.CANCELLED, about_the_run=True)
                return
            self._emit_event(
                outcome=StepOutcome.ERROR
                if self._state is PipelineState.ERROR
                else StepOutcome.COMPLETED,
                about_the_run=True,
            )

    def _execute_step(self, step: PipelineStep) -> None:
        step.status = StepStatus.RUNNING

        if isinstance(step.trigger, ConfirmationTrigger):
            self._confirm_event.clear()
            self._gate_message = step.confirm_message or step.trigger.message
            self._emit_event()
            while not self._stop_event.is_set() and not self._skip_event.is_set():
                if self._confirm_event.wait(timeout=self._tick_s):
                    break
            self._gate_message = ""
            if self._stop_event.is_set():
                step.status = StepStatus.CANCELLED
                self._emit_event(outcome=StepOutcome.CANCELLED)
                return
            if self._skip_event.is_set():
                self._skip_event.clear()
                step.status = StepStatus.SKIPPED
                self._emit_event(outcome=StepOutcome.SKIPPED)
                return
            step.status = StepStatus.COMPLETED
            self._apply_on_complete(step)
            self._emit_event(outcome=StepOutcome.COMPLETED, progress=1.0)
            return

        if step.confirm_message:
            self._confirm_event.clear()
            self._gate_message = step.confirm_message
            self._emit_event()
            while not self._stop_event.is_set() and not self._skip_event.is_set():
                if self._confirm_event.wait(timeout=self._tick_s):
                    break
            self._gate_message = ""
            if self._stop_event.is_set():
                step.status = StepStatus.CANCELLED
                self._emit_event(outcome=StepOutcome.CANCELLED)
                return
            if self._skip_event.is_set():
                self._skip_event.clear()
                step.status = StepStatus.SKIPPED
                self._emit_event(outcome=StepOutcome.SKIPPED)
                return

            self._pause_event.wait()
            if self._stop_event.is_set():
                step.status = StepStatus.CANCELLED
                self._emit_event(outcome=StepOutcome.CANCELLED)
                return

        self._step_start_volumes.clear()
        if self._acquisition:
            for sensor_index in step.sensor_setpoints:
                self._step_start_volumes[sensor_index] = self._acquisition.get_volume(sensor_index)

        self._emit_event()
        with self._flowing_lock:
            self._apply_setpoints(step)
            self._flowing_step = step
        try:
            self._run_trigger(step)
        finally:
            self._stop_flowing()

    def _stop_flowing(self) -> None:
        # Before the step's end is applied, so a resume never restarts a finished step.
        with self._flowing_lock:
            self._flowing_step = None

    def _apply_setpoints(self, step: PipelineStep) -> None:
        for sensor_index, setpoint in step.sensor_setpoints.items():
            channel_index = self._sensor_to_channel.get(sensor_index)
            if channel_index is not None:
                self._channel_manager.pipeline_set_setpoint(channel_index, setpoint)
        for channel_index, pressure_mbar in step.pressure_setpoints.items():
            self._channel_manager.pipeline_set_pressure(channel_index, pressure_mbar)

    def _run_trigger(self, step: PipelineStep) -> None:
        step.trigger.reset()
        while not self._stop_event.is_set() and not self._skip_event.is_set():
            waited_from = time.monotonic()
            self._pause_event.wait()
            paused_s = time.monotonic() - waited_from
            if paused_s > 0.001:
                step.trigger.shift(paused_s)
            if self._stop_event.is_set():
                break

            triggered = step.trigger.check(
                get_flow=self._get_flow,
                get_volume=self._get_volume,
            )
            step_volumes = self._compute_step_volumes()
            self._emit_event(progress=step.trigger.progress(), step_volumes=step_volumes)

            if triggered:
                # A trigger that gave up waiting also fires. Asking it which of
                # the two happened is the difference between a settle and a
                # timeout wearing a settle's clothes.
                timed_out = bool(getattr(step.trigger, "timed_out", False))
                self._stop_flowing()
                if isinstance(step.trigger, BoundedTrigger) and step.trigger.expired:
                    self._channel_manager.pipeline_zero_all()
                    raise TimeoutError(f"step {step.name} timed out")
                step.status = StepStatus.TIMED_OUT if timed_out else StepStatus.COMPLETED
                self._apply_on_complete(step)
                self._emit_event(
                    step_volumes=step_volumes,
                    outcome=StepOutcome.TIMED_OUT if timed_out else StepOutcome.COMPLETED,
                )
                return

            self._stop_event.wait(self._tick_s)

        if self._skip_event.is_set():
            self._skip_event.clear()
            step.status = StepStatus.SKIPPED
            self._emit_event(outcome=StepOutcome.SKIPPED)
            return
        step.status = StepStatus.CANCELLED
        self._emit_event(outcome=StepOutcome.CANCELLED)

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

    def _own_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def latest_event(self) -> PipelineEvent | None:
        """The newest event, without removing it. None before the first."""
        with self._observation_lock:
            return self._latest_event

    def events_after(self, sequence: int = 0, limit: int = 100) -> list[PipelineEvent]:
        """Events newer than a sequence, oldest first, without removing any.

        Bounded at both ends: the ring holds only so much history, so a reader
        that falls far enough behind sees a gap rather than a stall. The
        sequence it gets back tells it where it now is.
        """
        with self._observation_lock:
            events = list(self._recent_events)
        newer = [event for event in events if event.sequence > sequence]
        return newer[: max(0, limit)] if limit else newer

    def _emit_event(
        self,
        progress: float = 0.0,
        error_msg: str = "",
        step_volumes: dict[int, float] | None = None,
        confirmation_message: str | None = None,
        outcome: StepOutcome = StepOutcome.RUNNING,
        about_the_run: bool = False,
    ) -> None:
        if confirmation_message is None:
            confirmation_message = self._gate_message
        # A terminal event is about the run, so it carries no step name. Left
        # with the last step's name, a run that completed after that step timed
        # out would read as the step having completed.
        step_name = ""
        if not about_the_run and 0 <= self._current_step_idx < len(self._steps):
            step_name = self._steps[self._current_step_idx].name
            if progress == 0.0:
                progress = self._steps[self._current_step_idx].trigger.progress()

        sequence = self._next_sequence()
        with self._observation_lock:
            event = PipelineEvent(
                state=self._state,
                current_step=self._current_step_idx,
                total_steps=len(self._steps),
                step_name=step_name,
                progress=progress,
                error_msg=error_msg,
                step_volumes=step_volumes or {},
                confirmation_message=confirmation_message,
                sequence=sequence,
                at=now_iso(),
                monotonic=time.monotonic(),
                outcome=outcome,
            )
            self._latest_event = event
            self._recent_events.append(event)

        if self._on_event is not None:
            self._on_event(event)

        # Best effort, and after the observation state: a full queue must not
        # cost a reader the confirmation prompt or the outcome.
        if not self._event_queue.full():
            self._event_queue.put(event)

def build_pipeline_steps(steps: list[ProtocolStep]) -> list[PipelineStep]:
    return [
        PipelineStep(
            name=step.name,
            sensor_setpoints=dict(step.sensor_setpoints),
            trigger=(
                BoundedTrigger(create_trigger(step.trigger_type, step.trigger_params), step.timeout_s)
                if step.timeout_s is not None
                else create_trigger(step.trigger_type, step.trigger_params)
            ),
            pressure_setpoints=dict(step.pressure_setpoints),
            on_complete=step.on_complete,
            confirm_message=step.confirm_message,
        )
        for step in expand_protocol_steps(steps)
    ]
