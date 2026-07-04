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
class NoticeVM:
    message: str
    kind: str = "primary"


@dataclass(frozen=True)
class ProjectBarVM:
    title: str
    selected: str
    options: dict[str, str]
    root: str
    can_create: bool
    can_load: bool
    can_save: bool


@dataclass(frozen=True)
class ActionBoxVM:
    buttons: tuple[ButtonVM, ...]
    progress: float
    label: str


@dataclass(frozen=True)
class SettingsTableVM:
    mode: str
    fields: tuple[FieldVM, ...]
    all_fields: tuple[FieldVM, ...]
    rows: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ResultsVM:
    title: str
    rows: tuple[dict[str, Any], ...]
    empty_message: str


@dataclass(frozen=True)
class ScreenModel:
    workflow_id: str
    steps: tuple[StepVM, ...]
    title: str
    description: str
    instructions: tuple[str, ...]
    notice: NoticeVM | None
    project: ProjectBarVM
    action_box: ActionBoxVM
    settings: SettingsTableVM
    surfaces: tuple[SurfaceVM, ...]
    results: ResultsVM
    log: tuple[str, ...]

    @property
    def fields(self) -> tuple[FieldVM, ...]:
        return self.settings.fields

    @property
    def buttons(self) -> tuple[ButtonVM, ...]:
        return self.action_box.buttons


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
    settings = _settings_table_for_stage(stage, presenter_runtime)
    return ScreenModel(
        workflow_id=workflow.id,
        steps=_steps_for_workflow(workflow, state),
        title=stage.label,
        description=stage.description,
        instructions=presenter_runtime.instructions_for(stage),
        notice=_notice_for_runtime(presenter_runtime),
        project=_project_for_runtime(workflow, presenter_runtime),
        action_box=ActionBoxVM(
            buttons=buttons,
            progress=_progress_for_stage(state, stage),
            label=stage.label,
        ),
        settings=settings,
        surfaces=tuple(_surface_vm(surface, presenter_runtime) for surface in stage.surfaces),
        results=_results_for_runtime(workflow, presenter_runtime),
        log=_log_for_runtime(presenter_runtime),
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
    return tuple(_field_vm(field, runtime) for field in _ordered_params(stage.settings.params, stage.settings_options))


def _settings_table_for_stage(stage: Stage, runtime: PresenterRuntime) -> SettingsTableVM:
    if not stage.show_settings:
        return SettingsTableVM(mode="settings", fields=(), all_fields=())
    ordered = tuple(_ordered_params(stage.settings.params, stage.settings_options))
    visible = tuple(_collapsed_params(ordered, stage.settings_options))
    return SettingsTableVM(
        mode=str(stage.settings_options.get("mode") or "settings"),
        fields=tuple(_field_vm(field, runtime) for field in visible),
        all_fields=tuple(_field_vm(field, runtime) for field in ordered),
    )


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


def _ordered_params(params: tuple[Param, ...], options: dict[str, Any]) -> list[Param]:
    primary = _named_params(params, options.get("primary", ()))
    secondary = _named_params(params, options.get("secondary", ()))
    ordered_names = {param.name for param in (*primary, *secondary)}
    return [*primary, *secondary, *(param for param in params if param.name not in ordered_names)]


def _collapsed_params(params: tuple[Param, ...], options: dict[str, Any]) -> list[Param]:
    collapsed_count = int(options.get("collapsed_count") or 0)
    if collapsed_count > 0:
        return list(params[:collapsed_count])
    if len(params) > 6:
        return list(params[:6])
    return list(params)


def _named_params(params: tuple[Param, ...], names: Any) -> list[Param]:
    by_name = {param.name: param for param in params}
    return [by_name[name] for name in tuple(names or ()) if name in by_name]


def _notice_for_runtime(runtime: PresenterRuntime) -> NoticeVM | None:
    message = str(_runtime_value(runtime, ("notice", "status_text"), "") or "")
    if not message:
        return None
    kind = str(_runtime_value(runtime, ("notice_kind", "status_kind"), "primary") or "primary")
    return NoticeVM(message=message, kind=kind)


def _project_for_runtime(workflow: Workflow, runtime: PresenterRuntime) -> ProjectBarVM:
    options = _runtime_call(runtime, "project_options", {})
    if not isinstance(options, dict):
        options = {}
    selected = str(_runtime_value(runtime, ("project_path",), "") or "")
    root = str(_runtime_value(runtime, ("discovery_root",), "") or "")
    label = _runtime_call(runtime, "project_label", None)
    if not label:
        project_text = _runtime_call(runtime, "_project_text", None)
        label = project_text or selected or "Project: none"
    return ProjectBarVM(
        title=str(label),
        selected=selected,
        options={str(key): str(value) for key, value in options.items()},
        root=root,
        can_create=hasattr(runtime, "new_project") or hasattr(runtime, "_new_project"),
        can_load=hasattr(runtime, "load_project") or hasattr(runtime, "_load_project"),
        can_save=hasattr(runtime, "save_project") or hasattr(runtime, "_save_project"),
    )


def _results_for_runtime(workflow: Workflow, runtime: PresenterRuntime) -> ResultsVM:
    rows = _runtime_call(runtime, "result_rows", [])
    if not isinstance(rows, list):
        rows = []
    empty_message = (
        "No completed analysis jobs yet."
        if workflow.id == "analyze"
        else "No stored recordings yet."
    )
    return ResultsVM(
        title="Results",
        rows=tuple(dict(row) for row in rows),
        empty_message=empty_message,
    )


def _log_for_runtime(runtime: PresenterRuntime) -> tuple[str, ...]:
    lines = _runtime_value(runtime, ("action_log", "_log_lines"), ())
    return tuple(str(line) for line in tuple(lines or ())[-8:])


def _progress_for_stage(state: WorkflowState, stage: Stage) -> float:
    status = state.statuses.get(stage.id, StageStatus.PENDING)
    if status is StageStatus.COMPLETE:
        return 1.0
    if status is StageStatus.ACTIVE:
        return 0.5
    return 0.0


def _runtime_value(runtime: PresenterRuntime, names: tuple[str, ...], default: Any) -> Any:
    for name in names:
        if hasattr(runtime, name):
            return getattr(runtime, name)
    return default


def _runtime_call(runtime: PresenterRuntime, name: str, default: Any) -> Any:
    value = getattr(runtime, name, None)
    if callable(value):
        return value()
    return default


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
