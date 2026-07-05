from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


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
    progress: Callable[[int, str], None] | None = None


@dataclass(frozen=True)
class RunResult:
    job_id: str
    engine: str
    action: str
    status: str = "complete"
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class JsonlRunSink:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        self.row_count = 0

    def write(self, row: dict[str, Any]) -> None:
        self._handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        self.row_count += 1

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> JsonlRunSink:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
