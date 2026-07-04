from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from admet.core.engine import Param, ParamKind, ParamOption
from admet.workflows import (
    Stage,
    StageControl,
    StageStatus,
    StageSurface,
    Workflow,
    WorkflowState,
)


@dataclass(frozen=True)
class FieldVM:
    name: str
    label: str
    kind: ParamKind
    value: Any
    minimum: float | None
    maximum: float | None
    options: tuple[ParamOption, ...]


@dataclass(frozen=True)
class ButtonVM:
    label: str
    command: str
    variant: str
    enabled: bool
    active: bool


@dataclass(frozen=True)
class SurfaceVM:
    kind: str
    title: str
    fields: tuple[FieldVM, ...]
    buttons: tuple[ButtonVM, ...]
    options: dict[str, Any]


@dataclass(frozen=True)
class StepVM:
    id: str
    label: str
    status: StageStatus
    current: bool


@dataclass(frozen=True)
class ScreenModel:
    workflow_id: str
    steps: tuple[StepVM, ...]
    title: str
    description: str
    instructions: tuple[str, ...]
    fields: tuple[FieldVM, ...]
    surfaces: tuple[SurfaceVM, ...]
    buttons: tuple[ButtonVM, ...]


class PresenterRuntime(Protocol):
    def value_for(self, field: Param) -> Any: ...

    def button_enabled(self, command: str) -> bool: ...

    def button_active(self, command: str) -> bool: ...

    def instructions_for(self, stage: Stage) -> tuple[str, ...]: ...


class DefaultPresenterRuntime:
    def value_for(self, field: Param) -> Any:
        return field.default

    def button_enabled(self, command: str) -> bool:
        return True

    def button_active(self, command: str) -> bool:
        return False

    def instructions_for(self, stage: Stage) -> tuple[str, ...]:
        if stage.instructions:
            return stage.instructions
        if stage.description:
            return (stage.description,)
        return ()


def build_screen(
    workflow: Workflow,
    state: WorkflowState,
    runtime: PresenterRuntime | None = None,
) -> ScreenModel:
    presenter_runtime = runtime or DefaultPresenterRuntime()
    stage = workflow.current_stage(state)
    buttons = _buttons_for_stage(stage, state, presenter_runtime)
    return ScreenModel(
        workflow_id=workflow.id,
        steps=_steps_for_workflow(workflow, state),
        title=stage.label,
        description=stage.description,
        instructions=presenter_runtime.instructions_for(stage),
        fields=_fields_for_stage(stage, presenter_runtime),
        surfaces=tuple(_surface_vm(surface, presenter_runtime) for surface in stage.surfaces),
        buttons=buttons,
    )


def _steps_for_workflow(workflow: Workflow, state: WorkflowState) -> tuple[StepVM, ...]:
    return tuple(
        StepVM(
            id=stage.id,
            label=stage.label,
            status=state.statuses.get(stage.id, StageStatus.PENDING),
            current=index == state.index,
        )
        for index, stage in enumerate(workflow.stages)
    )


def _fields_for_stage(
    stage: Stage,
    runtime: PresenterRuntime,
) -> tuple[FieldVM, ...]:
    if not stage.show_settings:
        return ()
    return tuple(_field_vm(field, runtime) for field in stage.settings.params)


def _surface_vm(surface: StageSurface, runtime: PresenterRuntime) -> SurfaceVM:
    return SurfaceVM(
        kind=surface.kind,
        title=surface.title,
        fields=tuple(_field_vm(field, runtime) for field in surface.settings.params),
        buttons=tuple(_button_vm(control, runtime) for control in surface.controls),
        options=dict(surface.options),
    )


def _field_vm(field: Param, runtime: PresenterRuntime) -> FieldVM:
    return FieldVM(
        name=field.name,
        label=field.label,
        kind=field.kind,
        value=runtime.value_for(field),
        minimum=field.minimum,
        maximum=field.maximum,
        options=field.options,
    )


def _buttons_for_stage(
    stage: Stage,
    state: WorkflowState,
    runtime: PresenterRuntime,
) -> tuple[ButtonVM, ...]:
    buttons = [_button_vm(control, runtime) for control in stage.controls]
    if state.index > 0:
        buttons.insert(
            0,
            ButtonVM(
                label="Back",
                command="back",
                variant="secondary",
                enabled=runtime.button_enabled("back"),
                active=runtime.button_active("back"),
            ),
        )
    if stage.skippable and not any(button.command == "skip" for button in buttons):
        buttons.append(
            ButtonVM(
                label="Skip",
                command="skip",
                variant="secondary",
                enabled=runtime.button_enabled("skip"),
                active=runtime.button_active("skip"),
            )
        )
    return tuple(buttons)


def _button_vm(control: StageControl, runtime: PresenterRuntime) -> ButtonVM:
    command = _control_command(control)
    return ButtonVM(
        label=control.label,
        command=command,
        variant=control.variant,
        enabled=runtime.button_enabled(command),
        active=runtime.button_active(command),
    )


def _control_command(control: StageControl) -> str:
    if control.action:
        return control.action
    if control.completes:
        return "complete"
    if control.skippable:
        return "skip"
    if control.advances:
        return "advance"
    return _label_command(control.label)


def _label_command(label: str) -> str:
    return "_".join(label.strip().lower().split())
