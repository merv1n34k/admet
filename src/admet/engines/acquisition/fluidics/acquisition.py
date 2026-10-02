from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from queue import Queue


import numpy as np

from .config import (
    ACQUISITION_INTERVAL_MS,
    STABILITY_TOLERANCE_UL_MIN,
    STABILITY_WINDOW_SAMPLES,
    STATS_WINDOW_SAMPLES,
)
from .csv_logger import CsvLogger
from .sdk import FluigentSDK

log = logging.getLogger(__name__)


class FlowIntegral:
    """One sensor's dispensed volume, with bad readings rejected and bridged.

    Each reading is judged against the median of its neighbours: the last
    RADIUS kept readings, itself, and the RADIUS after it. Far from that median
    is a spike (5 -> -1530 -> 5.2). A median ignores a burst of up to RADIUS bad
    readings, follows a real ramp or step, and needs no trusted reference that
    one bad reading could poison. Rejected readings never become context.

    Volume is the trapezoid over kept readings and real timestamps, signed, so
    a rejected reading is bridged by the line between its neighbours: liquid
    kept flowing while the sensor misread.

    Deciding a reading needs RADIUS later ones, so decisions lag. The volume
    reported now adds the undecided readings provisionally, holding the last
    kept flow through any that disagree with it, so a volume step does not stop
    late while the filter waits.
    """

    def __init__(self, *, radius: int = 2, jump_abs: float = 20.0, jump_rel: float = 0.5,
                 limit: float = float("inf"), keep_rejected: int = 500):
        self.radius = radius
        self.jump_abs = jump_abs
        self.jump_rel = jump_rel
        self.limit = limit
        self.decided = 0.0
        self.rejected: deque[tuple[float, float]] = deque(maxlen=keep_rejected)
        self._left: list[float] = []
        self._pending: list[tuple[float, float]] = []
        self._last_kept: tuple[float, float] | None = None

    def jump(self, around: float) -> float:
        return max(self.jump_abs, self.jump_rel * abs(around))

    def add(self, t: float, q: float) -> None:
        if not math.isfinite(q) or abs(q) > self.limit:
            self.rejected.append((t, q))
            return
        self._pending.append((t, q))
        while len(self._pending) > self.radius:
            t_i, q_i = self._pending.pop(0)
            around = statistics.median(self._left + [q_i] + [value for _, value in self._pending])
            if abs(q_i - around) > self.jump(around):
                self.rejected.append((t_i, q_i))
                continue
            self._keep(t_i, q_i)

    def _keep(self, t: float, q: float) -> None:
        if self._last_kept is not None:
            t_prev, q_prev = self._last_kept
            self.decided += (q_prev + q) / 2 * (t - t_prev) / 60.0
        self._last_kept = (t, q)
        self._left = (self._left + [q])[-self.radius:]

    @property
    def volume(self) -> float:
        if self._last_kept is None:
            return self.decided
        volume = self.decided
        t_prev, q_prev = self._last_kept
        for t, q in self._pending:
            q_use = q if abs(q - q_prev) <= self.jump(q_prev) else q_prev
            volume += (q_prev + q_use) / 2 * (t - t_prev) / 60.0
            t_prev, q_prev = t, q_use
        return volume


@dataclass(frozen=True)
class ChannelStats:
    mean: float = 0.0
    std: float = 0.0
    min: float = 0.0
    max: float = 0.0


@dataclass(frozen=True)
class DataSnapshot:
    timestamp: str
    elapsed_s: float
    pressures: list[float]
    flows: list[float]
    volumes_ul: list[float]
    pressure_stats: list[ChannelStats]
    flow_stats: list[ChannelStats]
    stability: list[bool] = field(default_factory=list)


