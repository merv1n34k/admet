"""The oil-path capacity validation.

One specific recipe, not a general experiment builder. It asks a single
question: how much oil can this path carry before the controller runs out of
pressure -- and it answers it without going looking for the ceiling.

The rig has already been seen to saturate: about 1999.7 mbar for only
236.4 uL/min against a 300 uL/min target, and later about 128 uL/min against
250. Reproducing that tells nobody anything new and is hard on the hardware, so
this stops at a limit well under the controller's 2000 mbar and reports that it
stopped rather than pushing on.

What comes out is a classification, not a verdict on whether the thread ended:

    pass              every target settled, flow held at or above the fraction
                      asked for, and pressure stayed under the limit
    capacity_limited  pressure reached the boundary before the flow did
    unstable          pressure had room, but the flow never settled
    invalid           the run cannot be read: no data, no mapping confirmation,
                      a disconnect, a recording that did not happen
"""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from admet.core.clock import now_iso
from admet.engines.acquisition.pipeline import ProtocolStep

# Which channel carries the oil. Confirmed by the operator against what the
# instrument reports before anything flows, never assumed from this alone.
OIL_CHANNEL = 0

# The controller tops out at 2000 mbar. Everything here is meant to stay below
# that with room to spare, so the ceiling is never the thing being tested. The
# operation declares this as its parameter's maximum, which is where it is
# enforced -- a model reading the schema is told before it asks.
MAX_TRIP_MBAR = 1900.0

CONFIGURATIONS = ("bypass_chip", "with_chip")

# The first target a run is allowed to open with. The bench plan starts at 50,
# and opening higher is how a restricted line gets a pressure spike before
# anyone has seen a single measurement from it.
FIRST_TARGET_CEILING_UL_MIN = 50.0

PASS = "pass"
CAPACITY_LIMITED = "capacity_limited"
UNSTABLE = "unstable"
INVALID = "invalid"


def confirmation_message(configuration: str, channel: dict[str, Any]) -> str:
    """What the operator is asked before any oil moves.

    It quotes what the instrument says about the channel rather than what the
    configuration claims, because the mistake being guarded against is exactly
    that the two disagree.
    """
    detected = channel.get("detected") or {}
    return (
        f"About to run the oil capacity validation in {configuration}.\n"
        f"Channel {channel.get('index')} is configured as {channel.get('label')!r}; "
        f"the instrument reports sensor {detected.get('sensor_index')} "
        f"({detected.get('sensor_type')}, up to {detected.get('sensor_max_ul_min')} uL/min) "
        f"on pressure channel {detected.get('pressure_index')} "
        f"(up to {detected.get('pressure_max_mbar')} mbar).\n"
        f"Confirm that this channel is physically the oil line, that its outlet "
        f"runs to waste, and that the stated configuration is what is plumbed in."
    )


def check_targets(targets: list[float]) -> list[float]:
    """The targets as given, or a refusal saying what is wrong with them.

    Deliberately not sorted or deduplicated. Quietly rewriting what was asked
    for means a run that does not match its own request, and [300, 50] almost
    certainly means somebody made a mistake rather than that they wanted them
    reordered.
    """
    if not targets:
        raise ValueError("validate_oil_capacity needs at least one flow target")
    if any(target <= 0 for target in targets):
        raise ValueError("every flow target must be above zero")
    repeated = [t for t in targets if targets.count(t) > 1]
    if repeated:
        raise ValueError(
            f"flow targets repeat {', '.join(f'{t:g}' for t in sorted(set(repeated)))} uL/min; "
            f"give each one once"
        )
    if targets != sorted(targets):
        raise ValueError(
            f"flow targets must climb: {', '.join(f'{t:g}' for t in targets)} does not. "
            f"A run works upwards so it stops at the first target the path cannot carry"
        )
    if targets[0] > FIRST_TARGET_CEILING_UL_MIN:
        raise ValueError(
            f"the first target is {targets[0]:g} uL/min, above the "
            f"{FIRST_TARGET_CEILING_UL_MIN:g} this run may open with; start lower and "
            f"work up once the path is known"
        )
    return list(targets)


