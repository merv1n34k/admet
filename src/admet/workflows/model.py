from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from admet.core.engine import ParamSchema


class StageStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETE = "complete"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class StageAction:
    label: str
    action: str | None = None
    off_action: str | None = None
    guard: str = ""
    active_when: str = ""
    kind: str = "button"
    advances: bool = False
    completes: bool = False
    skippable: bool = False
    variant: str = "primary"


StageControl = StageAction


@dataclass(frozen=True)
class StageSurface:
    kind: str
    title: str = ""
    settings: ParamSchema = field(default_factory=ParamSchema)
    controls: tuple[StageAction, ...] = ()
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EditorSpec:
    kind: str
    persistent: bool = False
    surfaces: tuple[StageSurface, ...] = ()
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SettingsSpec:
    kind: str = "params"
    schema: ParamSchema = field(default_factory=ParamSchema)
    title: str = "Settings"
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResultsSpec:
    kind: str = "table"
    title: str = "Results"
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LogSpec:
    kind: str = "table"
    title: str = "Action Log"
    options: dict[str, Any] = field(default_factory=dict)


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
    settings_panel: SettingsSpec | None = None
    actions: tuple[StageAction, ...] = ()
    controls: tuple[StageAction, ...] = ()
    editor: EditorSpec | None = None
    results: ResultsSpec = field(default_factory=ResultsSpec)
    log: LogSpec = field(default_factory=LogSpec)
    surfaces: tuple[StageSurface, ...] = ()
    show_settings: bool = True
    settings_options: dict[str, Any] = field(default_factory=dict)
    pipeline: bool = False
    completion_gate: str = ""

    def __post_init__(self) -> None:
        if self.actions and not self.controls:
            object.__setattr__(self, "controls", self.actions)
        elif self.controls and not self.actions:
            object.__setattr__(self, "actions", self.controls)
        if self.settings_panel is None:
            object.__setattr__(self, "settings_panel", SettingsSpec(schema=self.settings))
        elif self.settings is not self.settings_panel.schema and not self.settings.params:
            object.__setattr__(self, "settings", self.settings_panel.schema)
        if self.editor is not None and not self.surfaces:
            object.__setattr__(self, "surfaces", self.editor.surfaces)


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
