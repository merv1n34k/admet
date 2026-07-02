from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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
