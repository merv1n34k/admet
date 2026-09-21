"""The way in.

Everything that drives this system talks to core, and core talks to the engines.
A terminal, an MCP client and any future interface all arrive here, so what one
of them can do they can all do, and none of them has to know that a recording
means two files in particular places.

Core owns the session. An engine is handed the paths it writes to and never
chooses them: only core knows which project is open, and an engine picking its
own would write somewhere the project never hears about -- data on disk that no
manifest mentions and nothing can find again.

    admet = Admet()
    admet.open_project("runs/today.admetp")
    admet.run("acquisition", "connect_fluidics", {"simulated": True})
    admet.run("acquisition", "start_recording", {"recording_label": "set01"})
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from admet.core.engine import ActionSpec, action_spec
from admet.core.project import ProjectStore
from admet.core.run import RunJob, RunResult
from admet.core.session import PROJECT_EXTENSION
from admet.workflows.operations import operation as find_operation

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


class NoProject(Exception):
    """An action needed somewhere to write, and no project was open."""


class Admet:
    """Sessions, and the engines that fill them."""

    def __init__(self, *, project: str | Path | None = None):
        self._engines: dict[str, Any] = {}
        self._marks: dict[str, Any] = {}
        self.project: ProjectStore | None = None
        # Attached by whoever provides them. Absent, observe reports honestly
        # that nothing is publishing and nothing is running.
        self._runtime: Any | None = None
        self._validation: Any | None = None
        self._safety: dict[str, Any] = {
            "armed": False,
            "tripped": False,
            "reason": "",
            "at": None,
            "limits": {},
        }
        if project is not None:
            self.open_project(project)

    # -- sessions -----------------------------------------------------------
    def create_project(self, path: str | Path, project_id: str = "") -> ProjectStore:
        """Start a new project, and make it the one runs write into."""
        path = Path(path)
        self.project = ProjectStore.create(path, project_id or path.stem)
        return self.project

    def open_project(self, path: str | Path) -> ProjectStore:
        """Open an existing project, or say plainly that it is not one."""
        path = Path(path)
        if not (path / "manifest.json").is_file():
            raise NoProject(f"{path} is not a project: no manifest.json in it")
        self.project = ProjectStore(path)
        return self.project

    def close_project(self) -> None:
        self.project = None

    def describe_project(self) -> dict[str, Any]:
        if self.project is None:
            return {"open": False}
        session = self.project.session
        return {
            "open": True,
            "path": str(self.project.path),
            "project_id": session.project_id,
            "updated_at": session.updated_at,
            "files": [
                {"id": file.id, "role": file.role, "path": file.path} for file in session.files
            ],
            "items": [{"id": item.id, "type": item.project_type} for item in session.items],
        }

    @staticmethod
    def discover_projects(root: str | Path) -> list[dict[str, Any]]:
        from admet.core.discovery import discover_projects, project_ref_label

        return [
            {
                "path": str(ref.path),
                "project_id": ref.project_id,
                "updated": ref.updated,
                "label": project_ref_label(ref),
            }
            for ref in discover_projects(root)
        ]

    # -- engines ------------------------------------------------------------
    def engine_ids(self) -> list[str]:
        """Every engine this build has, whether or not it has been created yet."""
        return list(ENGINE_GROUPS)

    def engine(self, engine_id: str) -> Any:
        """The engine, created on first use. Analysis stacks are slow to import."""
        if engine_id not in self._engines:
            group = ENGINE_GROUPS.get(engine_id)
            if group is None:
                known = ", ".join(sorted(ENGINE_GROUPS))
                raise LookupError(f"unknown engine {engine_id!r}; this build has: {known}")
            from admet.engines import create_engine_registry

            self._engines[engine_id] = create_engine_registry(group).create(engine_id)
        return self._engines[engine_id]

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
            if state in {"completed", "idle"}:
                return "completed"
            if state == "error":
                return "error"
            if self._waiting_for_operator(engine):
                return "waiting"
            time.sleep(poll_s)
        return "timeout"

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
    ANALYSIS_ROLES = ("analysis_video", "analysis_image_dir")

    def add_analysis_source(self, path: Path, *, engine: str, sample_id: str = "") -> dict[str, Any]:
        """Register something to analyse with the open project."""
        if self.project is None:
            raise NoProject("no project is open; create or open one first")
        file = self.project.register_analysis_file(
            path, engine=engine, sample_id=sample_id or Path(path).stem
        )
        self.project.save()
        return {"id": file.id, "role": file.role, "path": file.path, "engine": engine}

    def analysis_sources(self) -> list[dict[str, Any]]:
        if self.project is None:
            return []
        return [
            {
                "id": file.id,
                "role": file.role,
                "path": str(self.project.resolve_file_path(file)),
                "engine": file.metadata.get("engine", ""),
                "sample_id": file.metadata.get("sample_id", ""),
            }
            for file in self.project.files_by_role(self.ANALYSIS_ROLES)
        ]

    def analyze(self, *, engine: str = "", cache: str = "use") -> dict[str, Any]:
        """Analyse everything the project holds, writing the results into it."""
        if self.project is None:
            raise NoProject("no project is open; create or open one first")
        from admet.engines import create_engine_registry
        from admet.workflows.analyze_runner import AnalyzeBatchRunner, AnalyzeTarget

        targets = [
            AnalyzeTarget(
                project_path=self.project.path,
                source_path=Path(source["path"]),
                engine=engine or source["engine"],
                sample_id=source["sample_id"],
                cache_policy=cache,
            )
            for source in self.analysis_sources()
        ]
        report = AnalyzeBatchRunner(create_engine_registry("analyze")).run(targets)
        self.project = ProjectStore(self.project.path)
        return {
            "projects": len(report.projects),
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
    def state(self) -> dict[str, Any]:
        """What is true right now, as the guards understand it."""
        engine = self._engines.get("acquisition")
        if engine is None:
            return {
                "project": self.project is not None,
                "fluidics": False,
                "camera": False,
                "corrections": False,
                "running": False,
                "sources": len(self.analysis_sources()),
            }
        return {
            "project": self.project is not None,
            "fluidics": bool(engine.hardware.state.connected),
            "camera": bool(getattr(engine.camera, "connected", False)),
            "corrections": self._marks.get("corrections", False),
            "running": engine.pipeline_state in {"running", "paused", "stopping"},
            "sources": len(self.analysis_sources()),
        }

    def runtime_state(self) -> dict[str, Any]:
        """What the serving process is publishing, if it is publishing."""
        if self._runtime is None:
            return {"publishing": False, "path": None, "heartbeat": None, "pid": None}
        return self._runtime.describe()

    def validation_state(self) -> dict[str, Any]:
        """The validation run in progress, if there is one."""
        if self._validation is None:
            return {
                "active": False,
                "id": None,
                "state": "idle",
                "configuration": None,
                "current_target_ul_min": None,
                "artifacts": {},
                "error": "",
                "classification": None,
            }
        return self._validation.describe()

    def safety_state(self) -> dict[str, Any]:
        """Whether a limit is armed, and whether anything has tripped it.

        A trip latches: it stays reported until it is explicitly reset, because
        a safety event that clears itself is one nobody finds out about.
        """
        return dict(self._safety)

    def protocol_events(self, *, after_sequence: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Protocol events newer than one already seen, as plain data."""
        engine = self._engines.get("acquisition")
        if engine is None:
            return []
        return [_describe_event(event) for event in engine.events_after(after_sequence, limit)]

    def mark(self, name: str, value: Any) -> None:
        """Remember something the hardware does not report, such as corrections."""
        self._marks[name] = value

    def engine_action(self, engine_id: str, action: str, settings: dict[str, Any]):
        """An engine primitive, for operations only."""
        return self.run(engine_id, action, settings)

    def run_steps(self, steps: list[Any], *, tick_s: float = 0.2):
        engine = self.engine("acquisition")
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

    def _job_metadata(self) -> dict[str, Any]:
        if self.project is None:
            return {}
        return {
            "session_id": self.project.session.project_id,
            "workdir": str(self.project.path),
        }

    def _allocate_outputs(self, job: RunJob, spec: ActionSpec) -> RunJob:
        """Decide where this action's files go, before it is allowed to write."""
        if not spec.outputs:
            return job
        if self.project is None:
            raise NoProject(
                f"{spec.id} writes {', '.join(spec.outputs)} and needs a project open; "
                f"create or open one ending in {PROJECT_EXTENSION}"
            )
        label = str(job.settings.get("recording_label") or job.id)
        target = self.project.control_recording_target(label)
        missing = set(spec.outputs) - set(target.outputs)
        if missing:
            raise LookupError(
                f"{spec.id} declares outputs the project cannot place: {', '.join(sorted(missing))}"
            )
        metadata = {
            **job.metadata,
            "recording_id": target.recording_id,
            "recording_label": label,
        }
        outputs = {name: target.outputs[name] for name in spec.outputs}
        return replace(job, outputs=outputs, metadata=metadata)

    def _register(self, spec: ActionSpec, result: RunResult) -> None:
        """Put what the action produced into the manifest, so it can be found."""
        if not spec.artifact or self.project is None:
            return
        if spec.artifact == "control_recording":
            recording = result.metadata.get("recording")
            if recording:
                self.project.append_control_recording(recording)
            return
        raise LookupError(f"{spec.id} declares an artifact core cannot store: {spec.artifact!r}")