def build_steps(settings: dict[str, Any], channel: dict[str, Any]) -> tuple[list[ProtocolStep], list[dict[str, Any]]]:
    """The steps, and what each one is for.

    Returned together because the summary has to know which step was sampling
    which target, and reading that back out of step names later would be
    guessing at its own protocol.
    """
    targets = check_targets([float(target) for target in settings["flow_targets_ul_min"]])

    tolerance = float(settings["settle_tolerance_ul_min"])
    window_s = float(settings["settle_window_s"])
    timeout_s = float(settings["settle_timeout_s"])
    sample_s = float(settings["sample_window_s"])

    steps: list[ProtocolStep] = [
        # A gate, not a wait: the engine holds on confirm_message before it
        # applies any setpoint, so nothing flows until this is answered. The
        # trigger behind it is instant because the answering is the whole step.
        ProtocolStep(
            name="confirm the oil line",
            sensor_setpoints={},
            trigger_type="time",
            trigger_params={"duration_s": 0.0},
            confirm_message=confirmation_message(settings["configuration"], channel),
        )
    ]
    plan: list[dict[str, Any]] = [{"role": "confirm", "target_ul_min": None}]

    # A lead-in at half the first target. Going from nothing straight to a
    # target is how a line that is already restricted gets a pressure spike
    # before anything has had a chance to watch it.
    lead_in = round(targets[0] / 2.0, 3)
    if lead_in > 0:
        steps.append(
            ProtocolStep(
                name=f"lead-in {lead_in} uL/min",
                sensor_setpoints={OIL_CHANNEL: lead_in},
                trigger_type="time",
                trigger_params={"duration_s": window_s},
            )
        )
        plan.append({"role": "lead_in", "target_ul_min": lead_in})

    for target in targets:
        steps.append(
            ProtocolStep(
                name=f"settle {target} uL/min",
                sensor_setpoints={OIL_CHANNEL: target},
                trigger_type="stability",
                trigger_params={
                    "sensor_index": OIL_CHANNEL,
                    "tolerance_ul_min": tolerance,
                    "window_s": window_s,
                    "timeout_s": timeout_s,
                },
            )
        )
        plan.append({"role": "settle", "target_ul_min": target})
        steps.append(
            ProtocolStep(
                name=f"sample {target} uL/min",
                sensor_setpoints={OIL_CHANNEL: target},
                trigger_type="time",
                trigger_params={"duration_s": sample_s},
                on_complete="zero" if target == targets[-1] else "hold",
            )
        )
        plan.append({"role": "sample", "target_ul_min": target})

    return steps, plan


# ---- reading the run back ---------------------------------------------------


def read_rows(csv_path: str | Path) -> list[dict[str, float]]:
    """The recorded fluidics log, as numbers."""
    rows: list[dict[str, float]] = []
    try:
        with Path(csv_path).open() as handle:
            for raw in csv.DictReader(handle):
                try:
                    rows.append(
                        {
                            "elapsed_s": float(raw["elapsed_s"]),
                            "pressure_mbar": float(raw[f"pressure_{OIL_CHANNEL}_mbar"]),
                            "flow_ul_min": float(raw[f"flow_{OIL_CHANNEL}_ul_min"]),
                        }
                    )
                except (KeyError, TypeError, ValueError):
                    continue  # a row being written as this is read is not corruption
    except OSError:
        return []
    return rows


def _spread(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "std": None, "min": None, "max": None}
    return {
        "mean": mean(values),
        "std": pstdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
    }


def summarise_target(
    entry: dict[str, Any],
    rows: list[dict[str, float]],
    *,
    minimum_flow_fraction: float,
) -> dict[str, Any]:
    """One target: what was asked for, what happened, and whether it held."""
    window = [
        row
        for row in rows
        if entry["from_elapsed_s"] is not None
        and entry["from_elapsed_s"] <= row["elapsed_s"] <= entry["to_elapsed_s"]
    ]
    flows = [row["flow_ul_min"] for row in window]
    pressures = [row["pressure_mbar"] for row in window]
    flow = _spread(flows)
    pressure = _spread(pressures)
    requested = float(entry["target_ul_min"])
    fraction = (flow["mean"] / requested) if flow["mean"] is not None and requested else None

    return {
        "requested_ul_min": requested,
        "settled": entry["settled"],
        "settle_outcome": entry["settle_outcome"],
        "flow_ul_min": flow,
        "pressure_mbar": pressure,
        "samples": len(window),
        "sample_duration_s": (
            round(window[-1]["elapsed_s"] - window[0]["elapsed_s"], 3) if window else 0.0
        ),
        "flow_fraction": fraction,
        "held_the_flow": fraction is not None and fraction >= minimum_flow_fraction,
        "from_elapsed_s": entry["from_elapsed_s"],
        "to_elapsed_s": entry["to_elapsed_s"],
    }


