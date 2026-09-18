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

from dataclasses import replace
from pathlib import Path
from typing import Any

from admet.core.engine import ActionSpec, action_spec
from admet.core.project import ProjectStore
from admet.core.run import RunJob, RunResult
from admet.core.session import PROJECT_EXTENSION

# Which engine registry each engine belongs to.
ENGINE_GROUPS = {
    "acquisition": "control",
    "opencv": "analyze",
    "cellpose": "analyze",
}


class NoProject(Exception):
    """An action needed somewhere to write, and no project was open."""


class Admet:
    """Sessions, and the engines that fill them."""

    def __init__(self, *, project: str | Path | None = None):
        self._engines: dict[str, Any] = {}
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

    # -- running ------------------------------------------------------------
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
