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

from admet.core.engine import READ, START, WRITE, Param, ParamKind, ParamOption
from admet.engines.acquisition.fluidics.config import (
    GRAVIMETRIC_REPLICATES,
    STABILITY_DURATION_S,
    STABILITY_TIMEOUT_S,
    STABILITY_TOLERANCE_UL_MIN,
)
from admet.engines.acquisition.pipeline import ON_COMPLETE, ProtocolStep
from admet.engines.acquisition.triggers import TRIGGER_TYPES, trigger_params
from admet.workflows import protocols


class Refused(Exception):
    """An operation was asked for at a moment when it would be wrong."""


class Runner(Protocol):
    """What an operation needs from core. Core implements this; core is not imported."""

    def engine_action(self, engine_id: str, action: str, settings: dict[str, Any]) -> Any: ...

    def run_steps(self, steps: list[Any], *, tick_s: float) -> Any: ...

    def state(self) -> dict[str, Any]: ...

    def mark(self, name: str, value: Any) -> None: ...

    def create_project(self, path: str, project_id: str) -> Any: ...

    def open_project(self, path: str) -> Any: ...

    def describe_project(self) -> dict[str, Any]: ...

    def discover_projects(self, root: str) -> list[dict[str, Any]]: ...

    def add_analysis_source(self, path: Path, *, engine: str, sample_id: str) -> dict[str, Any]: ...

    def analysis_sources(self) -> list[dict[str, Any]]: ...

    def analyze(self, *, engine: str, cache: str) -> dict[str, Any]: ...


# ---- requirements ----------------------------------------------------------
#
# Each one answers: is this true, and if not, what should the operator do?

REQUIREMENTS: dict[str, tuple[Callable[[dict[str, Any]], bool], str]] = {
    "project": (
        lambda state: state["project"],
        "no project is open; run create_project or open_project first",
    ),
    "fluidics": (
        lambda state: state["fluidics"],
        "the fluidics are not connected; run connect_fluidics first",
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


# What an operation is about. Some belong to the instrument, some to the analysis
# of what it produced, and some to neither -- the session both of them work in.
CONTROL = "control"
ANALYZE = "analyze"
GENERAL = "general"
TARGETS = (GENERAL, CONTROL, ANALYZE)


@dataclass(frozen=True)
class Operation:
    id: str
    label: str
    description: str
    target: str = CONTROL
    kind: str = WRITE
    # Settings too structured for a Param -- a step list, say. The JSON schema is
    # declared here so every surface can describe them without a special case.
    raw: dict[str, Any] = field(default_factory=dict)
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
    return {**runner.engine_action("acquisition", "read_status", {}).metadata, **runner.state()}


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


# ---- the session -----------------------------------------------------------


def _project_create(runner: Runner, settings: dict[str, Any]) -> dict[str, Any]:
    runner.create_project(str(settings["path"]), str(settings.get("project_id") or ""))
    return runner.describe_project()


def _project_open(runner: Runner, settings: dict[str, Any]) -> dict[str, Any]:
    runner.open_project(str(settings["path"]))
    return runner.describe_project()


def _project_status(runner: Runner, _settings: dict[str, Any]) -> dict[str, Any]:
    return runner.describe_project()


def _project_list(runner: Runner, settings: dict[str, Any]) -> dict[str, Any]:
    return {"projects": runner.discover_projects(str(settings["root"]))}


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


def _trigger_help() -> str:
    """What to pass each trigger, read from the triggers themselves."""
    lines = []
    for name in TRIGGER_TYPES:
        taken = trigger_params(name)
        required = ", ".join(n for n, needed in taken.items() if needed) or "nothing"
        optional = ", ".join(n for n, needed in taken.items() if not needed)
        lines.append(f"{name}: {required}" + (f" (optional: {optional})" if optional else ""))
    return "; ".join(lines)


_TRIGGER_HELP = _trigger_help()


# The step vocabulary, declared once. A step holds channels at setpoints until
# its trigger fires; this is the whole of what a protocol is made of.
STEP_LIST_SCHEMA = {
    "type": "array",
    "description": "The steps, in order.",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "What this step is called"},
            "sensor_setpoints": {
                "type": "object",
                "description": "Channel index -> flow in uL/min",
            },
            "pressure_setpoints": {
                "type": "object",
                "description": "Channel index -> pressure in mbar, for open-loop steps",
            },
            "trigger_type": {
                "type": "string",
                "enum": list(TRIGGER_TYPES),
                "description": "What ends the step",
            },
            "trigger_params": {
                "type": "object",
                "description": "The trigger's own settings. " + _TRIGGER_HELP,
            },
            "on_complete": {
                "type": "string",
                "enum": list(ON_COMPLETE),
                "description": "What to do with the setpoints when the step ends",
            },
            "confirm_message": {
                "type": "string",
                "description": "What to ask the operator, for a confirmation trigger",
            },
            "repeat": {"type": "integer", "minimum": 1},
            "group": {"type": "string", "description": "Steps sharing a group repeat together"},
        },
        "required": ["name", "trigger_type"],
        "additionalProperties": False,
    },
}


def _run_steps(runner: Runner, settings: dict[str, Any]) -> dict[str, Any]:
    """Run a protocol the caller wrote, rather than one this build ships."""
    declared = settings.get("steps") or []
    if not declared:
        raise Refused("run_steps needs at least one step")
    steps = [_step_from(entry, index) for index, entry in enumerate(declared)]
    result = runner.run_steps(steps, tick_s=float(settings.get("tick_s", 0.2)))
    return {**result.metadata, "protocol": "custom", "steps": len(steps)}


