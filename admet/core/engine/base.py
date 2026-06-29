from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .actions import ActionSpec
from admet.core.schema import ParamSchema, ResultSet
from admet.core.session import AdmetSession


@dataclass(frozen=True)
class EngineContext:
    workdir: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    session: AdmetSession | None = None


@dataclass(frozen=True)
class EngineResult:
    result_set: ResultSet
    artifacts: dict[str, Any] = field(default_factory=dict)


class Engine(Protocol):
    id: str
    name: str
    settings: ParamSchema
    actions: tuple[ActionSpec, ...]

    def run_action(
        self,
        action: str,
        settings: dict[str, Any],
        context: EngineContext | None = None,
    ) -> EngineResult:
        ...
