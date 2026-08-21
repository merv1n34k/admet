from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from queue import Queue
from typing import Protocol

from admet.engines.acquisition.fluidics.config import (
    BEADS_M_SENSOR,
    CELLS_M_SENSOR,
    DROPSEQ_OIL_FLOW_UL_MIN,
    OIL_L_SENSOR,
    PRIMING_AQUEOUS_FLOW_UL_MIN,
    PRIMING_OIL_FLOW_UL_MIN,
    STABILITY_DURATION_S,
    STABILITY_TOLERANCE_UL_MIN,
)

log = logging.getLogger(__name__)

SensorReader = Callable[[int], float]


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


PIPELINES: dict[str, list[ProtocolStep]] = {
    "Drop-Seq": [
        ProtocolStep(
            name="Prerun",
            sensor_setpoints={0: 250.0, 1: 67.0, 2: 67.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 0, "target_volume_ul": 75.0},
            on_complete="zero",
        ),
        ProtocolStep(
            name="Run-prestab",
            sensor_setpoints={0: 250.0, 1: 0.0, 2: 0.0},
            trigger_type="condition",
            trigger_params={"sensor_index": 0, "min_value": 125.0},
            confirm_message="Prerun complete. Start stabilization?",
            group="run",
            repeat=3,
        ),
        ProtocolStep(
            name="Run-stab",
            sensor_setpoints={0: 250.0, 1: 67.0, 2: 67.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 0, "target_volume_ul": 250.0},
            on_complete="zero",
            group="run",
            repeat=3,
        ),
    ],
    "Priming": [
        ProtocolStep(
            name="Prime Oil L",
            sensor_setpoints={0: 250.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 0, "target_volume_ul": 40.0},
            on_complete="zero",
            confirm_message="Prime Oil L at 250 uL/min for 40 uL. Proceed?",
        ),
        ProtocolStep(
            name="Prime Cells M",
            sensor_setpoints={1: 67.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 1, "target_volume_ul": 5.0},
            on_complete="zero",
            confirm_message="Prime Cells M at 67 uL/min for 5 uL. Proceed?",
        ),
        ProtocolStep(
            name="Prime Beads M",
            sensor_setpoints={2: 67.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 2, "target_volume_ul": 5.0},
            on_complete="zero",
            confirm_message="Prime Beads M at 67 uL/min for 5 uL. Proceed?",
        ),
    ],
    "Wash": [
        ProtocolStep(
            name="Wash flow phase",
            sensor_setpoints={0: 250.0, 1: 80.0, 2: 80.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 0, "target_volume_ul": 500.0},
            on_complete="zero",
            confirm_message="Start wash phase: 250/80/80 uL/min until Oil L dispenses 500 uL?",
        ),
        ProtocolStep(
            name="Wash pressure phase",
            sensor_setpoints={},
            trigger_type="time",
            trigger_params={"duration_s": 120.0},
            pressure_setpoints={0: 2000.0, 1: 2000.0, 2: 2000.0},
            on_complete="zero",
            confirm_message="Set all pressure channels to 2000 mbar for 120 seconds?",
        ),
        ProtocolStep(
            name="Confirm wash complete",
            sensor_setpoints={},
            trigger_type="time",
            trigger_params={"duration_s": 0.0},
            confirm_message="Pressure wash complete. Confirm pipeline close.",
        ),
    ],
}


def build_protocol(name: str, settings: dict | None = None) -> list[ProtocolStep]:
    if name == "Priming" and settings:
        return build_priming_protocol(settings)
    if name == "Drop-Seq" and settings:
        return build_dropseq_protocol(settings)
    if name == "Wash" and settings:
        return build_wash_protocol(settings)
    if name == "Characterise" and settings:
        return build_characterise_protocol(settings)
    protocol = PIPELINES.get(name)
    if protocol is None:
        raise ValueError(f"Unknown pipeline: {name}")
    return list(protocol)


