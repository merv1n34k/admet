"""The way in.

Everything that drives this system talks to core, and core talks to the engines.
The desktop and Python callers share this service; neither needs to know
which files a recording creates or where those files belong.

Core owns the home. An engine is handed the paths it writes to and never
chooses them: only core knows the home's data folder, and an engine picking its
own would write somewhere nothing lists -- data on disk nothing can find again.

    admet = Admet()                       # ~/.admet, or ADMET_HOME
    admet.run("acquisition", "connect_fluidics", {"simulated": True})
    admet.run("acquisition", "start_recording", {"recording_label": "set01"})
"""

from __future__ import annotations

import logging
import threading

import time
import hashlib
import json
import uuid
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from admet.core.engine import ActionSpec, action_spec
from admet.core.clock import now_iso as _now_iso
from admet.core.home import AdmetHome
from admet.core.runs import RECORD, Run, RunStore
from admet.core.run import RunJob, RunResult
from admet.workflows.operations import operation as find_operation

log = logging.getLogger(__name__)

# Which engine registry each engine belongs to.
ENGINE_GROUPS = {
    "acquisition": "control",
    "opencv": "analyze",
    "cellpose": "analyze",
}


def _describe_event(event: Any) -> dict[str, Any]:
    """One protocol event as plain data, for anything outside the engine."""
    return {
        "sequence": event.sequence,
        "at": event.at,
        "monotonic": event.monotonic,
        "state": str(event.state),
        "outcome": str(event.outcome),
        "step_index": event.current_step,
        "total_steps": event.total_steps,
        "step_name": event.step_name,
        "progress": event.progress,
        "step_volumes": dict(event.step_volumes),
        "confirmation_message": event.confirmation_message,
        "error": event.error_msg,
    }


def _protocol_yield_reason(event: Any) -> str | None:
    outcome = str(event.outcome)
    state = str(event.state)
    if str(event.confirmation_message or "").strip():
        return "confirmation_required"
    if state == "paused":
        return "protocol_paused"
    if event.step_name and outcome != "running":
        return {
            "completed": "step_completed",
            "timed_out": "step_timed_out",
            "skipped": "step_skipped",
            "cancelled": "step_cancelled",
            "error": "step_failed",
        }.get(outcome)
    if not event.step_name and outcome != "running":
        return {
            "completed": "protocol_completed",
            "cancelled": "protocol_cancelled",
            "error": "protocol_failed",
        }.get(outcome)
    if state == "running" and event.current_step < 0:
        return "protocol_started"
    return None


def _describe_param(param: Any) -> dict[str, Any]:
    return {
        "name": param.name,
        "type": param.kind.value,
        "default": param.default,
        "minimum": param.minimum,
        "maximum": param.maximum,
        "description": param.description or param.label,
    }


def _requirement_reason(name: str) -> str:
    from admet.workflows.operations import REQUIREMENTS

    return REQUIREMENTS[name][1]


def _fill_defaults(operation: Any, settings: dict[str, Any]) -> dict[str, Any]:
    """Validate what was supplied and fill in the rest from the operation."""
    known = {param.name: param for param in operation.params}
    raw = getattr(operation, "raw", {}) or {}
    unknown = set(settings) - set(known) - set(raw)
    if unknown:
        offered = ", ".join(sorted({*known, *raw})) or "nothing"
        raise LookupError(
            f"{operation.id} has no setting {', '.join(sorted(unknown))}; it takes: {offered}"
        )
    filled = {
        name: param.validate(settings.get(name, param.default)) for name, param in known.items()
    }
    return {**filled, **{name: settings[name] for name in raw if name in settings}}


def _planned_step(index: int, step: Any) -> dict[str, Any]:
    from admet.engines.acquisition.pipeline import RECORD_CAP_S

    params = deepcopy(step.trigger_params)
    timeout = step.timeout_s if step.timeout_s is not None else params.get("timeout_s")
    expected = params.get("duration_s")
    if expected is None and step.trigger_type == "confirmation":
        expected = 0.0
    if expected is None and step.trigger_type == "stability":
        expected = timeout
    if expected is None and step.trigger_type == "volume":
        sensor_index = int(params["sensor_index"])
        requested_flow = step.sensor_setpoints.get(sensor_index)
        target_volume = params.get("target_volume_ul")
        if requested_flow is not None and requested_flow > 0 and target_volume is not None:
            expected = float(target_volume) / float(requested_flow) * 60.0
    return {
        "number": index,
        "name": step.name,
        "flow_setpoints_ul_min": {str(k): v for k, v in step.sensor_setpoints.items()},
        "pressure_setpoints_mbar": {str(k): v for k, v in step.pressure_setpoints.items()},
        "trigger_type": step.trigger_type,
        "trigger_params": params,
        "timeout_s": timeout,
        "expected_duration_s": expected,
        "on_complete": step.on_complete,
        "confirmation": step.confirm_message or None,
        "record_s": min(step.record_max_s or RECORD_CAP_S, RECORD_CAP_S) if step.record else None,
    }


