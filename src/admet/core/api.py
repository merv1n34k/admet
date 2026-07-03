from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from admet.core.engine import Engine, RunJob, RunResult
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

    def run(self, job: RunJob) -> RunResult:
        if job.engine != self.engine.id:
            raise ValueError(f"job engine {job.engine!r} does not match {self.engine.id!r}")
        metadata = dict(job.metadata)
        if self.session is not None:
            metadata.setdefault("session_id", self.session.project_id)
        if self.workdir is not None:
            metadata.setdefault("workdir", self.workdir)
        if metadata != job.metadata:
            job = replace(job, metadata=metadata)
        return self.engine.run(job)
