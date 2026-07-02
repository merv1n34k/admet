from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from admet.core.engine import Engine, EngineContext, EngineResult, RunJob, RunResult
from admet.core.schema import ParamSchema
from admet.core.session import AdmetSession


@dataclass
class AdmetAPI:
    engine: Engine
    session: AdmetSession | None = None
    workdir: str | None = None

    @property
    def id(self) -> str:
        return self.engine.id

    @property
    def name(self) -> str:
        return self.engine.name

    @property
    def settings(self) -> ParamSchema:
        return self.engine.settings

    def describe(self) -> dict[str, Any]:
        return {
            "engine": {
                "id": self.engine.id,
                "name": self.engine.name,
            },
            "settings": [param.name for param in self.engine.settings.params],
            "actions": [asdict(action) for action in self.engine.actions],
            "session": self.session.project_id if self.session else None,
        }

    def run_action(
        self,
        action: str,
        settings: dict[str, Any],
        context: EngineContext | None = None,
    ) -> EngineResult:
        context = context or EngineContext()
        if context.session is None and self.session is not None:
            context = EngineContext(
                workdir=context.workdir,
                metadata=context.metadata,
                session=self.session,
            )
        if context.workdir is None and self.workdir is not None:
            context = EngineContext(
                workdir=self.workdir,
                metadata=context.metadata,
                session=context.session,
            )
        return self.engine.run_action(action, settings, context)

    def run(self, job: RunJob) -> RunResult:
        if job.engine != self.engine.id:
            raise ValueError(f"job engine {job.engine!r} does not match {self.engine.id!r}")
        return self.engine.run(job)