def classify(
    targets: list[dict[str, Any]],
    *,
    tripped: bool,
    trip_mbar: float,
    reason: str = "",
) -> tuple[str, str]:
    """What the run showed, and why. Never "the thread ended, so it passed"."""
    if reason:
        return INVALID, reason
    if not targets:
        return INVALID, "no targets were measured"

    # A trip is the answer to the question this run asks, so it is read before
    # anything else. It also explains its own missing data: the run stopped, so
    # the targets above it were never reached. Calling that invalid would file
    # the clearest possible result as a broken run.
    if tripped:
        reached = [t for t in targets if t["samples"]]
        unreached = [t for t in targets if not t["samples"]]
        note = ""
        if reached:
            note = f"; the highest target reached was {max(t['requested_ul_min'] for t in reached):g} uL/min"
        if unreached:
            names = ", ".join(f"{t['requested_ul_min']:g}" for t in unreached)
            note += f"; {names} uL/min was never reached"
        return CAPACITY_LIMITED, f"the pressure limit was reached{note}"

    without_data = [t for t in targets if not t["samples"]]
    if without_data:
        missing = ", ".join(f"{t['requested_ul_min']:g}" for t in without_data)
        return INVALID, f"no recorded rows for {missing} uL/min"

    near_limit = [
        t
        for t in targets
        if not t["settled"] and (t["pressure_mbar"]["max"] or 0.0) >= trip_mbar * 0.95
    ]
    if near_limit:
        worst = max(near_limit, key=lambda t: t["pressure_mbar"]["max"])
        return CAPACITY_LIMITED, (
            f"{worst['requested_ul_min']:g} uL/min did not settle and reached "
            f"{worst['pressure_mbar']['max']:.0f} mbar, at the limit"
        )

    unsettled = [t for t in targets if not t["settled"]]
    if unsettled:
        names = ", ".join(f"{t['requested_ul_min']:g}" for t in unsettled)
        return UNSTABLE, f"pressure had room, but {names} uL/min never settled"

    short = [t for t in targets if not t["held_the_flow"]]
    if short:
        worst = min(short, key=lambda t: t["flow_fraction"] or 0.0)
        return CAPACITY_LIMITED, (
            f"{worst['requested_ul_min']:g} uL/min settled at only "
            f"{(worst['flow_fraction'] or 0) * 100:.0f}% of what was asked for"
        )
    return PASS, "every target settled and held its flow below the pressure limit"


