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


def _fill_defaults(operation: Any, settings: dict[str, Any]) -> dict[str, Any]:
    """Validate what was supplied and fill in the rest from the operation."""
    known = {param.name: param for param in operation.params}
    unknown = set(settings) - set(known)
    if unknown:
        offered = ", ".join(sorted(known)) or "nothing"
        raise LookupError(
            f"{operation.id} has no setting {', '.join(sorted(unknown))}; it takes: {offered}"
        )
    return {
        name: param.validate(settings.get(name, param.default)) for name, param in known.items()
    }


class NoProject(Exception):
    """An action needed somewhere to write, and no project was open."""


class Admet:
    """Sessions, and the engines that fill them."""

    def __init__(self, *, project: str | Path | None = None):
        self._engines: dict[str, Any] = {}
        self._marks: dict[str, Any] = {}
        self.project: ProjectStore | None = None
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

    def describe(self, engine_id: str) -> dict[str, Any]:
        engine = self.engine(engine_id)
        return {
            "engine": {"id": engine.id, "name": engine.name},
            "actions": [
                {
                    "id": action.id,
                    "label": action.label,
                    "category": action.category,
                    "params": list(action.params),
                    "outputs": list(action.outputs),
                    "artifact": action.artifact,
                    "destructive": action.destructive,
                }
                for action in engine.actions
            ],
            "settings": [param.name for param in engine.settings.params],
        }

    # -- operations ---------------------------------------------------------
    def operations(self) -> list[dict[str, Any]]:
        """Everything that can be asked for, with its parameters and its guards."""
        from admet.workflows.operations import OPERATIONS

        return [
            {
                "id": op.id,
                "label": op.label,
                "description": op.description,
                "requires": list(op.requires),
                "params": [
                    {
                        "name": param.name,
                        "kind": param.kind.value,
                        "default": param.default,
                        "minimum": param.minimum,
                        "maximum": param.maximum,
                        "description": param.description or param.label,
                    }
                    for param in op.params
                ],
            }
            for op in OPERATIONS
        ]

    def do(self, operation_id: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """Carry out one operation, if now is a moment it makes sense.

        This is the way in. Engine actions are hardware primitives and are not
        reachable from outside: an operation is the same primitive plus the
        conditions under which using it is not a mistake.
        """
        from admet.workflows.operations import operation as find_operation

        op = find_operation(operation_id)
        op.check(self.state())
        settings = _fill_defaults(op, dict(settings or {}))
        return op.run(self, settings)

    # -- pipelines -----------------------------------------------------------
    def pipelines(self) -> list[dict[str, Any]]:
        from admet.workflows.pipelines import PIPELINES

        return [
            {
                "id": line.id,
                "label": line.label,
                "description": line.description,
                "params": [
                    {"name": p.name, "kind": p.kind.value, "default": p.default} for p in line.params
                ],
            }
            for line in PIPELINES
        ]

    def plan(self, pipeline_id: str, settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """The stages a pipeline would run, without running any of them."""
        from admet.workflows.pipelines import pipeline as find_pipeline

        line = find_pipeline(pipeline_id)
        bound = _fill_defaults(line, dict(settings or {}))
        return [
            {"index": index, "operation": stage.operation, "label": stage.described()}
            for index, stage in enumerate(line.stages(bound))
        ]

    def run_pipeline(
        self,
        pipeline_id: str,
        settings: dict[str, Any] | None = None,
        *,
        from_stage: int = 0,
        wait_s: float = 600.0,
    ) -> dict[str, Any]:
        """Run the stages in order, waiting for each protocol to finish.

        Stops at the first stage that is refused, that fails, or that reaches a
        step needing the operator -- and says which, so the run can be picked up
        from there once it has been dealt with.
        """
        from admet.workflows.pipelines import pipeline as find_pipeline

        line = find_pipeline(pipeline_id)
        bound = _fill_defaults(line, dict(settings or {}))
        stages = line.stages(bound)
        report: list[dict[str, Any]] = []

        for index, stage in enumerate(stages):
            if index < from_stage:
                report.append({"index": index, "operation": stage.operation, "outcome": "skipped"})
                continue
            entry: dict[str, Any] = {"index": index, "operation": stage.operation}
            try:
                entry["result"] = self.do(stage.operation, stage.settings)
            except Exception as exc:
                entry.update(outcome="refused", reason=f"{type(exc).__name__}: {exc}")
                report.append(entry)
                return {"pipeline": line.id, "completed": False, "stopped_at": index, "stages": report}

            if find_operation(stage.operation).starts_protocol:
                outcome = self.wait_for_protocol(timeout_s=wait_s)
                entry["outcome"] = outcome
                report.append(entry)
                if outcome != "completed":
                    return {
                        "pipeline": line.id,
                        "completed": False,
                        "stopped_at": index,
                        "reason": outcome,
                        "stages": report,
                    }
                continue

            entry["outcome"] = "completed"
            report.append(entry)

        return {"pipeline": line.id, "completed": True, "stages": report}

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
