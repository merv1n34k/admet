from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable

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

    def get_dispensed(self, get_volume: SensorReader) -> float:
        if self._start_volume is None:
            return 0.0
        return get_volume(self._sensor_index) - self._start_volume

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


def create_trigger(trigger_type: str, params: dict) -> Trigger:
    if trigger_type == "time":
        return TimeTrigger(**params)
    if trigger_type == "volume":
        return VolumeTrigger(**params)
    if trigger_type == "threshold":
        return ThresholdTrigger(**params)
    if trigger_type == "condition":
        return ConditionTrigger(**params)
    if trigger_type == "confirmation":
        return ConfirmationTrigger(**params)
    raise ValueError(f"Unknown trigger type: {trigger_type}")