def build_priming_protocol(settings: dict) -> list[ProtocolStep]:
    oil_volume = float(settings["prime_oil_volume_ul"])
    aqueous_volume = float(settings["prime_aqueous_volume_ul"])
    return [
        ProtocolStep(
            name="Prime Oil L",
            sensor_setpoints={OIL_L_SENSOR: PRIMING_OIL_FLOW_UL_MIN},
            trigger_type="volume",
            trigger_params={"sensor_index": OIL_L_SENSOR, "target_volume_ul": oil_volume},
            on_complete="zero",
            confirm_message=f"Prime Oil L at {PRIMING_OIL_FLOW_UL_MIN:g} uL/min for {oil_volume:g} uL. Proceed?",
        ),
        ProtocolStep(
            name="Prime Cells M",
            sensor_setpoints={CELLS_M_SENSOR: PRIMING_AQUEOUS_FLOW_UL_MIN},
            trigger_type="volume",
            trigger_params={"sensor_index": CELLS_M_SENSOR, "target_volume_ul": aqueous_volume},
            on_complete="zero",
            confirm_message=f"Prime Cells M at {PRIMING_AQUEOUS_FLOW_UL_MIN:g} uL/min for {aqueous_volume:g} uL. Proceed?",
        ),
        ProtocolStep(
            name="Prime Beads M",
            sensor_setpoints={BEADS_M_SENSOR: PRIMING_AQUEOUS_FLOW_UL_MIN},
            trigger_type="volume",
            trigger_params={"sensor_index": BEADS_M_SENSOR, "target_volume_ul": aqueous_volume},
            on_complete="zero",
            confirm_message=f"Prime Beads M at {PRIMING_AQUEOUS_FLOW_UL_MIN:g} uL/min for {aqueous_volume:g} uL. Proceed?",
        ),
    ]


