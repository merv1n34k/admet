from __future__ import annotations

from dataclasses import replace
from typing import Any

from admet.core.engine import Engine, EngineContext, EngineResult, validate_action_settings

from .model import Workflow, WorkflowState


class WorkflowRunner:
    def __init__(self, workflow: Workflow, engine: Engine) -> None:
        self.workflow = workflow
        self.engine = engine

    def run_current(
        self,
        state: WorkflowState,
        settings: dict[str, Any],
        *,
        confirmed: bool = False,
        context: EngineContext | None = None,
    ) -> tuple[WorkflowState, EngineResult | None]:
        if state.paused:
            raise RuntimeError("workflow is paused")

        stage = self.workflow.current_stage(state)
        result = None
        if stage.action is not None:
            validated = validate_action_settings(self.engine.settings, self.engine.actions, stage.action, settings)
            result = self.engine.run_action(stage.action, validated, context)
            data = dict(state.data)
            data[stage.id] = result
            state = replace(state, data=data)

        return self.workflow.complete_current(state, confirmed=confirmed), result
