from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from admet.core.schema import ParamSchema


class StageStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETE = "complete"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class StageControl:
    label: str
    action: str | None = None
    advances: bool = False
    completes: bool = False
    skippable: bool = False
    variant: str = "primary"


@dataclass(frozen=True)
class Stage:
    id: str
    label: str
    action: str | None = None
    skippable: bool = False
    confirmation_required: bool = False
    description: str = ""
    instructions: tuple[str, ...] = ()
    settings: ParamSchema = field(default_factory=ParamSchema)
    controls: tuple[StageControl, ...] = ()


@dataclass(frozen=True)
class WorkflowState:
    index: int = 0
    statuses: dict[str, StageStatus] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    paused: bool = False


class Workflow:
    def __init__(self, workflow_id: str, label: str, stages: tuple[Stage, ...]) -> None:
        if not stages:
            raise ValueError("workflow requires at least one stage")
        ids = [stage.id for stage in stages]
        if len(ids) != len(set(ids)):
            raise ValueError("stage ids must be unique")
        self.id = workflow_id
        self.label = label
        self.stages = stages

    def initial_state(self) -> WorkflowState:
        statuses = {stage.id: StageStatus.PENDING for stage in self.stages}
        statuses[self.stages[0].id] = StageStatus.ACTIVE
        return WorkflowState(statuses=statuses)

    def current_stage(self, state: WorkflowState) -> Stage:
        return self.stages[state.index]

    def complete_current(self, state: WorkflowState, *, confirmed: bool = False) -> WorkflowState:
        stage = self.current_stage(state)
        if stage.confirmation_required and not confirmed:
            raise PermissionError(f"{stage.id} requires confirmation")
        statuses = dict(state.statuses)
        statuses[stage.id] = StageStatus.COMPLETE
        return self._move_to_next_open(replace(state, statuses=statuses))

    def skip_current(self, state: WorkflowState) -> WorkflowState:
        stage = self.current_stage(state)
        if not stage.skippable:
            raise ValueError(f"{stage.id} is not skippable")
        statuses = dict(state.statuses)
        statuses[stage.id] = StageStatus.SKIPPED
        return self._move_to_next_open(replace(state, statuses=statuses))

    def rewind(self, state: WorkflowState) -> WorkflowState:
        if state.index == 0:
            return state
        statuses = dict(state.statuses)
        statuses[self.current_stage(state).id] = StageStatus.PENDING
        previous_index = state.index - 1
        statuses[self.stages[previous_index].id] = StageStatus.ACTIVE
        return replace(state, index=previous_index, statuses=statuses)

    def pause(self, state: WorkflowState) -> WorkflowState:
        return replace(state, paused=True)

    def resume(self, state: WorkflowState) -> WorkflowState:
        return replace(state, paused=False)

    def _move_to_next_open(self, state: WorkflowState) -> WorkflowState:
        next_index = state.index + 1
        statuses = dict(state.statuses)
        if next_index >= len(self.stages):
            return replace(state, statuses=statuses)
        statuses[self.stages[next_index].id] = StageStatus.ACTIVE
        return replace(state, index=next_index, statuses=statuses)