class ValidationRun(threading.Thread):
    """Watches one validation to the end, then writes down what it showed.

    A thread rather than a blocking call, because the point of the whole
    arrangement is that observe can be polled and the monitor watched while the
    run is going. Blocking here would make the run invisible for its duration.
    """

    def __init__(self, admet: Any, settings: dict[str, Any], plan: list[dict[str, Any]], *, check_id: str):
        super().__init__(daemon=True, name="ValidationRun")
        self._admet = admet
        self._settings = dict(settings)
        self._plan = plan
        self._lock = threading.Lock()
        self._state = {
            "active": True,
            "id": check_id,
            "state": "running",
            "configuration": settings["configuration"],
            "current_target_ul_min": None,
            "artifacts": {},
            "error": "",
            "classification": None,
        }
        self._windows: dict[int, dict[str, Any]] = {}
        self._confirmed = False

    # -- what observe and the monitor read ----------------------------------
    def describe(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)

    def note_artifact(self, name: str, path: str) -> None:
        with self._lock:
            self._state["artifacts"] = {**self._state["artifacts"], name: path}

    # -- the watch ----------------------------------------------------------
    def run(self) -> None:
        reason = ""
        try:
            reason = self._follow()
        except Exception as exc:  # the run is over either way; say why
            reason = f"the supervisor failed: {exc}"
        finally:
            try:
                self._finish(reason)
            except Exception as exc:
                self._set(state="failed", active=False, error=f"could not write the summary: {exc}")

    def _follow(self) -> str:
        """Track the protocol to its end, recording where each step sat."""
        sequence = 0
        deadline = time.monotonic() + self._total_allowance_s()
        while time.monotonic() < deadline:
            for event in self._admet.protocol_events(after_sequence=sequence, limit=500):
                sequence = max(sequence, event["sequence"])
                self._record(event)
                if event["step_name"] == "" and event["outcome"] != "running":
                    # A terminal event describes the run rather than a step.
                    if event["outcome"] == "completed":
                        return ""
                    # A trip cancels the protocol, so the cancellation is the
                    # trip's own consequence. Reporting it as a reason would
                    # override the finding with its own side effect and file
                    # the clearest result as an unreadable run.
                    if self._admet.safety_state()["tripped"]:
                        return ""
                    return f"the run {event['outcome']}"
            if self._admet.safety_state()["tripped"]:
                return ""  # a trip is a finding, and the summary says so
            if not self._admet.state()["running"]:
                return ""
            time.sleep(0.1)
        return "the run did not finish within the time it was given"

    def _record(self, event: dict[str, Any]) -> None:
        index = event["step_index"]
        if index is None or not 0 <= index < len(self._plan):
            return
        entry = self._plan[index]
        if entry["role"] == "confirm" and event["outcome"] == "completed":
            self._confirmed = True
        window = self._windows.setdefault(
            index, {"from_monotonic": event["monotonic"], "to_monotonic": event["monotonic"],
                    "outcome": event["outcome"]}
        )
        window["to_monotonic"] = event["monotonic"]
        if event["outcome"] != "running":
            window["outcome"] = event["outcome"]
        if entry["role"] == "sample":
            self._set(current_target_ul_min=entry["target_ul_min"])

    def _total_allowance_s(self) -> float:
        """Long enough for the run, and not a second more than that.

        Bounded so a protocol that never reports again leaves a supervisor that
        gives up and records why, rather than one that waits for ever.
        """
        per_target = float(self._settings["settle_timeout_s"]) + float(
            self._settings["sample_window_s"]
        )
        targets = len(self._settings["flow_targets_ul_min"])
        return 120.0 + per_target * max(1, targets) * 1.5

    # -- what it leaves behind ----------------------------------------------
    def _finish(self, reason: str) -> None:
        self._set(state="finishing")
        stopped = {}
        try:
            if self._admet.state()["running"]:
                self._admet.do("stop_protocol")
        except Exception:
            pass
        try:
            # In a finally in spirit: the recording is closed whatever happened,
            # because an open recording is a file nobody can read.
            stopped = self._admet.do("stop_recording")
        except Exception as exc:
            reason = reason or f"the recording could not be closed: {exc}"

        csv_path = str(stopped.get("csv_path") or self.describe()["artifacts"].get("fluidics_csv", ""))
        if csv_path:
            self.note_artifact("fluidics_csv", csv_path)
        if not self._confirmed:
            reason = reason or "the channel mapping was never confirmed"

        summary = self._summarise(csv_path, reason)
        path = self._admet.save_validation(summary, check_id=self.describe()["id"])
        self.note_artifact("summary", str(path))
        self._set(
            state="complete",
            active=False,
            classification=summary["classification"],
            error=summary["classification_reason"] if summary["classification"] == INVALID else "",
        )

    def _summarise(self, csv_path: str, reason: str) -> dict[str, Any]:
        rows = read_rows(csv_path) if csv_path else []
        origin = self._admet.polling_started_monotonic()
        safety = self._admet.safety_state()

        measured = []
        for index, entry in enumerate(self._plan):
            if entry["role"] != "sample":
                continue
            window = self._windows.get(index)
            settle = self._windows.get(index - 1, {})
            outcome = settle.get("outcome", "")
            measured.append(
                summarise_target(
                    {
                        "target_ul_min": entry["target_ul_min"],
                        "settled": outcome == "completed",
                        "settle_outcome": outcome or "never reached",
                        "from_elapsed_s": (window["from_monotonic"] - origin) if window else None,
                        "to_elapsed_s": (window["to_monotonic"] - origin) if window else None,
                    },
                    rows,
                    minimum_flow_fraction=float(self._settings["minimum_flow_fraction"]),
                )
            )

        classification, why = classify(
            measured,
            tripped=bool(safety["tripped"]),
            trip_mbar=float(self._settings["oil_pressure_trip_mbar"]),
            reason=reason,
        )
        return {
            "kind": "oil_capacity",
            "id": self.describe()["id"],
            "at": now_iso(),
            "configuration": self._settings["configuration"],
            "mapping_confirmed": self._confirmed,
            "settings": self._settings,
            "classification": classification,
            "classification_reason": why,
            "safety": safety,
            "targets": measured,
            "artifacts": self.describe()["artifacts"],
            "context": self._admet.run_context(),
        }