class AcquisitionThread(threading.Thread):
    def __init__(
        self,
        sdk: FluigentSDK,
        pressure_count: int,
        sensor_count: int,
        data_queue: Queue[DataSnapshot],
        csv_logger: CsvLogger | None = None,
        *,
        interval_ms: int = ACQUISITION_INTERVAL_MS,
        stats_window_samples: int = STATS_WINDOW_SAMPLES,
        stability_window_samples: int = STABILITY_WINDOW_SAMPLES,
        stability_tolerance_ul_min: float = STABILITY_TOLERANCE_UL_MIN,
        recent_samples: int = 600,
    ):
        super().__init__(daemon=True, name="AcquisitionThread")
        self._sdk = sdk
        self._pressure_count = pressure_count
        self._sensor_count = sensor_count
        self._data_queue = data_queue
        self._csv_logger = csv_logger
        self._csv_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._start_time = 0.0
        self._interval_ms = interval_ms
        self._stability_tolerance_ul_min = stability_tolerance_ul_min

        self._pressure_history: list[deque] = [
            deque(maxlen=stats_window_samples) for _ in range(pressure_count)
        ]
        self._flow_history: list[deque] = [
            deque(maxlen=stats_window_samples) for _ in range(sensor_count)
        ]
        self._stability_history: list[deque] = [
            deque(maxlen=stability_window_samples) for _ in range(sensor_count)
        ]
        self._integrals = [FlowIntegral() for _ in range(sensor_count)]
        self._lock = threading.Lock()

        # Observation, kept apart from the queue. The queue is bounded and has
        # one consumer at most; anyone asking what the rig is doing right now
        # must not have to drain it, and must not be starved when it is full.
        self._latest: DataSnapshot | None = None
        self._recent: deque[DataSnapshot] = deque(maxlen=recent_samples)
        self._observation_lock = threading.Lock()

    def set_csv_logger(self, logger: CsvLogger | None) -> None:
        with self._csv_lock:
            self._csv_logger = logger

    def get_volume(self, sensor_index: int) -> float:
        with self._lock:
            if 0 <= sensor_index < len(self._integrals):
                return self._integrals[sensor_index].volume
            return 0.0

    def rejected_readings(self, sensor_index: int) -> list[tuple[float, float]]:
        """Recent readings left out of the volume, as (elapsed_s, flow)."""
        with self._lock:
            if 0 <= sensor_index < len(self._integrals):
                return list(self._integrals[sensor_index].rejected)
            return []

    def get_flow(self, sensor_index: int) -> float:
        return float(self._sdk.get_sensor_value(sensor_index))

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self._start_time = time.monotonic()
        interval = self._interval_ms / 1000.0
        while not self._stop_event.is_set():
            loop_start = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                log.exception("Acquisition poll error")
            elapsed = time.monotonic() - loop_start
            sleep_time = max(0.0, interval - elapsed)
            if sleep_time > 0:
                self._stop_event.wait(sleep_time)

    def poll_once(self) -> DataSnapshot:
        if self._start_time == 0.0:
            self._start_time = time.monotonic()

        now = datetime.now()
        elapsed_s = time.monotonic() - self._start_time
        timestamp = now.strftime("%H:%M:%S.%f")[:-3]
        pressures = [self._sdk.get_pressure(index) for index in range(self._pressure_count)]
        flows = [self._sdk.get_sensor_value(index) for index in range(self._sensor_count)]

        for index, pressure in enumerate(pressures):
            self._pressure_history[index].append(pressure)
        for index, flow in enumerate(flows):
            self._flow_history[index].append(flow)
            self._stability_history[index].append(flow)

        with self._lock:
            for integral, flow in zip(self._integrals, flows):
                integral.add(elapsed_s, flow)
            volumes_snapshot = [integral.volume for integral in self._integrals]

        stability = [self._is_stable(history) for history in self._stability_history]
        pressure_stats = [self._compute_stats(history) for history in self._pressure_history]
        flow_stats = [self._compute_stats(history) for history in self._flow_history]
        snapshot = DataSnapshot(
            timestamp=timestamp,
            elapsed_s=elapsed_s,
            pressures=pressures,
            flows=flows,
            volumes_ul=volumes_snapshot,
            pressure_stats=pressure_stats,
            flow_stats=flow_stats,
            stability=stability,
        )

        with self._csv_lock:
            if self._csv_logger:
                self._csv_logger.write_row(
                    timestamp,
                    elapsed_s,
                    pressures,
                    flows,
                    volumes=volumes_snapshot,
                    stability=stability,
                )

        with self._observation_lock:
            self._latest = snapshot
            self._recent.append(snapshot)

        # Best effort, and deliberately after the observation state: a full
        # queue must not cost anyone the current reading.
        if not self._data_queue.full():
            self._data_queue.put(snapshot)
        return snapshot

    @property
    def started_monotonic(self) -> float:
        """When polling began, so a CSV's elapsed_s can be placed on a clock.

        Every row is stamped relative to this, and protocol events carry raw
        monotonic times. Without it the two cannot be lined up, and a sampling
        window cannot be turned into the rows it covers.
        """
        return self._start_time

    def latest_snapshot(self) -> DataSnapshot | None:
        """The newest reading, without removing it. None before the first poll."""
        with self._observation_lock:
            return self._latest

    def recent_snapshots(self, limit: int = 0) -> list[DataSnapshot]:
        """The newest readings, oldest first, for a short sampling window."""
        with self._observation_lock:
            snapshots = list(self._recent)
        return snapshots[-limit:] if limit > 0 else snapshots

    def _is_stable(self, history: deque) -> bool:
        if len(history) < history.maxlen:
            return False
        return bool(np.ptp(np.array(history)) <= 2 * self._stability_tolerance_ul_min)

    @staticmethod
    def _compute_stats(history: deque) -> ChannelStats:
        if not history:
            return ChannelStats()
        values = np.array(history)
        return ChannelStats(
            mean=float(np.mean(values)),
            std=float(np.std(values, ddof=1)) if len(values) > 1 else 0.0,
            min=float(np.min(values)),
            max=float(np.max(values)),
        )
