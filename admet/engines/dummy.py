from __future__ import annotations

from typing import Any

from admet.core.engine import ActionSpec, EngineContext, EngineResult, validate_action_settings
from admet.core.schema import Param, ParamKind, ParamSchema, ResultRecord, ResultSet, SummaryStat


class DummyEngine:
    id = "dummy"
    name = "Dummy Engine"
    settings = ParamSchema(
        (
            Param("sample_id", "Sample ID", ParamKind.TEXT, default="demo"),
            Param("threshold", "Threshold", ParamKind.FLOAT, default=1.0, minimum=0.0, step=0.1),
        )
    )
    actions = (
        ActionSpec(
            "analyze",
            "Analyze",
            "analysis",
            params=("sample_id", "threshold"),
        ),
    )

    def run_action(
        self,
        action: str,
        settings: dict[str, Any],
        context: EngineContext | None = None,
    ) -> EngineResult:
        normalized = validate_action_settings(self.settings, self.actions, action, settings)
        record = ResultRecord(
            sample_id=normalized["sample_id"],
            engine=self.id,
            values={"action": action, "threshold": normalized["threshold"]},
        )
        return EngineResult(
            result_set=ResultSet(
                records=(record,),
                stats=(SummaryStat("records", 1),),
                metadata={"context": context.metadata if context else {}},
            )
        )


def create_engine() -> DummyEngine:
    return DummyEngine()
