"""What a protocol step waits for.

A trigger answers one question -- is this step done yet? -- and says how far
along it is. Every trigger that can wait indefinitely takes a timeout, and
reports having used it, because a step that gave up must never be mistaken for
a step that succeeded.
"""

from __future__ import annotations

import logging
import threading
import statistics
import time
from abc import ABC, abstractmethod
from collections.abc import Callable

from admet.engines.acquisition.fluidics.config import (
    STABILITY_DURATION_S,
    STABILITY_TIMEOUT_S,
    STABILITY_TOLERANCE_UL_MIN,
)

log = logging.getLogger(__name__)

SensorReader = Callable[[int], float]

SensorReader = Callable[[int], float]

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

class BoundedTrigger(Trigger):
    def __init__(self, inner: Trigger, timeout_s: float):
        self.inner = inner
        self.timeout_s = timeout_s
        self.started = 0.0
        self.timed_out = False

    def reset(self):
        self.started = time.monotonic()
        self.timed_out = False
        self.inner.reset()

    def check(self, get_flow, get_volume):
        self.timed_out = time.monotonic() - self.started >= self.timeout_s
        if self.timed_out:
            return True
        done = self.inner.check(get_flow, get_volume)
        self.timed_out = bool(getattr(self.inner, "timed_out", False))
        return done

    def progress(self):
        return self.inner.progress()

    def description(self):
        return self.inner.description()


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

VOLUME_MODES = ("adaptive", "integral")


class VolumeTrigger(Trigger):
    """Stops a step once its volume is dispensed.

    integral  stops when the counted volume reaches the target. Flow keeps
              coming for a moment after the stop, so it overshoots by that tail.
    adaptive  stops early by the tail it expects: when counted + k x flow / 60
              reaches the target, k being how many seconds of flow the channel
              delivers after a stop. With no k yet it behaves as integral.

    k is TAIL_S when the protocol fixes it, otherwise whatever tail_source
    returns -- the engine supplies the value it learned from earlier stops.
    The flow is the median of the last three readings, so a single misread
    cannot make the step stop early.
    """

    def __init__(self, sensor_index: int, target_volume_ul: float, mode: str = "adaptive",
                 tail_s: float | None = None):
        if mode not in VOLUME_MODES:
            raise ValueError(f"volume mode must be one of {', '.join(VOLUME_MODES)}")
        self._sensor_index = sensor_index
        self._target_ul = target_volume_ul
        self.mode = mode
        self.tail_s = tail_s
        self.tail_source = None
        self._start_volume: float | None = None
        self._last_dispensed = 0.0
        self._flows: list[float] = []
        self.tail_used_s: float | None = None

    @property
    def sensor_index(self) -> int:
        return self._sensor_index

    def reset(self) -> None:
        self._start_volume = None
        self._last_dispensed = 0.0
        self._flows = []
        self.tail_used_s = None

    def _tail(self) -> float | None:
        if self.mode != "adaptive":
            return None
        if self.tail_s is not None:
            return self.tail_s
        return self.tail_source() if self.tail_source is not None else None

    def check(self, get_flow: SensorReader, get_volume: SensorReader) -> bool:
        current = get_volume(self._sensor_index)
        if self._start_volume is None:
            self._start_volume = current
        self._last_dispensed = current - self._start_volume
        self._flows = (self._flows + [float(get_flow(self._sensor_index))])[-3:]
        tail = self._tail()
        expected_tail = 0.0
        if tail is not None and tail > 0 and len(self._flows) == 3:
            expected_tail = tail * max(0.0, statistics.median(self._flows)) / 60.0
        done = self._last_dispensed + expected_tail >= self._target_ul
        if done:
            self.tail_used_s = tail if expected_tail else None
        return done

    def progress(self) -> float:
        if self._target_ul <= 0:
            return 1.0
        return min(1.0, self._last_dispensed / self._target_ul)

    def description(self) -> str:
        return f"Volume: {self._target_ul:g} ul (sensor {self._sensor_index}, {self.mode})"

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
        timeout_s: float = STABILITY_TIMEOUT_S,
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
        """How close this step is to giving up, not to settling.

        A settle has no schedule -- it happens when the flow holds still, which may
        be at once or never. Reporting the window instead would sit at 100% for the
        rest of the wait and read as a stall, which is exactly how an unsettled
        sweep used to look. Filling towards the timeout says what is really being
        waited on: if the bar fills, the step gave up and recorded the ceiling.
        """
        if not self._started:
            return 0.0
        elapsed = time.monotonic() - self._started
        return min(1.0, elapsed / self._timeout_s) if self._timeout_s else 1.0

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

# What can end a step, and what each one needs. Read from the triggers
# themselves so a caller can be told what to pass instead of finding out from a
# TypeError, and so this cannot drift from the constructors it describes.
_TRIGGERS: dict[str, type] = {
    "time": TimeTrigger,
    "volume": VolumeTrigger,
    "stability": StabilityTrigger,
    "threshold": ThresholdTrigger,
    "condition": ConditionTrigger,
    "confirmation": ConfirmationTrigger,
}
TRIGGER_TYPES = tuple(_TRIGGERS)


def trigger_params(trigger_type: str) -> dict[str, bool]:
    """The settings a trigger takes, and whether each one is required."""
    import inspect

    signature = inspect.signature(_TRIGGERS[trigger_type].__init__)
    return {
        name: parameter.default is inspect.Parameter.empty
        for name, parameter in signature.parameters.items()
        if name != "self"
    }


def create_trigger(trigger_type: str, params: dict) -> Trigger:
    if trigger_type not in _TRIGGERS:
        raise ValueError(f"Unknown trigger type: {trigger_type}")
    return _TRIGGERS[trigger_type](**params)
