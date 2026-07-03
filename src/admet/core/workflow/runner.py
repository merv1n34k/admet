from __future__ import annotations

from dataclasses import replace
from typing import Any

from admet.core.engine import Engine, action_spec
from admet.core.run import RunJob, RunResult

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
    ) -> tuple[WorkflowState, RunResult | None]:
        if state.paused:
            raise RuntimeError("workflow is paused")

        stage = self.workflow.current_stage(state)
        result = None
        if stage.action is not None:
            action_spec(self.engine.actions, stage.action)
            result = self.engine.run(
                RunJob(
                    id=f"{self.workflow.id}-{stage.id}",
                    engine=self.engine.id,
                    action=stage.action,
                    settings=settings,
                    metadata={"workflow": self.workflow.id, "stage": stage.id},
                )
            )
            data = dict(state.data)
            data[stage.id] = result
            state = replace(state, data=data)

        return self.workflow.complete_current(state, confirmed=confirmed), result