def _sum_expected_duration(steps: list[dict[str, Any]]) -> float | None:
    values = [step["expected_duration_s"] for step in steps]
    return sum(float(value) for value in values) if all(value is not None for value in values) else None


class Admet:
    """The rig's home and the engines that fill it."""

    def __init__(self, *, home: AdmetHome | str | Path | None = None):
        self._engines: dict[str, Any] = {}
        self._marks: dict[str, Any] = {}
        self.home = (home if isinstance(home, AdmetHome) else AdmetHome.resolve(home)).ensure()
        self.runs = RunStore(self.home.data_dir)
        self._protocol_plans: dict[str, dict[str, Any]] = {}
        self._executing_plan_id: str | None = None
        self._run_artifacts: dict[str, Any] = {}
        self._execution_lock = threading.Lock()
        # Observation and commands may arrive from different desktop threads.
        self._engine_lock = threading.Lock()

        self._tell_engine_home()
        self._encode_leftovers()

    # -- the rig's settings ----------------------------------------------------
    def rig(self) -> dict[str, Any]:
        """The rig's settings, from the home's rig.json."""
        return self.home.rig()

    def update_rig(self, **changes: Any) -> dict[str, Any]:
        """Keep rig settings; recordings from now on carry what matters to them."""
        settings = self.home.update_rig(**changes)
        self._tell_engine_home()
        return settings

    def _tell_engine_home(self) -> None:
        """The acquisition engine stamps recordings with what the rig knows about itself."""
        engine = self._engines.get("acquisition")
        if engine is not None and hasattr(engine, "recording_context"):
            context: dict[str, Any] = {}
            optics = self.rig().get("optics")
            if isinstance(optics, dict) and optics.get("microns_per_pixel"):
                context["optics"] = dict(optics)           # µm per px as measured on the preview
            engine.recording_context = context

    def _require_idle(self):
        engine = self._engines.get("acquisition")
        if self._executing_plan_id or (engine and (getattr(engine, "pipeline_state", "idle") in {
            "running", "paused", "stopping",
        } or getattr(engine, "recording_active", False))):
            raise RuntimeError("finish the active protocol/recording first")

    def protocol_store(self):
        from admet.core.protocol_store import ProtocolStore

        return ProtocolStore(self.home.data_dir)

    @property
    def experiments(self):
        """The experiments designed on this home, each a table of runs with their ids."""
        from admet.core.experiments import ExperimentStore

        return ExperimentStore(self.home.data_dir)

    def protocol_document(self, name: str) -> dict[str, Any]:
        """A protocol by its name: one saved in the data folder first, else a template; LookupError if neither."""
        from admet.workflows.json_protocol import template_documents

        try:
            return self.protocol_store().read(name)["protocol"]
        except (OSError, ValueError):
            pass
        templates = template_documents()
        if name in templates:
            return templates[name]
        raise LookupError(f"no protocol {name}")

    @property
    def registry(self):
        """Analyze's registry: the sources it knows and where their results are."""
        from admet.core.registry import Registry

        return Registry(self.home.analyze_dir)

    # -- runs ----------------------------------------------------------------
    def measurements(self, run_id: str, changes: dict[str, Any] | None = None) -> dict[str, Any]:
        """The values typed during a run; with changes, updated."""
        return self.runs.open(run_id).measurements(changes)

    def measurement_runs(self) -> list[dict[str, Any]]:
        """The runs that declared measurements, newest first."""
        return [{"run_id": run["run_id"], "name": run["name"], "at": run["at"]}
                for run in self.runs.list() if run["measurements"]]

    # -- export ----------------------------------------------------------------
    def export_path(self) -> str:
        """The server folder runs are exported to, as the rig remembers it; empty until set."""
        export = self.rig().get("export")
        return str(export.get("path") or "") if isinstance(export, dict) else ""

    def set_export_path(self, path: str) -> None:
        text = str(path or "").strip()
        self.update_rig(export={"path": text} if text else None)

    def export_runs(self, progress=None) -> list[dict[str, Any]]:
        """Every finished run to the export folder, checked, the local videos then let go."""
        from admet.core.export import export_runs

        path = self.export_path()
        if not path:
            raise ValueError("Choose the export folder first")
        return export_runs(self.runs, path, progress=progress)

    def plan_protocol_file(self, path: str):
        from admet.workflows.json_protocol import load

        return self.plan_protocol("run_json_protocol", {"protocol": load(path)})

    def describe_home(self) -> dict[str, Any]:
        return {"home": str(self.home.root), "control": str(self.home.control_dir), "data": str(self.home.data_dir),
                "analyze": str(self.home.analyze_dir), "rig": self.rig()}

    # -- engines ------------------------------------------------------------
    def engine_ids(self) -> list[str]:
        """Every engine this build has, whether or not it has been created yet."""
        return list(ENGINE_GROUPS)

    def engine(self, engine_id: str) -> Any:
        """The engine, created on first use. Analysis stacks are slow to import."""
        with self._engine_lock:
            return self._engine(engine_id)

    def _engine(self, engine_id: str) -> Any:
        if engine_id not in self._engines:
            group = ENGINE_GROUPS.get(engine_id)
            if group is None:
                known = ", ".join(sorted(ENGINE_GROUPS))
                raise LookupError(f"unknown engine {engine_id!r}; this build has: {known}")
            from admet.engines import create_engine_registry

            engine = create_engine_registry(group).create(engine_id)
            if hasattr(engine, "on_video_finished"):
                engine.on_video_finished = self._video_finished
            self._engines[engine_id] = engine
            self._tell_engine_home()
        return self._engines[engine_id]

    def _video_finished(self, video_path: str, encoded: bool, reason: str) -> None:
        """A video finished encoding after its recording stopped, or could not: its run's record says so."""
        path = Path(video_path)
        if not (path.parent / RECORD).is_file():
            return
        try:
            Run(path.parent).video_finished(path.name, encoded, reason)
        except Exception:
            log.exception("could not note the encoded video %s", video_path)

    def _encode_leftovers(self) -> None:
        """Finish, in the background, encodings a previous session left unfinished in any run."""
        from admet.engines.acquisition.camera.video import encode_leftovers

        folders = list(dict.fromkeys(path.parent for path in self.runs.runs_dir.glob("*/*.video.json")))
        if folders:
            threading.Thread(target=lambda: [encode_leftovers(folder, self._video_finished) for folder in folders],
                             name="EncodeLeftovers", daemon=True).start()

    def describe(self, target: str = "") -> dict[str, Any]:
        """What exists, at whichever level was asked about.

        With no target, the operations that are the way in and the engines
        underneath them. With one, the detail of that operation or engine -- and
        for an operation, which engine actions it drives, so the two can be read
        against each other rather than guessed at.
        """
        from admet.workflows.operations import BY_ID as OPERATIONS_BY_ID

        if not target:
            return {
                "operations": [
                    {
                        "id": op.id,
                        "label": op.label,
                        "target": op.target,
                        "kind": op.kind,
                        "requires": list(op.requires),
                    }
                    for op in OPERATIONS_BY_ID.values()
                ],
                "engines": [
                    {"id": engine_id, "group": group} for engine_id, group in ENGINE_GROUPS.items()
                ],
            }

        if target in OPERATIONS_BY_ID:
            return self._describe_operation(OPERATIONS_BY_ID[target])
        if target in ENGINE_GROUPS:
            return self.describe_engine(target)

        known = ", ".join(sorted({*OPERATIONS_BY_ID, *ENGINE_GROUPS}))
        raise LookupError(f"nothing called {target!r} to describe; there is: {known}")

    def _describe_operation(self, op: Any) -> dict[str, Any]:
        return {
            "layer": "operation",
            "id": op.id,
            "label": op.label,
            "target": op.target,
            "kind": op.kind,
            "description": op.description,
            "requires": [
                {"name": name, "why": _requirement_reason(name)} for name in op.requires
            ],
            "params": [_describe_param(param) for param in op.params],
            # Settings too structured for a Param carry their own JSON schema.
            "structured_params": op.raw,
            "engine_actions": list(op.uses),
            "protocol": op.protocol,
            "starts_protocol": op.starts_protocol,
        }


    def describe_engine(self, engine_id: str) -> dict[str, Any]:
        """A workhorse and everything it can be told to do.

        Engine actions are the low level: no guards, one call, everything the
        device can be told. Operations are the same calls with the conditions
        under which using them is not a mistake.
        """
        engine = self.engine(engine_id)
        from admet.workflows.operations import OPERATIONS

        driven: dict[str, list[str]] = {}
        for op in OPERATIONS:
            for action in op.uses:
                driven.setdefault(action, []).append(op.id)
        return {
            "layer": "engine",
            "engine": {"id": engine.id, "name": engine.name},
            "actions": [
                {
                    "id": action.id,
                    "label": action.label,
                    "category": action.category,
                    "kind": action.kind,
                    "params": list(action.params),
                    "outputs": list(action.outputs),
                    "artifact": action.artifact,
                    "used_by": driven.get(action.id, []),
                }
                for action in engine.actions
            ],
            "settings": [param.name for param in engine.settings.params],
        }

    # -- operations ---------------------------------------------------------
    def operations(self, target: str = "") -> list[dict[str, Any]]:
        """Everything that can be asked for, with its parameters and its guards.

        Narrowed to one half of the system when asked: the instrument, the
        analysis of what it produced, or the session both work in.
        """
        from admet.workflows.operations import OPERATIONS

        return [
            {
                "id": op.id,
                "label": op.label,
                "target": op.target,
                "kind": op.kind,
                "description": op.description,
                "requires": list(op.requires),
                "params": [
                    {
                        "name": param.name,
                        "type": param.kind.value,
                        "default": param.default,
                        "minimum": param.minimum,
                        "maximum": param.maximum,
                        "description": param.description or param.label,
                    }
                    for param in op.params
                ],
            }
            for op in OPERATIONS
            if not target or op.target == target
        ]

    def do(self, operation_id: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """Carry out one operation, if now is a moment it makes sense.

        This is the way in. Engine actions are hardware primitives and are not
        reachable from outside: an operation is the same primitive plus the
        conditions under which using it is not a mistake.
        """

        op = find_operation(operation_id)
        op.check(self.state())
        settings = _fill_defaults(op, dict(settings or {}))
        return op.run(self, settings)

    def plan_protocol(self, operation_id: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """Validate and freeze a protocol proposal without performing an engine action.

        The protocol's run block names the plan: the tag is the run's name as the operator
        gave it, which the run's record, its recordings and the lists of runs carry; the condition
        is how the droplets were made, the group analyze puts its videos in."""
        from admet.engines.acquisition.pipeline import expand_protocol_steps
        from admet.workflows.operations import REQUIREMENTS, build_protocol_steps

        op = find_operation(operation_id)
        if not op.starts_protocol:
            raise LookupError(f"{operation_id} is not a protocol-producing operation")
        normalized = _fill_defaults(op, dict(settings or {}))
        state = self.state()
        guards = {
            name: {"met": bool(REQUIREMENTS[name][0](state)), "why_not": (
                "" if REQUIREMENTS[name][0](state) else REQUIREMENTS[name][1]
            )}
            for name in op.requires
        }
        channels = self._cached_channel_mapping()
        steps = expand_protocol_steps(build_protocol_steps(op, normalized, channels=channels))
        protocol = normalized.get("protocol") if isinstance(normalized.get("protocol"), dict) else {}
        run = protocol.get("run") if isinstance(protocol.get("run"), dict) else {}
        described_steps = [_planned_step(index, step) for index, step in enumerate(steps, 1)]
        video_steps = [step["number"] for step in described_steps if step["record_s"]]
        if video_steps:
            engine = self._engines.get("acquisition")
            ready = bool(engine and engine.camera_live)
            guards["camera_live"] = {"met": ready, "why_not": "" if ready else "Connect the camera and start Live"}
        executable = {
            "operation_id": operation_id,
            "normalized_settings": normalized,
            "steps": described_steps,
        }
        digest = hashlib.sha256(
            json.dumps(executable, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        plan_id = f"plan_{uuid.uuid4().hex}"
        warnings = ([] if all(g["met"] for g in guards.values()) else [
            "one or more execution guards are currently unmet"
        ])
        plan = {
            "plan_id": plan_id,
            "operation_id": operation_id,
            "tag": str(run.get("name") or ""),
            "condition": str(run.get("condition") or ""),
            "run_code": str(run.get("id") or ""),          # the run's id in its experiment; its samples add -rN
            "experiment": str(run.get("experiment") or ""),
            "state": "planned",
            "created_at": _now_iso(),
            "normalized_settings": normalized,
            "steps": described_steps,
            "step_count": len(described_steps),
            "expected_duration_s": _sum_expected_duration(described_steps),
            "required_confirmations": [
                step["confirmation"] for step in described_steps if step["confirmation"]
            ],
            "recording": {
                "required": operation_id == "run_json_protocol",
                "video_steps": video_steps,
            },
            "camera_required": bool(video_steps),
            "abort_conditions": [
                "fluidics disconnects or required telemetry becomes unavailable",
                "protocol reports an error",
            ],
            "warnings": warnings,
            "assumptions": [
                "configured channel labels match the physical tubing only after operator confirmation"
            ],
            "guards": guards,
            "unmet_guards": [name for name, guard in guards.items() if not guard["met"]],
            "digest": digest,
            "rig_fingerprint": self._rig_fingerprint(),
            "error": "",
        }
        self._protocol_plans[plan_id] = deepcopy(plan)
        return deepcopy(plan)

    def discard_protocol_preview(self, plan_id):
        with self._execution_lock:
            plan = self._protocol_plans.get(plan_id)
            if plan is not None and not plan.get("run_id"):
                self._protocol_plans.pop(plan_id, None)

    def planned_protocols(self, plan_id: str = "") -> dict[str, Any]:
        self._refresh_plan_lifecycle()
        if plan_id:
            if plan_id not in self._protocol_plans:
                raise LookupError(f"unknown protocol plan {plan_id!r}")
            return {"plans": [deepcopy(self._protocol_plans[plan_id])]}
        return {"plans": [deepcopy(plan) for plan in self._protocol_plans.values()]}

    def execute_protocol_plan(self, plan_id: str) -> dict[str, Any]:
        with self._execution_lock:
            return self._execute_protocol_plan(plan_id)

    def _execute_protocol_plan(self, plan_id: str) -> dict[str, Any]:
        self._refresh_plan_lifecycle()
        plan = self._protocol_plans.get(plan_id)
        if plan is None:
            raise LookupError(f"unknown protocol plan {plan_id!r}")
        if plan["state"] != "planned":
            raise RuntimeError(f"protocol plan {plan_id} is {plan['state']} and cannot be executed")
        if plan["rig_fingerprint"] != self._rig_fingerprint():
            plan["state"] = "replaced"
            self._save_plan(plan)
            raise RuntimeError(f"protocol plan {plan_id} is stale because the rig context changed")
        op = find_operation(plan["operation_id"])
        engine = self._engines.get("acquisition")
        latest = engine.latest_event() if engine is not None else None
        before_sequence = latest.sequence if latest else 0
        op.check(self.state())
        if self._executing_plan_id:
            raise RuntimeError("another plan is still executing")
        if plan["camera_required"] and not (engine and engine.camera_live):
            raise RuntimeError("Connect the camera and start Live before executing this plan")
        plan["state"] = "executing"
        plan["executed_at"] = _now_iso()
        self._executing_plan_id = plan_id
        try:
            run = self.runs.begin(plan)                    # the run's folder, its record holding the plan
            plan["run_id"] = run.run_id
            self._run_artifacts[plan_id] = {"run": run, "artifacts": {}, "recording": False}
            if plan["operation_id"] == "run_json_protocol":
                if engine is not None and engine.recording_active:
                    raise RuntimeError("stop the existing recording before executing a JSON protocol")
                # A protocol's recording lasts as long as the protocol: a frame or
                # time limit would close the timeline file while it still runs.
                self.do("start_recording", {
                    "recording_label": run.run_id, "include_video": False,
                    "recording_max_frames": 0, "recording_max_seconds": 0,
                })
                if plan["recording"]["video_steps"]:
                    # Video is per step, opened and closed by the steps themselves, into the run's
                    # folder. Every recording's record names the run's timeline file, so its slice
                    # of it is one cut by time.
                    engine.arm_step_video(run.path, run.name, {
                        "run_id": run.run_id, "tag": plan.get("tag", ""), "condition": plan.get("condition", ""),
                        "protocol": str(plan.get("name") or plan.get("protocol_name") or plan.get("operation_id") or ""),
                        "timeline": RECORD,
                    })
                artifact = self._run_artifacts[plan_id]
                artifact["recording"] = True
                artifact["artifacts"]["timeline"] = RECORD
                artifact["artifacts"]["polling_origin_monotonic"] = self.polling_started_monotonic()
                artifact["artifacts"]["recording_closed"] = False
                run.update(artifacts=artifact["artifacts"])
            result = op.run(self, deepcopy(plan["normalized_settings"]))
        except Exception as exc:
            plan["state"] = "failed"
            plan["error"] = str(exc)
            self._executing_plan_id = None
            self._finish_plan(plan)
            raise
        started = self.wait_protocol_event(after_sequence=before_sequence, timeout_s=1.0)
        return {
            **result,
            "plan_id": plan_id,
            "run_id": plan["run_id"],
            "plan_state": plan["state"],
            "yield": started,
        }

    def cancel_protocol_plan(self, plan_id: str) -> dict[str, Any]:
        plan = self._protocol_plans.get(plan_id)
        if plan is None:
            raise LookupError(f"unknown protocol plan {plan_id!r}")
        if plan["state"] != "planned":
            raise RuntimeError(f"protocol plan {plan_id} is {plan['state']} and cannot be cancelled")
        plan["state"] = "cancelled"
        plan["cancelled_at"] = _now_iso()
        self._save_plan(plan)
        return deepcopy(plan)

    def _run_of(self, plan) -> Run | None:
        """The run a plan became, once it has one."""
        artifact = self._run_artifacts.get(plan["plan_id"])
        if artifact is not None and artifact.get("run") is not None:
            return artifact["run"]
        run_id = plan.get("run_id")
        return self.runs.open(run_id) if self.runs.exists(run_id) else None

    def _save_plan(self, plan):
        run = self._run_of(plan)
        if run is not None:
            run.update(**plan)

    def _finish_plan(self, plan):
        artifact = self._run_artifacts.get(plan["plan_id"])
        try:
            if artifact and artifact["recording"]:
                self.do("stop_recording")
                artifact["artifacts"]["recording_closed"] = True
                engine = self._engines.get("acquisition")
                videos = engine.step_videos() if engine is not None and plan["recording"]["video_steps"] else []
                if videos:
                    artifact["artifacts"]["videos"] = [        # named within the run's folder
                        {**video, "video_path": Path(video["video_path"]).name if video["video_path"] else ""}
                        for video in videos]
                    for video in videos:
                        if not video["video_path"]:
                            continue
                        try:                       # one video's record failing must not lose the others
                            self._keep_recording(video)
                        except Exception:
                            log.exception("could not list the video of step %s", video.get("step"))
        finally:
            self._save_plan(plan)
            if artifact and artifact.get("run") is not None:
                artifact["run"].update(artifacts=artifact["artifacts"])
            self._run_artifacts.pop(plan["plan_id"], None)

    def _archive_protocol_event(self, plan_id, event):
        plan = self._protocol_plans[plan_id]
        if event.error_msg:
            plan["error"] = event.error_msg
        artifact = self._run_artifacts.get(plan_id)
        if artifact and artifact.get("run") is not None:
            artifact["run"].event(_describe_event(event))
        if not event.step_name and str(event.outcome) in {"completed", "cancelled", "error"}:
            finished = deepcopy(plan)
            finished["state"] = "completed" if str(event.outcome) == "completed" else "failed"
            finished["completed_at"] = _now_iso()
            try:
                self._finish_plan(finished)
                plan.update(finished)
            finally:
                if self._executing_plan_id == plan_id:
                    self._executing_plan_id = None

    def _refresh_plan_lifecycle(self) -> None:
        if not self._executing_plan_id:
            return
        engine = self._engines.get("acquisition")
        if engine is None or engine.pipeline_state in {"running", "paused", "stopping"}:
            return
        plan = self._protocol_plans[self._executing_plan_id]
        if self._executing_plan_id in self._run_artifacts:
            return
        event = engine.latest_event()
        outcome = str(getattr(event, "outcome", "")) if event else ""
        plan["state"] = "completed" if outcome == "completed" else "failed"
        plan["completed_at"] = _now_iso()
        plan["error"] = getattr(event, "error_msg", "") if event else "protocol ended without an event"
        self._executing_plan_id = None
        self._save_plan(plan)

    def _rig_fingerprint(self) -> dict[str, Any]:
        engine = self._engines.get("acquisition")
        mapping = []
        hardware = None
        if engine is not None:
            hardware = engine.hardware.state
            mapping = [channel.get("detected") for channel in self._cached_channel_mapping()]
        return {
            "home": str(self.home.root),
            "fluidics_connected": bool(hardware and hardware.connected),
            "simulated": bool(hardware and hardware.simulated),
            "channel_mapping": mapping,
            "correction_settings": deepcopy(self._marks.get("correction_settings")),
            "hardware_identity": {
                "controller_serials": sorted({
                    getattr(item, "controller_sn", None)
                    for item in getattr(hardware, "pressure_channels", ())
                }) if hardware else [],
                "camera_connected": bool(engine and getattr(engine.camera, "connected", False)),
                "cameras": deepcopy(
                    getattr(getattr(engine, "_camera", None), "_preflight_cache", {}).get(
                        "cameras", []
                    )
                ) if engine else [],
            },
        }

    def _cached_channel_mapping(self) -> list[dict[str, Any]]:
        """Configured/detected identities already held in memory; never query an SDK."""
        engine = self._engines.get("acquisition")
        if engine is None:
            return []
        from admet.engines.acquisition.engine import _detected_channel
        from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNEL_LABELS

        state = engine.hardware.state
        return [
            {
                "index": index,
                "label": FLUIDIC_CHANNEL_LABELS[index]
                if index < len(FLUIDIC_CHANNEL_LABELS)
                else "",
                "detected": _detected_channel(state, channel),
            }
            for index, channel in enumerate(engine.channel_manager.channels)
        ]


    def wait_for_protocol(self, *, timeout_s: float = 600.0, poll_s: float = 0.1) -> str:
        """Wait for the running protocol to end, or to want the operator.

        Returns what happened: completed, waiting (a step needs an answer),
        error, or timeout. A protocol that stopped for a person is not a failure
        and is not treated as one.
        """
        engine = self.engine("acquisition")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            state = engine.pipeline_state
            if state in {"completed", "idle", "error"}:
                self._wait_for_plan_to_close(deadline, poll_s)
                return "error" if state == "error" else "completed"
            if self._waiting_for_operator(engine):
                return "waiting"
            time.sleep(poll_s)
        return "timeout"

    def _wait_for_plan_to_close(self, deadline: float, poll_s: float) -> None:
        """The pipeline ends a moment before its last event closes the plan; a caller that asks
        about the plan right after waiting must find it closed, not still executing."""
        while self._executing_plan_id in self._run_artifacts and time.monotonic() < deadline:
            time.sleep(poll_s)

    @staticmethod
    def _waiting_for_operator(engine: Any) -> bool:
        """True when the newest event is a step asking for a confirmation."""
        queue = getattr(engine, "pipeline_queue", None)
        if queue is None:
            return False
        waiting = False
        while not queue.empty():
            event = queue.get_nowait()
            if str(getattr(event, "confirmation_message", "") or "").strip():
                waiting = True
        return waiting

    # -- analysis ------------------------------------------------------------
    def add_analysis_source(self, path: Path, *, engine: str, sample_id: str = "") -> dict[str, Any]:
        """Register something to analyse with analyze's registry."""
        source = Path(path).resolve()
        entry = self.registry.add_source(source, engine=engine, sample_id=sample_id or source.stem) \
            or self.registry.source(source) or {}
        role = "analysis_image_dir" if engine == "cellpose" else "analysis_video"
        return {"id": str(entry.get("path", source)), "role": role, "path": str(source), "engine": engine}

    def analysis_sources(self) -> list[dict[str, Any]]:
        return [
            {
                "id": entry["path"],
                "role": "analysis_image_dir" if entry.get("engine") == "cellpose" else "analysis_video",
                "path": entry["path"],
                "engine": entry.get("engine", ""),
                "sample_id": entry.get("sample_id", ""),
            }
            for entry in self.registry.sources()
        ]

    def analyze(self, *, engine: str = "", cache: str = "use") -> dict[str, Any]:
        """Analyse every registered source, the results going to analyze's results folder."""
        from admet.engines import create_engine_registry
        from admet.workflows.analyze_runner import AnalyzeBatchRunner, AnalyzeTarget

        registry = self.registry
        targets = [
            AnalyzeTarget(
                source_path=Path(source["path"]),
                engine=engine or source["engine"],
                sample_id=source["sample_id"],
                settings=dict((registry.source(source["path"]) or {}).get("settings") or {}),
                cache_policy=cache,
            )
            for source in self.analysis_sources()
        ]
        report = AnalyzeBatchRunner(create_engine_registry("analyze"), results_dir=registry.results_dir).run(targets)
        return {
            "results": str(registry.results_dir),
            "jobs": [
                {
                    "sample_id": job.sample_id,
                    "engine": job.engine,
                    "status": job.status,
                    "warnings": list(job.warnings),
                }
                for job in report.jobs
            ],
        }

    # -- what an operation needs from core -----------------------------------
    def corrections_applied(self) -> bool:
        """Whether correction factors are on the hardware for this connection."""
        return bool(self._marks.get("corrections", False))

    def state(self) -> dict[str, Any]:
        """What is true right now, as the guards understand it."""
        engine = self._engines.get("acquisition")
        if engine is None:
            return {
                "fluidics": False,
                "camera": False,
                "corrections": False,
                "running": False,
                "sources": len(self.analysis_sources()),
            }
        return {
            "fluidics": bool(engine.hardware.state.connected),
            "camera": bool(getattr(engine.camera, "connected", False)),
            "corrections": self._marks.get("corrections", False),
            "running": engine.pipeline_state in {"running", "paused", "stopping"},
            "sources": len(self.analysis_sources()),
        }


    def polling_started_monotonic(self) -> float:
        engine = self._engines.get("acquisition")
        return engine.polling_started_monotonic() if engine is not None else 0.0


    def emergency_stop(self, reason: str = "") -> dict[str, Any]:
        """Zero every channel, stop the protocol, close the recording.

        A recording closed here is added to the project like any other, so it
        is not left on disk where the project cannot see it.
        """
        stopped = self.engine("acquisition").emergency_stop(reason)
        self._keep_recording(stopped.get("closed_recording"))
        return stopped

    def protocol_events(self, *, after_sequence: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Protocol events newer than one already seen, as plain data."""
        engine = self._engines.get("acquisition")
        if engine is None:
            return []
        return [_describe_event(event) for event in engine.events_after(after_sequence, limit)]

    def wait_protocol_event(
        self,
        *,
        after_sequence: int = 0,
        timeout_s: float = 10.0,
    ) -> dict[str, Any]:
        """Long-poll until a protocol milestone or a bounded timeout."""
        if timeout_s < 0.1 or timeout_s > 60.0:
            raise ValueError("timeout_s must be between 0.1 and 60 seconds")
        engine = self._engines.get("acquisition")
        if engine is None:
            return {
                "status": "idle",
                "reason": "no_acquisition_session",
                "after_sequence": after_sequence,
                "next_sequence": after_sequence,
                "event": None,
            }
        event, cursor = engine.wait_for_event(
            after_sequence,
            lambda candidate: _protocol_yield_reason(candidate) is not None,
            timeout_s,
        )
        if event is None:
            return {
                "status": "timeout",
                "reason": "timeout",
                "after_sequence": after_sequence,
                "next_sequence": cursor,
                "event": None,
            }
        return {
            "status": "event",
            "reason": _protocol_yield_reason(event),
            "after_sequence": after_sequence,
            "next_sequence": cursor,
            "event": _describe_event(event),
        }

    def control_protocol(
        self,
        *,
        action: str,
        plan_id: str = "",
        after_sequence: int | None = None,
        timeout_s: float = 10.0,
    ) -> dict[str, Any]:
        """Apply one protocol action and return at its next bounded yield."""
        actions = {
            "confirm": "confirm_protocol",
            "skip": "skip_protocol",
            "pause": "pause_protocol",
            "resume": "resume_protocol",
            "abort": "stop_protocol",
        }
        if action not in {"execute", "wait", *actions}:
            raise ValueError(
                "action must be execute, wait, confirm, skip, pause, resume, or abort"
            )
        if timeout_s < 0.1 or timeout_s > 60.0:
            raise ValueError("timeout_s must be between 0.1 and 60 seconds")
        engine = self._engines.get("acquisition")
        latest = engine.latest_event() if engine is not None else None
        cursor = latest.sequence if latest else 0
        if after_sequence is not None:
            cursor = int(after_sequence)
            if cursor < 0:
                raise ValueError("after_sequence must be at least 0")

        if action == "execute":
            if not plan_id:
                raise ValueError("plan_id is required for execute")
            started = self.execute_protocol_plan(plan_id)
            start_yield = started["yield"]
            yielded = self.wait_protocol_event(
                after_sequence=start_yield["next_sequence"],
                timeout_s=timeout_s,
            )
            return {
                "action": action,
                **{key: value for key, value in started.items() if key != "yield"},
                "yield": yielded,
            }
        if plan_id:
            raise ValueError("plan_id is accepted only for execute")
        if action != "wait":
            self.do(actions[action])
        yielded = self.wait_protocol_event(after_sequence=cursor, timeout_s=timeout_s)
        return {"action": action, "yield": yielded}

    def mark(self, name: str, value: Any) -> None:
        """Remember something the hardware does not report, such as corrections."""
        self._marks[name] = value

    def fluidics_configuration(self) -> dict[str, Any]:
        """Configured channel mapping and the last correction set sent to the rig."""
        return {
            "corrections_applied": bool(self._marks.get("corrections", False)),
            "correction_settings": deepcopy(self._marks.get("correction_settings")),
        }

    def engine_action(self, engine_id: str, action: str, settings: dict[str, Any]):
        """An engine primitive, for operations only."""
        return self.run(engine_id, action, settings)

    def run_steps(self, steps: list[Any], *, tick_s: float = 0.2):
        engine = self.engine("acquisition")
        plan_id = self._executing_plan_id
        if plan_id and plan_id in self._run_artifacts:
            engine.start_pipeline(
                steps, tick_s=tick_s,
                on_event=lambda event: self._archive_protocol_event(plan_id, event),
            )
        else:
            engine.start_pipeline(steps, tick_s=tick_s)
        from admet.core.run import RunResult

        return RunResult("run_steps", "acquisition", "run_steps", metadata={"started": True})

    # -- running an engine action -------------------------------------------
    def run(
        self,
        engine_id: str,
        action: str,
        settings: dict[str, Any] | None = None,
        *,
        job_id: str = "",
    ) -> RunResult:
        """Do one thing, in the open project, and keep what it leaves behind."""
        engine = self.engine(engine_id)
        spec = action_spec(engine.actions, action)
        job = RunJob(
            id=job_id or f"{engine_id}_{action}",
            engine=engine_id,
            action=action,
            settings=dict(settings or {}),
            metadata=self._job_metadata(),
        )
        job = self._allocate_outputs(job, spec)
        result = engine.run(job)
        self._register(spec, result)
        return result

    def run_context(self) -> dict[str, Any]:
        """What was true when something ran, kept alongside what it produced.

        A measurement without its calibration, its channel mapping and the
        limits it ran under is not reproducible, and the flows are not even
        comparable with another run's.
        """
        from admet import __version__

        engine = self._engines.get("acquisition")
        observation = engine.observation() if engine is not None else {}
        return {
            "software_version": __version__,
            "connection": observation.get("connection", {}),
            "corrections": self._marks.get("correction_settings", {}),
            "channels": [
                {"index": channel["index"], "label": channel["label"], **channel["detected"]}
                for channel in observation.get("channels", [])
            ],
        }

    def _job_metadata(self) -> dict[str, Any]:
        return {"home": str(self.home.root), "workdir": str(self.home.data_dir)}

    def _allocate_outputs(self, job: RunJob, spec: ActionSpec) -> RunJob:
        """Decide where this action's files go, before it is allowed to write."""
        if not spec.outputs:
            return job
        label = str(job.settings.get("recording_label") or job.id)
        if self.runs.exists(label):                       # a protocol's run records into its own folder
            run, recording_id = self.runs.open(label), label
        else:                                             # a recording started by hand is a run of its own
            run = self.runs.begin_recording(label)
            recording_id = f"{run.name}_{time.strftime('%Y%m%d_%H%M%S')}"
            run.update(recording_id=recording_id)
        targets = {"video": run.path / f"{recording_id}.avi", "timeline": run.db_path}
        missing = set(spec.outputs) - set(targets)
        if missing:
            raise LookupError(
                f"{spec.id} declares outputs a run cannot place: {', '.join(sorted(missing))}"
            )
        metadata = {**job.metadata, "recording_id": recording_id, "recording_label": label, "run_id": run.run_id}
        return replace(job, outputs={name: targets[name] for name in spec.outputs}, metadata=metadata)

    def _keep_recording(self, recording: dict[str, Any] | None) -> None:
        """What the recording left behind goes into its run's record, named within the folder."""
        if not recording:
            return
        folder = Path(str(recording.get("timeline") or recording.get("video_path") or "")).parent
        if not (folder / RECORD).is_file():
            return
        run = Run(folder)
        kept = {**recording, "context": self.run_context()}
        for key in ("video_path", "timeline"):
            if kept.get(key):
                kept[key] = Path(str(kept[key])).name
        fields: dict[str, Any] = {"recording": kept}
        if run.read().get("kind") == "recording":
            fields.update(state="completed", stopped_at=_now_iso())
        run.update(**fields)

    def _register(self, spec: ActionSpec, result: RunResult) -> None:
        """Put what the action produced into its run's record, so it can be found."""
        if not spec.artifact:
            return
        if spec.artifact == "control_recording":
            self._keep_recording(result.metadata.get("recording"))
            return
        raise LookupError(f"{spec.id} declares an artifact core cannot store: {spec.artifact!r}")