def build_dropseq_protocol(settings: dict) -> list[ProtocolStep]:
    steps: list[ProtocolStep] = []
    set_count = int(settings["set_count"])
    replicate_count = int(settings["replicate_count"])
    run_volume_ul = float(settings["run_volume_ul"])
    oil_flow = float(settings.get("run_oil_flow_ul_min", DROPSEQ_OIL_FLOW_UL_MIN))
    aqueous_total = float(settings["run_aqueous_total_flow_ul_min"])
    aqueous_channel = aqueous_total / 2.0
    for set_index in range(1, set_count + 1):
        for replicate_index in range(1, replicate_count + 1):
            label = f"set{set_index:02d}_rep{replicate_index:02d}"
            steps.extend(
                (
                    ProtocolStep(
                        name=f"Run {label}",
                        sensor_setpoints={
                            OIL_L_SENSOR: oil_flow,
                            CELLS_M_SENSOR: aqueous_channel,
                            BEADS_M_SENSOR: aqueous_channel,
                        },
                        trigger_type="volume",
                        trigger_params={
                            "sensor_index": OIL_L_SENSOR,
                            "target_volume_ul": run_volume_ul,
                        },
                        on_complete="zero",
                        confirm_message=(
                            f"Start {label}: Oil L {oil_flow:g} uL/min, "
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


def build_wash_protocol(settings: dict) -> list[ProtocolStep]:
    oil_flow = float(settings["wash_oil_flow_ul_min"])
    aqueous_channel = float(settings["wash_aqueous_total_flow_ul_min"]) / 2.0
    oil_volume = float(settings["wash_oil_volume_ul"])
    pressure = float(settings["wash_pressure_mbar"])
    duration_s = float(settings["wash_pressure_duration_s"])
    return [
        ProtocolStep(
            name="Wash flow phase",
            sensor_setpoints={
                OIL_L_SENSOR: oil_flow,
                CELLS_M_SENSOR: aqueous_channel,
                BEADS_M_SENSOR: aqueous_channel,
            },
            trigger_type="volume",
            trigger_params={"sensor_index": OIL_L_SENSOR, "target_volume_ul": oil_volume},
            on_complete="zero",
            confirm_message=(
                f"Start wash phase 1: {oil_flow:g}/{aqueous_channel:g}/{aqueous_channel:g} "
                f"uL/min until Oil L dispenses {oil_volume:g} uL?"
            ),
        ),
        ProtocolStep(
            name="Wash pressure phase",
            sensor_setpoints={},
            pressure_setpoints={OIL_L_SENSOR: pressure, CELLS_M_SENSOR: pressure, BEADS_M_SENSOR: pressure},
            trigger_type="time",
            trigger_params={"duration_s": duration_s},
            on_complete="zero",
            confirm_message=f"Set all pressure channels to {pressure:g} mbar for {duration_s:g} seconds?",
        ),
        ProtocolStep(
            name="Confirm wash complete",
            sensor_setpoints={},
            trigger_type="time",
            trigger_params={"duration_s": 0.0},
            confirm_message="Pressure wash complete. Confirm pipeline close.",
        ),
    ]


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


class Trigger(ABC):
    @abstractmethod
    def reset(self) -> None:
        ...

    @abstractmethod
    def check(self, get_flow: SensorReader, get_volume: SensorReader) -> bool:
        ...

    @abstractmethod
    def progress(self) -> float:
        ...

    @abstractmethod
    def description(self) -> str:
        ...


class TimeTrigger(Trigger):
    def __init__(self, duration_s: float):
        self._duration_s = duration_s
        self._start_time: float | None = None

    def reset(self) -> None:
        self._start_time = time.monotonic()

    def check(self, get_flow: SensorReader, get_volume: SensorReader) -> bool:
        if self._start_time is None:
            self._start_time = time.monotonic()
        return (time.monotonic() - self._start_time) >= self._duration_s

    def progress(self) -> float:
        if self._start_time is None:
            return 0.0
        if self._duration_s <= 0:
            return 1.0
        elapsed = time.monotonic() - self._start_time
        return min(1.0, elapsed / self._duration_s)

    def description(self) -> str:
        return f"Time: {self._duration_s:.0f}s"


class VolumeTrigger(Trigger):
    def __init__(self, sensor_index: int, target_volume_ul: float):
        self._sensor_index = sensor_index
        self._target_ul = target_volume_ul
        self._start_volume: float | None = None
        self._last_dispensed = 0.0

    def reset(self) -> None:
        self._start_volume = None
        self._last_dispensed = 0.0

    def check(self, get_flow: SensorReader, get_volume: SensorReader) -> bool:
        current = get_volume(self._sensor_index)
        if self._start_volume is None:
            self._start_volume = current
        self._last_dispensed = current - self._start_volume
        return self._last_dispensed >= self._target_ul

    def progress(self) -> float:
        if self._target_ul <= 0:
            return 1.0
        return min(1.0, self._last_dispensed / self._target_ul)

    def description(self) -> str:
        return f"Volume: {self._target_ul:.0f} ul (sensor {self._sensor_index})"


class ThresholdTrigger(Trigger):
    def __init__(
        self,
        sensor_index: int,
        target: float,
        tolerance_pct: float = 5.0,
        stable_duration_s: float = 10.0,
    ):
        self._sensor_index = sensor_index
        self._target = target
        self._tolerance_pct = tolerance_pct
        self._stable_duration_s = stable_duration_s
        self._stable_since: float | None = None

    def reset(self) -> None:
        self._stable_since = None

    def check(self, get_flow: SensorReader, get_volume: SensorReader) -> bool:
        flow = get_flow(self._sensor_index)
        low = self._target * (1 - self._tolerance_pct / 100)
        high = self._target * (1 + self._tolerance_pct / 100)
        in_range = low <= flow <= high

        now = time.monotonic()
        if in_range:
            if self._stable_since is None:
                self._stable_since = now
            return (now - self._stable_since) >= self._stable_duration_s

        self._stable_since = None
        return False

    def progress(self) -> float:
        if self._stable_since is None:
            return 0.0
        if self._stable_duration_s <= 0:
            return 1.0
        elapsed = time.monotonic() - self._stable_since
        return min(1.0, elapsed / self._stable_duration_s)

    def description(self) -> str:
        return (
            f"Threshold: {self._target:.1f} +/-{self._tolerance_pct:.0f}% "
            f"for {self._stable_duration_s:.0f}s (sensor {self._sensor_index})"
        )


class ConditionTrigger(Trigger):
    def __init__(
        self,
        sensor_index: int,
        min_value: float | None = None,
        max_value: float | None = None,
    ):
        self._sensor_index = sensor_index
        self._min_value = min_value
        self._max_value = max_value
        self._triggered = False

    def reset(self) -> None:
        self._triggered = False

    def check(self, get_flow: SensorReader, get_volume: SensorReader) -> bool:
        flow = get_flow(self._sensor_index)
        ok = True
        if self._min_value is not None:
            ok = ok and flow >= self._min_value
        if self._max_value is not None:
            ok = ok and flow <= self._max_value
        if ok:
            self._triggered = True
        return self._triggered

    def progress(self) -> float:
        return 1.0 if self._triggered else 0.0

    def description(self) -> str:
        parts = []
        if self._min_value is not None:
            parts.append(f">= {self._min_value:.1f}")
        if self._max_value is not None:
            parts.append(f"<= {self._max_value:.1f}")
        return f"Condition: sensor {self._sensor_index} {' & '.join(parts)}"


class StabilityTrigger(Trigger):
    """Fires once a channel's flow has held steady, or once it gives up waiting.

    Applies the same rule the monitor uses -- spread within twice the tolerance
    across the window -- to the readings the trigger already receives.

    Timing out is a legitimate outcome, not a failure: when the controller cannot
    reach the setpoint the flow never settles, and the point recorded at that
    moment is the ceiling the system actually delivers. Waiting forever would just
    stall the sweep on the very case it exists to find.
    """

    def __init__(
        self,
        sensor_index: int,
        tolerance_ul_min: float = STABILITY_TOLERANCE_UL_MIN,
        window_s: float = STABILITY_DURATION_S,
        timeout_s: float = 60.0,
    ):
        self._sensor_index = sensor_index
        self._tolerance = tolerance_ul_min
        self._window_s = window_s
        self._timeout_s = timeout_s
        self._samples: list[tuple[float, float]] = []
        self._started = 0.0
        self.timed_out = False

    def reset(self) -> None:
        self._samples = []
        self._started = 0.0
        self.timed_out = False

    def check(self, get_flow: SensorReader, get_volume: SensorReader) -> bool:
        now = time.monotonic()
        if not self._started:
            self._started = now
        self._samples.append((now, float(get_flow(self._sensor_index))))
        cutoff = now - self._window_s
        while len(self._samples) > 2 and self._samples[1][0] < cutoff:
            self._samples.pop(0)

        if now - self._started >= self._timeout_s:
            self.timed_out = True
            return True
        if now - self._started < self._window_s:
            return False  # not enough history to call it steady yet
        flows = [flow for moment, flow in self._samples if moment >= cutoff]
        return len(flows) >= 2 and (max(flows) - min(flows)) <= 2 * self._tolerance

    def progress(self) -> float:
        if not self._started:
            return 0.0
        elapsed = time.monotonic() - self._started
        return min(1.0, elapsed / self._window_s) if self._window_s else 1.0

    def description(self) -> str:
        return f"Stable: sensor {self._sensor_index} within {self._tolerance:g} uL/min"


class ConfirmationTrigger(Trigger):
    def __init__(self, message: str):
        self._message = message
        self._event = threading.Event()

    @property
    def message(self) -> str:
        return self._message

    def confirm(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()

    def check(self, get_flow: SensorReader, get_volume: SensorReader) -> bool:
        return self._event.is_set()

    def progress(self) -> float:
        return 1.0 if self._event.is_set() else 0.0

    def description(self) -> str:
        return f"Confirm: {self._message}"


CHARACTERISE_FRACTIONS = (0.2, 0.4, 0.6, 0.8, 1.0)

# Protocols built from settings rather than stored as a fixed step list. They are
# still valid pipeline names, so they belong in the allowed set even though they
# have no entry in PIPELINES.
BUILT_PROTOCOLS = ("Characterise",)


def protocol_names() -> tuple[str, ...]:
    """Every name build_protocol accepts."""
    return tuple(sorted({*PIPELINES, *BUILT_PROTOCOLS}))


def build_characterise_protocol(settings: dict) -> list[ProtocolStep]:
    """Sweep the setup to measure what the plumbing and chip cost.

    Every channel is scaled by the same fraction of its working flow, so the phase
    ratio holds throughout. That is what keeps each channel linear in its own flow
    and lets the three be fitted separately afterwards.

    Flow regulation is used rather than open-loop pressure: driving pressure would
    let each channel land wherever its own resistance put it and the ratio would
    not hold. The pressure the controller settles at is the measurement.
    """
    oil = float(settings["run_oil_flow_ul_min"])
    aqueous = float(settings["run_aqueous_total_flow_ul_min"]) / 2.0
    steps: list[ProtocolStep] = []
    for fraction in CHARACTERISE_FRACTIONS:
        steps.append(
            ProtocolStep(
                name=f"Sweep {fraction * 100:.0f}%",
                sensor_setpoints={
                    OIL_L_SENSOR: oil * fraction,
                    CELLS_M_SENSOR: aqueous * fraction,
                    BEADS_M_SENSOR: aqueous * fraction,
                },
                trigger_type="stability",
                trigger_params={"sensor_index": OIL_L_SENSOR},
                on_complete="hold",
                group="characterise",
            )
        )
    steps.append(
        ProtocolStep(
            name="Stop",
            sensor_setpoints={OIL_L_SENSOR: 0.0, CELLS_M_SENSOR: 0.0, BEADS_M_SENSOR: 0.0},
            trigger_type="time",
            trigger_params={"duration_s": 1.0},
            on_complete="zero",
            group="characterise",
        )
    )
    return steps


def create_trigger(trigger_type: str, params: dict) -> Trigger:
    if trigger_type == "time":
        return TimeTrigger(**params)
    if trigger_type == "stability":
        return StabilityTrigger(**params)
    if trigger_type == "volume":
        return VolumeTrigger(**params)
    if trigger_type == "threshold":
        return ThresholdTrigger(**params)
    if trigger_type == "condition":
        return ConditionTrigger(**params)
    if trigger_type == "confirmation":
        return ConfirmationTrigger(**params)
    raise ValueError(f"Unknown trigger type: {trigger_type}")


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

