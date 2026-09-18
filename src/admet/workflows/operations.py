"""What the system can be asked to do, and what has to be true first.

This is the exposed surface. A terminal, an MCP client and any future interface
call operations from here; none of them reaches an engine directly. An engine
action is a hardware primitive with no opinion about whether now is a sensible
moment -- an operation is that opinion.

Each operation carries its own parameters and its own requirements. A
requirement that is not met refuses the operation and says which one and what to
do about it. There is no override: a guard that can be waved through is a guard
nobody trusts.

To add an experiment: write a builder in protocols.py, then add an Operation here
naming it. The parameters are declared on the operation, so nothing else changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pathlib import Path

from admet.core.engine import Param, ParamKind, ParamOption
from admet.engines.acquisition.fluidics.config import (
    GRAVIMETRIC_REPLICATES,
    STABILITY_DURATION_S,
    STABILITY_TIMEOUT_S,
    STABILITY_TOLERANCE_UL_MIN,
)
from admet.workflows import protocols


class Refused(Exception):
    """An operation was asked for at a moment when it would be wrong."""


class Runner(Protocol):
    """What an operation needs from core. Core implements this; core is not imported."""

    def engine_action(self, engine_id: str, action: str, settings: dict[str, Any]) -> Any: ...

    def run_steps(self, steps: list[Any], *, tick_s: float) -> Any: ...

    def state(self) -> dict[str, Any]: ...

    def mark(self, name: str, value: Any) -> None: ...

    def add_analysis_source(self, path: Path, *, engine: str, sample_id: str) -> dict[str, Any]: ...

    def analysis_sources(self) -> list[dict[str, Any]]: ...

    def analyze(self, *, engine: str, cache: str) -> dict[str, Any]: ...


# ---- requirements ----------------------------------------------------------
#
# Each one answers: is this true, and if not, what should the operator do?

REQUIREMENTS: dict[str, tuple[Callable[[dict[str, Any]], bool], str]] = {
    "project": (
        lambda state: state["project"],
        "no project is open; create or open one first",
    ),
    "fluidics": (
        lambda state: state["fluidics"],
        "the fluidics are not connected; run connect first",
    ),
    "camera": (
        lambda state: state["camera"],
        "no camera is connected; run connect_camera first",
    ),
    "corrections": (
        lambda state: state["corrections"],
        "correction factors have not been applied, so flows would not be true flows; "
        "run apply_corrections first",
    ),
    "idle": (
        lambda state: not state["running"],
        "a protocol is already running; stop it or wait for it to finish",
    ),
    "running": (
        lambda state: state["running"],
        "no protocol is running",
    ),
    "sources": (
        lambda state: state["sources"] > 0,
        "the project holds nothing to analyse; add a video or an image directory first",
    ),
}


@dataclass(frozen=True)
class Operation:
    id: str
    label: str
    description: str
    params: tuple[Param, ...] = ()
    requires: tuple[str, ...] = ()
    # True when this hands a step list to the pipeline and returns before it has
    # finished. A pipeline built from operations has to know which ones to wait on.
    starts_protocol: bool = False
    # The engine actions this drives, declared so the two layers can be read
    # against each other. A test checks that each one exists.
    uses: tuple[str, ...] = ()
    # The protocol this runs, if it runs one.
    protocol: str = ""
    run: Callable[[Runner, dict[str, Any]], dict[str, Any]] = field(repr=False, default=None)  # type: ignore[assignment]

    def check(self, state: dict[str, Any]) -> None:
        """Refuse before anything happens, naming the first thing that is wrong."""
        for name in self.requires:
            passes, remedy = REQUIREMENTS[name]
            if not passes(state):
                raise Refused(f"{self.id}: {remedy}")


def _number(name: str, label: str, default: float, *, minimum: float = 0.0, unit: str = "") -> Param:
    return Param(
        name,
        label,
        ParamKind.FLOAT,
        default=default,
        minimum=minimum,
        description=f"{label} ({unit})" if unit else label,
    )


def _whole(name: str, label: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> Param:
    return Param(name, label, ParamKind.INTEGER, default=default, minimum=minimum, maximum=maximum)


TICK = _number("tick_s", "Pipeline tick", 0.2, minimum=0.001, unit="s")


# ---- setup -----------------------------------------------------------------


def _connect(runner: Runner, settings: dict[str, Any]) -> dict[str, Any]:
    result = runner.engine_action(
        "acquisition",
        "connect_fluidics",
        {"simulated": settings.get("simulated", False), "start_polling": True},
    )
    runner.mark("corrections", False)
    return result.metadata


def _disconnect(runner: Runner, _settings: dict[str, Any]) -> dict[str, Any]:
    runner.mark("corrections", False)
    return runner.engine_action("acquisition", "disconnect_fluidics", {}).metadata


def _connect_camera(runner: Runner, _settings: dict[str, Any]) -> dict[str, Any]:
    return runner.engine_action("acquisition", "connect_camera", {}).metadata


def _apply_corrections(runner: Runner, settings: dict[str, Any]) -> dict[str, Any]:
    result = runner.engine_action("acquisition", "apply_corrections", settings)
    runner.mark("corrections", True)
    return result.metadata


def _status(runner: Runner, _settings: dict[str, Any]) -> dict[str, Any]:
    return {**runner.engine_action("acquisition", "camera_status", {}).metadata, **runner.state()}


# ---- channels --------------------------------------------------------------


def _set_flow(runner: Runner, settings: dict[str, Any]) -> dict[str, Any]:
    return runner.engine_action("acquisition", "set_channel_flow", settings).metadata


def _stop_channel(runner: Runner, settings: dict[str, Any]) -> dict[str, Any]:
    return runner.engine_action("acquisition", "stop_channel", settings).metadata


# ---- protocols -------------------------------------------------------------


def _protocol(name: str) -> Callable[[Runner, dict[str, Any]], dict[str, Any]]:
    """Run a named protocol, built here and handed to the engine as steps."""

    def start(runner: Runner, settings: dict[str, Any]) -> dict[str, Any]:
        steps = protocols.build_protocol(name, settings)
        result = runner.run_steps(steps, tick_s=float(settings.get("tick_s", 0.2)))
        return {**result.metadata, "protocol": name, "steps": len(steps)}

    return start


def _pipeline_control(action: str) -> Callable[[Runner, dict[str, Any]], dict[str, Any]]:
    def control(runner: Runner, _settings: dict[str, Any]) -> dict[str, Any]:
        return runner.engine_action("acquisition", action, {}).metadata

    return control


# ---- recording -------------------------------------------------------------


def _start_recording(runner: Runner, settings: dict[str, Any]) -> dict[str, Any]:
    return runner.engine_action("acquisition", "start_recording", settings).metadata


def _stop_recording(runner: Runner, _settings: dict[str, Any]) -> dict[str, Any]:
    return runner.engine_action("acquisition", "stop_recording", {}).metadata


# ---- analysis --------------------------------------------------------------


def _add_source(runner: Runner, settings: dict[str, Any]) -> dict[str, Any]:
    from admet.workflows.analyze_runner import infer_engine

    path = Path(str(settings["path"])).expanduser()
    if not path.exists():
        raise Refused(f"add_source: {path} does not exist")
    engine_id = str(settings.get("engine") or "") or infer_engine(path)
    return runner.add_analysis_source(path, engine=engine_id, sample_id=str(settings.get("sample_id") or ""))


def _analyze(runner: Runner, settings: dict[str, Any]) -> dict[str, Any]:
    return runner.analyze(
        engine=str(settings.get("engine") or ""),
        cache=str(settings.get("cache") or "use"),
    )


def _sources(runner: Runner, _settings: dict[str, Any]) -> dict[str, Any]:
    return {"sources": runner.analysis_sources()}


OPERATIONS: tuple[Operation, ...] = (
    Operation(
        "connect",
        "Connect fluidics",
        "Connect the pressure controller and start reading from it.",
        params=(
            Param(
                "simulated",
                "Simulated hardware",
                ParamKind.BOOLEAN,
                default=False,
                description="Drive the SDK's simulator instead of an instrument",
            ),
        ),
        uses=("connect_fluidics",),
        run=_connect,
    ),
    Operation("disconnect", "Disconnect fluidics", "Release the instrument.",
              requires=("fluidics",), uses=("disconnect_fluidics",), run=_disconnect),
    Operation("connect_camera", "Connect camera", "Open the camera.", uses=("connect_camera",), run=_connect_camera),
    Operation(
        "apply_corrections",
        "Apply correction factors",
        "Send each channel its liquid calibration. Flows are not true flows until this is done.",
        params=tuple(
            param
            for prefix in ("oil_l", "cells_m", "beads_m")
            for param in (
                Param(f"{prefix}_calibration", f"{prefix} calibration", ParamKind.TEXT, default="H2O"),
                Param(f"{prefix}_scale", f"{prefix} scale", ParamKind.FLOAT, default=1.0),
                Param(f"{prefix}_offset", f"{prefix} offset", ParamKind.FLOAT, default=0.0),
                Param(f"{prefix}_quadratic", f"{prefix} quadratic", ParamKind.FLOAT, default=0.0),
            )
        ),
        requires=("fluidics",),
        uses=("apply_corrections",),
        run=_apply_corrections,
    ),
    Operation("status", "Status", "What the instrument and the session are doing.", uses=("camera_status",), run=_status),
    Operation(
        "set_flow",
        "Set a channel's flow",
        "Hold one channel at a flow rate.",
        params=(
            _whole("channel_index", "Channel", 0, minimum=0),
            _number("channel_flow_ul_min", "Flow", 0.0, unit="uL/min"),
        ),
        requires=("fluidics", "corrections", "idle"),
        uses=("set_channel_flow",),
        run=_set_flow,
    ),
    Operation(
        "stop_channel",
        "Stop a channel",
        "Take one channel to zero.",
        params=(_whole("channel_index", "Channel", 0, minimum=0),),
        requires=("fluidics",),
        uses=("stop_channel",),
        run=_stop_channel,
    ),
    Operation(
        "prime",
        "Priming protocol",
        "Wet every line, confirming each channel in turn.",
        params=(
            _number("prime_oil_volume_ul", "Oil volume", 40.0, minimum=0.1, unit="uL"),
            _number("prime_aqueous_volume_ul", "Aqueous volume", 5.0, minimum=0.1, unit="uL"),
            TICK,
        ),
        requires=("fluidics", "corrections", "idle"),
        protocol="Priming",
        starts_protocol=True,
        run=_protocol("Priming"),
    ),
    Operation(
        "wash",
        "Wash protocol",
        "Flush the chip with IPA, then hold pressure.",
        params=(
            _number("wash_oil_flow_ul_min", "Oil flow", 250.0, unit="uL/min"),
            _number("wash_aqueous_total_flow_ul_min", "Total aqueous flow", 160.0, unit="uL/min"),
            _number("wash_oil_volume_ul", "Oil volume", 500.0, minimum=0.1, unit="uL"),
            _number("wash_pressure_mbar", "Pressure", 2000.0, unit="mbar"),
            _number("wash_pressure_duration_s", "Pressure duration", 120.0, unit="s"),
            TICK,
        ),
        requires=("fluidics", "idle"),
        protocol="Wash",
        starts_protocol=True,
        run=_protocol("Wash"),
    ),
    Operation(
        "characterise",
        "Flow check",
        "Sweep the working flows to measure what the plumbing and chip cost.",
        params=(
            _number("run_oil_flow_ul_min", "Oil flow", 300.0, unit="uL/min"),
            _number("run_aqueous_total_flow_ul_min", "Total aqueous flow", 80.0, unit="uL/min"),
            _number("sweep_tolerance_ul_min", "Settle tolerance", STABILITY_TOLERANCE_UL_MIN,
                    minimum=0.1, unit="uL/min"),
            _number("sweep_window_s", "Settle window", STABILITY_DURATION_S, minimum=0.5, unit="s"),
            _number("sweep_timeout_s", "Settle timeout", STABILITY_TIMEOUT_S, minimum=1.0, unit="s"),
            TICK,
        ),
        requires=("fluidics", "corrections", "idle"),
        protocol="Characterise",
        starts_protocol=True,
        run=_protocol("Characterise"),
    ),
    Operation(
        "gravimetry",
        "Dispense check",
        "Dispense a weighed volume from every channel, gated on the operator.",
        params=(
            _number("gravimetric_target_ul", "Target volume", 100.0, minimum=0.1, unit="uL"),
            _number("gravimetric_flow_ul_min", "Dispense flow", 250.0, minimum=0.1, unit="uL/min"),
            _whole("gravimetric_replicates", "Replicates", GRAVIMETRIC_REPLICATES,
                   maximum=GRAVIMETRIC_REPLICATES),
            TICK,
        ),
        requires=("fluidics", "corrections", "idle"),
        protocol="Gravimetry",
        starts_protocol=True,
        run=_protocol("Gravimetry"),
    ),
    Operation(
        "dropseq",
        "Drop-Seq run",
        "Generate droplets for each set and replicate.",
        params=(
            _whole("set_count", "Sets", 1),
            _whole("replicate_count", "Replicates", 1),
            _number("run_volume_ul", "Oil volume per run", 150.0, minimum=0.1, unit="uL"),
            _number("run_oil_flow_ul_min", "Oil flow", 300.0, unit="uL/min"),
            _number("run_aqueous_total_flow_ul_min", "Total aqueous flow", 80.0, unit="uL/min"),
            TICK,
        ),
        requires=("project", "fluidics", "corrections", "idle"),
        protocol="Drop-Seq",
        starts_protocol=True,
        run=_protocol("Drop-Seq"),
    ),
    Operation("pause", "Pause protocol", "Hold the protocol and zero the channels.",
              requires=("running",), uses=("pause_protocol",), run=_pipeline_control("pause_protocol")),
    Operation("resume", "Resume protocol", "Carry on from a pause.",
              requires=("running",), uses=("resume_protocol",), run=_pipeline_control("resume_protocol")),
    Operation("stop", "Stop protocol", "End the protocol and release the channels.",
              requires=("running",), uses=("stop_protocol",), run=_pipeline_control("stop_protocol")),
    Operation("confirm", "Confirm step", "Answer a step that is waiting for the operator.",
              requires=("running",), uses=("confirm_protocol",), run=_pipeline_control("confirm_protocol")),
    Operation("skip", "Skip step", "Abandon the waiting step and move on.",
              requires=("running",), uses=("skip_protocol",), run=_pipeline_control("skip_protocol")),
    Operation(
        "start_recording",
        "Start recording",
        "Record video and fluidics together into the open project.",
        params=(
            Param("recording_label", "Label", ParamKind.TEXT, default="recording"),
            _whole("recording_max_frames", "Frame limit", 100_000, minimum=0),
            _number("recording_max_seconds", "Time limit", 0.0, unit="s"),
        ),
        requires=("project", "fluidics", "camera"),
        uses=("start_recording",),
        run=_start_recording,
    ),
    Operation("stop_recording", "Stop recording", "End the recording and file it in the project.",
              requires=("project",), uses=("stop_recording",), run=_stop_recording),
    Operation(
        "add_source",
        "Add something to analyse",
        "Register a video or an image directory with the project so it can be analysed.",
        params=(
            Param("path", "Path", ParamKind.PATH, default="", required=True,
                  description="A video file, or a directory of images"),
            Param("engine", "Engine", ParamKind.CHOICE, default="",
                  options=(ParamOption("", "infer from the file"),
                           ParamOption("opencv", "opencv"),
                           ParamOption("cellpose", "cellpose")),
                  description="Which analysis engine; inferred from the file when left empty"),
            Param("sample_id", "Sample", ParamKind.TEXT, default="",
                  description="Names this source in the results; defaults to the file name"),
        ),
        requires=("project",),
        run=_add_source,
    ),
    Operation(
        "sources",
        "What is there to analyse",
        "Everything the project holds that analysis can be run over.",
        requires=("project",),
        run=_sources,
    ),
    Operation(
        "analyze",
        "Analyse the project",
        "Run analysis over everything registered, writing results into the project.",
        params=(
            Param("engine", "Engine", ParamKind.CHOICE, default="",
                  options=(ParamOption("", "as each source was registered"),
                           ParamOption("opencv", "opencv"),
                           ParamOption("cellpose", "cellpose")),
                  description="Override which engine analyses every source"),
            Param("cache", "Cache", ParamKind.CHOICE, default="use",
                  options=(ParamOption("use", "reuse a previous result"),
                           ParamOption("refresh", "recompute and replace"),
                           ParamOption("ignore", "recompute without storing")),
                  description="What to do about work already done"),
        ),
        requires=("project", "sources"),
        run=_analyze,
    ),
)

BY_ID = {operation.id: operation for operation in OPERATIONS}


def operation(operation_id: str) -> Operation:
    known = ", ".join(sorted(BY_ID))
    if operation_id not in BY_ID:
        raise LookupError(f"unknown operation {operation_id!r}; this build offers: {known}")
    return BY_ID[operation_id]
