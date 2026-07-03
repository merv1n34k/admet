from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .actions import ActionSpec
from admet.core.schema import ParamSchema


class RunSink(Protocol):
    def write(self, row: dict[str, Any]) -> None:
        ...


@dataclass(frozen=True)
class RunJob:
    id: str
    engine: str
    action: str
    settings: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Path] = field(default_factory=dict)
    outputs: dict[str, Path] = field(default_factory=dict)
    cache_dir: Path | None = None
    sink: RunSink | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunResult:
    job_id: str
    engine: str
    action: str
    status: str = "complete"
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class Engine(Protocol):
    id: str
    name: str
    settings: ParamSchema
    actions: tuple[ActionSpec, ...]

    def run(self, job: RunJob) -> RunResult:
        ...
