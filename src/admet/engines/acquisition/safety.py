"""The safety boundary: a latch, and a watchdog that trips it.

Two things live here, and neither knows anything about protocols.

The watchdog reads *measured* pressure, not requested pressure. A setpoint is
what somebody asked for; pressure is what the rig actually did about it, and the
failure this exists to catch -- a line that will not flow, so the controller
pushes harder and harder -- shows up only in the measurement.

The latch is what makes a trip mean something afterwards. It stays set until it
is explicitly reset, and it refuses to reset while anything still reads unsafe.
A trip that cleared itself would be a trip nobody found out about.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from admet.core.clock import now_iso


@dataclass
class SafetyState:
    armed: bool = False
    tripped: bool = False
    reason: str = ""
    at: str | None = None
    limits: dict[str, float] = field(default_factory=dict)
    readings: dict[str, float] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "armed": self.armed,
            "tripped": self.tripped,
            "reason": self.reason,
            "at": self.at,
            "limits": dict(self.limits),
            "readings": dict(self.readings),
        }


class PressureWatchdog:
    """Watches measured pressures and trips once, immediately, on a breach.

    Deliberately not part of the protocol runtime. A protocol that has hung, or
    one whose own logic is what is wrong, must still be stopped -- so this runs
    off the acquisition poll, which is the same place the numbers come from.
    """

    def __init__(self, on_trip: Callable[[str], None]):
        self._on_trip = on_trip
        self._lock = threading.Lock()
        self.state = SafetyState()

    # -- arming -------------------------------------------------------------
    def arm(self, limits: dict[int, float]) -> dict[str, Any]:
        """Arm a ceiling per pressure channel, in mbar.

        Explicit per run rather than a standing default: a limit that is always
        on is one nobody chose, and the right ceiling depends on what is
        plumbed in.
        """
        with self._lock:
            if self.state.tripped:
                raise SafetyTripped(
                    f"the safety latch is still set ({self.state.reason}); "
                    f"reset it before arming again"
                )
            self.state.armed = bool(limits)
            self.state.limits = {str(index): float(limit) for index, limit in limits.items()}
        return self.state.describe()

    # -- watching -----------------------------------------------------------
    def check(self, pressures: list[float]) -> None:
        """One poll's worth of measurements. Trips at most once."""
        with self._lock:
            if not self.state.armed or self.state.tripped:
                return
            breach = None
            for index, limit in self.state.limits.items():
                position = int(index)
                if 0 <= position < len(pressures) and pressures[position] > limit:
                    breach = (position, pressures[position], limit)
                    break
            if breach is None:
                return
            position, measured, limit = breach
            self.state.tripped = True
            self.state.at = now_iso()
            self.state.reason = (
                f"channel {position} reached {measured:.1f} mbar, "
                f"over its {limit:.0f} mbar limit"
            )
            self.state.readings = {str(i): float(p) for i, p in enumerate(pressures)}
            reason = self.state.reason

        # Outside the lock: stopping the rig must not be able to deadlock
        # against a reading arriving.
        self._on_trip(reason)

    # -- the latch ----------------------------------------------------------
    def trip(self, reason: str, pressures: list[float] | None = None) -> dict[str, Any]:
        """Latch a trip that something other than a pressure breach caused."""
        with self._lock:
            if not self.state.tripped:
                self.state.tripped = True
                self.state.at = now_iso()
                self.state.reason = reason
                if pressures is not None:
                    self.state.readings = {str(i): float(p) for i, p in enumerate(pressures)}
            return self.state.describe()

    def reset(self, pressures: list[float]) -> dict[str, Any]:
        """Clear the latch, but only once everything reads safe.

        Resetting while a channel is still over its limit would hand back a rig
        that is about to trip again, which teaches the operator that the latch
        is noise.
        """
        with self._lock:
            if not self.state.tripped:
                return self.state.describe()
            unsafe = [
                f"channel {index} is at {pressures[int(index)]:.1f} mbar, over {limit:.0f}"
                for index, limit in self.state.limits.items()
                if 0 <= int(index) < len(pressures) and pressures[int(index)] > limit
            ]
            if unsafe:
                raise SafetyUnsafe(
                    "cannot reset while the rig is still over its limits: " + "; ".join(unsafe)
                )
            self.state = SafetyState(armed=False, limits=dict(self.state.limits))
            return self.state.describe()

    def describe(self) -> dict[str, Any]:
        with self._lock:
            return self.state.describe()


class SafetyTripped(Exception):
    """Something was asked for while the safety latch is set."""


class SafetyUnsafe(Exception):
    """A reset was asked for while the rig still reads unsafe."""
