from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from admet.core.engine import Engine, EngineContext, EngineResult
from admet.core.schema import ResultRecord, ResultSet, SummaryStat


@dataclass(frozen=True)
class BatchItem:
    settings: dict[str, Any]
    item_id: str | None = None
    context: EngineContext | None = None
    params: dict[str, Any] = field(default_factory=dict)


class BatchRunner:
    def __init__(
        self,
        engine: Engine,
        items: list[BatchItem],
        *,
        action: str = "analyze",
        context: EngineContext | None = None,
        item_started: Callable[[str], None] | None = None,
        item_progress: Callable[[str, int, str], None] | None = None,
        item_finished: Callable[[str, EngineResult, dict[str, Any]], None] | None = None,
        item_failed: Callable[[str, str], None] | None = None,
    ):
        self.engine = engine
        self.items = items
        self.action = action
        self.context = context
        self.item_started = item_started
        self.item_progress = item_progress
        self.item_finished = item_finished
        self.item_failed = item_failed

    def run(self) -> EngineResult:
        records: list[ResultRecord] = []
        outcomes: list[dict[str, Any]] = []
        artifacts: dict[str, Any] = {}

        for index, item in enumerate(self.items):
            item_id = item.item_id or _item_id(item.settings, index)
            if self.item_started:
                self.item_started(item_id)
            try:
                result = self.engine.run_action(
                    self.action,
                    dict(item.settings),
                    item.context or self.context,
                )
                outcome = {
                    "item_id": item_id,
                    "status": "finished",
                    "params": dict(item.params),
                    "metadata": dict(result.result_set.metadata),
                }
                records.extend(_batch_records(item_id, result.result_set.records, self.engine.id))
                if not result.result_set.records:
                    records.append(_status_record(item_id, self.engine.id, "finished"))
                outcomes.append(outcome)
                artifacts[item_id] = dict(result.artifacts)
                if self.item_finished:
                    self.item_finished(item_id, result, outcome)
            except Exception as exc:
                message = str(exc)
                outcome = {
                    "item_id": item_id,
                    "status": "failed",
                    "params": dict(item.params),
                    "error": message,
                }
                records.append(_status_record(item_id, self.engine.id, "failed", error=message))
                outcomes.append(outcome)
                if self.item_failed:
                    self.item_failed(item_id, message)

        finished = sum(1 for outcome in outcomes if outcome["status"] == "finished")
        failed = sum(1 for outcome in outcomes if outcome["status"] == "failed")
        result_set = ResultSet(
            records=tuple(records),
            stats=(
                SummaryStat("batch_total", len(outcomes)),
                SummaryStat("batch_finished", finished),
                SummaryStat("batch_failed", failed),
            ),
            metadata={
                "action": self.action,
                "engine": self.engine.id,
                "items": outcomes,
            },
        )
        return EngineResult(result_set=result_set, artifacts={"items": artifacts})

    def progress(self, item_id: str, percent: int, message: str) -> None:
        if self.item_progress:
            self.item_progress(item_id, percent, message)


def _batch_records(
    item_id: str,
    records: tuple[ResultRecord, ...],
    engine_id: str,
) -> list[ResultRecord]:
    batched = []
    for record in records:
        values = dict(record.values)
        values.setdefault("batch_item_id", item_id)
        batched.append(
            ResultRecord(
                sample_id=record.sample_id or item_id,
                engine=record.engine or engine_id,
                values=values,
            )
        )
    return batched


def _status_record(
    item_id: str,
    engine_id: str,
    status: str,
    *,
    error: str = "",
) -> ResultRecord:
    values = {"batch_item_id": item_id, "status": status}
    if error:
        values["error"] = error
    return ResultRecord(sample_id=item_id, engine=engine_id, values=values)


def _item_id(settings: dict[str, Any], index: int) -> str:
    for key in ("sample_id", "video_id"):
        value = settings.get(key)
        if value:
            return str(value)
    for key in ("video_path", "input_dir"):
        value = settings.get(key)
        if value:
            path = Path(str(value))
            return path.stem if key == "video_path" else path.name
    return f"item_{index + 1}"