def _step_from(entry: dict[str, Any], index: int) -> ProtocolStep:
    trigger = str(entry.get("trigger_type") or "")
    if trigger not in TRIGGER_TYPES:
        raise Refused(
            f"step {index} has trigger {trigger!r}; the triggers are: {', '.join(TRIGGER_TYPES)}"
        )
    on_complete = str(entry.get("on_complete") or "hold")
    if on_complete not in ON_COMPLETE:
        raise Refused(
            f"step {index} ends with {on_complete!r}; it must be one of: {', '.join(ON_COMPLETE)}"
        )
    given = dict(entry.get("trigger_params") or {})
    taken = trigger_params(trigger)
    unknown = set(given) - set(taken)
    if unknown:
        raise Refused(
            f"step {index}: the {trigger} trigger has no setting "
            f"{', '.join(sorted(unknown))}; it takes: {', '.join(taken) or 'nothing'}"
        )
    missing = {name for name, needed in taken.items() if needed} - set(given)
    if missing:
        raise Refused(f"step {index}: the {trigger} trigger needs {', '.join(sorted(missing))}")
    return ProtocolStep(
        name=str(entry.get("name") or f"step {index}"),
        sensor_setpoints={int(k): float(v) for k, v in (entry.get("sensor_setpoints") or {}).items()},
        pressure_setpoints={
            int(k): float(v) for k, v in (entry.get("pressure_setpoints") or {}).items()
        },
        trigger_type=trigger,
        trigger_params=given,
        on_complete=on_complete,
        confirm_message=str(entry.get("confirm_message") or ""),
        repeat=int(entry.get("repeat") or 1),
        group=str(entry.get("group") or ""),
    )


OPERATIONS: tuple[Operation, ...] = (
    Operation(
        "create_project",
        "Create a project",
        "Start a project and make it the one this session writes into.",
        target=GENERAL,
        params=(
            Param("path", "Path", ParamKind.PATH, default="", required=True,
                  description="Where to create it; .admetp is added if missing"),
            Param("project_id", "Name", ParamKind.TEXT, default="",
                  description="Defaults to the directory name"),
        ),
        run=_project_create,
    ),
    Operation(
        "open_project",
        "Open a project",
        "Work in an existing project.",
        target=GENERAL,
        params=(Param("path", "Path", ParamKind.PATH, default="", required=True),),
        run=_project_open,
    ),
    Operation(
        "read_project",
        "Which project is open",
        "The open project and what it holds.",
        kind=READ,
        target=GENERAL,
        run=_project_status,
    ),
    Operation(
        "list_projects",
        "Projects on disk",
        "Projects found under a directory.",
        kind=READ,
        target=GENERAL,
        params=(Param("root", "Directory", ParamKind.PATH, default="", required=True),),
        run=_project_list,
    ),
    Operation(
        "connect_fluidics",
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
    Operation("disconnect_fluidics", "Disconnect fluidics", "Release the instrument.",
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
    Operation("read_status", "Status", "What the instrument and the session are doing.", kind=READ,
              target=GENERAL, uses=("read_status",), run=_status),
    Operation(
        "set_channel_flow",
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
        "run_priming",
        "Priming protocol",
        "Wet every line, confirming each channel in turn.",
        kind=START,
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
        "run_wash",
        "Wash protocol",
        "Flush the chip with IPA, then hold pressure.",
        kind=START,
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
        "run_characterisation",
        "Flow check",
        "Sweep the working flows to measure what the plumbing and chip cost.",
        kind=START,
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
        "run_gravimetry",
        "Dispense check",
        "Dispense a weighed volume from every channel, gated on the operator.",
        kind=START,
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
        "run_dropseq",
        "Drop-Seq run",
        "Generate droplets for each set and replicate.",
        kind=START,
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
    Operation(
        "run_steps",
        "Run steps you wrote",
        "Run a protocol given as a list of steps, without adding it to this build.",
        kind=START,
        params=(TICK,),
        raw={"steps": STEP_LIST_SCHEMA},
        requires=("fluidics", "corrections", "idle"),
        starts_protocol=True,
        run=_run_steps,
    ),
    Operation("pause_protocol", "Pause protocol", "Hold the protocol and zero the channels.",
              requires=("running",), uses=("pause_protocol",), run=_pipeline_control("pause_protocol")),
    Operation("resume_protocol", "Resume protocol", "Carry on from a pause.",
              requires=("running",), uses=("resume_protocol",), run=_pipeline_control("resume_protocol")),
    Operation("stop_protocol", "Stop protocol", "End the protocol and release the channels.",
              requires=("running",), uses=("stop_protocol",), run=_pipeline_control("stop_protocol")),
    Operation("confirm_protocol", "Confirm step", "Answer a step that is waiting for the operator.",
              requires=("running",), uses=("confirm_protocol",), run=_pipeline_control("confirm_protocol")),
    Operation("skip_protocol", "Skip step", "Abandon the waiting step and move on.",
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
        target=ANALYZE,
        requires=("project",),
        run=_add_source,
    ),
    Operation(
        "list_sources",
        "What is there to analyse",
        "Everything the project holds that analysis can be run over.",
        kind=READ,
        target=ANALYZE,
        requires=("project",),
        run=_sources,
    ),
    Operation(
        "run_analysis",
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
        target=ANALYZE,
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
