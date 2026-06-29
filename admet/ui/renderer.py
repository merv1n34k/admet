from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import Any

from admet.core.engine import EngineContext, EngineResult
from admet.core.schema import ParamKind, ParamSchema, ResultRecord, ResultSet
from admet.core.workflow import Stage, StageControl, StageStatus, Workflow, WorkflowState


def render_workflow(
    workflow: Workflow,
    state: WorkflowState,
    settings: ParamSchema | None = None,
    *,
    engine: Any | None = None,
) -> None:
    from nicegui import ui

    ui.add_head_html(
        """
        <style>
        body { background: #f7f8fa; }
        .admet-shell { min-height: 100vh; color: #17202a; }
        .admet-topbar { border-bottom: 1px solid #d9dee7; background: #ffffff; }
        .admet-sidebar { border-right: 1px solid #d9dee7; background: #ffffff; }
        .admet-panel { border: 1px solid #d9dee7; background: #ffffff; border-radius: 8px; }
        .admet-muted { color: #677383; }
        .admet-camera { background: #151a21; color: #dfe7f1; aspect-ratio: 16 / 9; }
        .admet-toc-button { min-height: 56px; }
        </style>
        """
    )
    CoreWorkflowView(workflow, state, settings, engine).render()


class CoreWorkflowView:
    def __init__(
        self,
        workflow: Workflow,
        state: WorkflowState,
        settings: ParamSchema | None,
        engine: Any | None,
    ) -> None:
        self.workflow = workflow
        self.state = state
        self.settings = settings
        self.engine = engine
        self.values: dict[str, Any] = {}
        if settings is not None:
            self.values.update(settings.defaults())
        for stage in workflow.stages:
            self.values.update(stage.settings.defaults())
        self.engine_setting_names = {
            param.name for param in settings.params
        } if settings is not None else set()
        self.status = "Ready"
        self.status_kind = "info"
        self.last_result: EngineResult | None = None
        self.batch_files: list[dict[str, Any]] = []
        self._screen: Any | None = None

    def render(self) -> None:
        from nicegui import ui

        @ui.refreshable
        def screen() -> None:
            self._render_screen()

        self._screen = screen
        screen()

    def _render_screen(self) -> None:
        from nicegui import ui

        ui.query("body").classes("m-0")
        with ui.column().classes("admet-shell w-full gap-0"):
            self._render_topbar()
            with ui.row().classes("w-full flex-nowrap gap-0 grow"):
                with ui.column().classes("admet-sidebar w-96 shrink-0 gap-3 p-4"):
                    self._render_workflow_sidebar()
                with ui.column().classes("grow gap-4 p-4"):
                    if self.workflow.id == "control":
                        self._render_camera_panel()
                    self._render_stage_panel()
                    self._render_batch_panel()
                    self._render_results_panel()

    def _render_topbar(self) -> None:
        from nicegui import ui

        with ui.row().classes("admet-topbar w-full items-center justify-between px-4 py-3"):
            with ui.column().classes("gap-0"):
                ui.label(self.workflow.label).classes("text-xl font-semibold")
                engine_name = getattr(self.engine, "name", "No engine")
                ui.label(f"Engine: {engine_name}").classes("admet-muted text-sm")
            with ui.row().classes("items-center gap-2"):
                ui.badge(self.status_kind.upper()).props(_badge_color(self.status_kind))
                ui.label(self.status).classes("text-sm")

    def _render_workflow_sidebar(self) -> None:
        from nicegui import ui

        ui.label("Workflow").classes("text-lg font-semibold")
        for index, stage in enumerate(self.workflow.stages):
            status = self.state.statuses[stage.id]
            selected = index == self.state.index
            with ui.column().classes("gap-2"):
                button = ui.button(
                    stage.label,
                    icon=_status_icon(status),
                    on_click=partial(self._activate_index, index),
                ).props("align=left")
                button.classes(
                    "admet-toc-button w-full justify-start"
                    + (" bg-blue-50 text-blue-800" if selected else "")
                )
                with ui.row().classes("items-center gap-1 pl-2 flex-wrap"):
                    ui.badge(status.value).props(_status_badge(status))
                    if stage.skippable:
                        ui.badge("skippable").props("color=orange")
            if selected:
                self._render_stage_controls(stage)
        with ui.row().classes("gap-2 pt-2"):
            ui.button("Previous", icon="chevron_left", on_click=self._previous_stage).props("outline")
            ui.button("Next", icon="chevron_right", on_click=self._next_stage).props("outline")

    def _render_stage_controls(self, stage: Stage) -> None:
        from nicegui import ui

        controls = stage.controls or self._default_controls(stage)
        with ui.column().classes("gap-2 pl-2"):
            for control in controls:
                props = _button_props(control.variant)
                ui.button(
                    control.label,
                    icon=_control_icon(control),
                    on_click=partial(self._handle_control, stage, control),
                ).props(props).classes("w-full justify-start")

    def _render_camera_panel(self) -> None:
        from nicegui import ui

        metadata = self._latest_metadata()
        cameras = metadata.get("cameras") or metadata.get("camera", {}).get("cameras") or []
        camera_message = metadata.get("camera_message", "Refresh cameras when a device is attached.")
        connected = bool(metadata.get("camera_connected"))

        with ui.column().classes("admet-panel w-full gap-3 p-4"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Camera").classes("text-lg font-semibold")
                with ui.row().classes("gap-2"):
                    ui.button(
                        "Refresh",
                        icon="refresh",
                        on_click=partial(self._run_action, "refresh_cameras", False),
                    ).props("outline")
                    ui.button(
                        "Connect",
                        icon="videocam",
                        on_click=partial(self._run_action, "connect_camera", False),
                    )
            with ui.row().classes("w-full gap-4"):
                with ui.column().classes("admet-camera grow items-center justify-center rounded-md p-4"):
                    ui.icon("videocam").classes("text-5xl")
                    ui.label("Live camera feed").classes("text-lg")
                    ui.label("Preview frames will render here once acquisition is attached.").classes(
                        "text-sm text-center"
                    )
                with ui.column().classes("w-80 gap-2"):
                    ui.badge("connected" if connected else "not connected").props(
                        "color=green" if connected else "color=grey"
                    )
                    ui.label(camera_message).classes("admet-muted text-sm")
                    if cameras:
                        ui.label("Detected cameras").classes("font-medium")
                        for camera in cameras:
                            ui.label(str(camera)).classes("text-sm")

    def _render_stage_panel(self) -> None:
        from nicegui import ui

        stage = self.workflow.current_stage(self.state)
        with ui.column().classes("admet-panel w-full gap-4 p-4"):
            with ui.row().classes("w-full items-start justify-between"):
                with ui.column().classes("gap-1"):
                    ui.label(stage.label).classes("text-xl font-semibold")
                    if stage.description:
                        ui.label(stage.description).classes("admet-muted")
                ui.badge(self.state.statuses[stage.id].value).props(
                    _status_badge(self.state.statuses[stage.id])
                )

            if stage.instructions:
                with ui.column().classes("gap-1"):
                    for instruction in stage.instructions:
                        ui.label(instruction).classes("text-sm")

            if stage.settings.params:
                ui.label("Section Settings").classes("font-semibold")
                render_settings(stage.settings, self.values)

            if self.settings is not None and self.settings.params:
                with ui.expansion("Engine Settings", icon="tune").classes("w-full"):
                    render_settings(self.settings, self.values)

    def _render_batch_panel(self) -> None:
        from nicegui import ui

        if self.workflow.id != "analyze":
            return
        with ui.column().classes("admet-panel w-full gap-3 p-4"):
            ui.label("Batch").classes("text-lg font-semibold")
            ui.upload(
                label="Browse Files",
                multiple=True,
                auto_upload=True,
                on_upload=self._handle_upload,
            ).classes("w-full")
            rows = self.batch_files or [{"name": "No files selected", "size": ""}]
            ui.table(
                columns=[
                    {"name": "name", "label": "File", "field": "name"},
                    {"name": "size", "label": "Size", "field": "size"},
                ],
                rows=rows,
                row_key="name",
            ).classes("w-full")

    def _render_results_panel(self) -> None:
        from nicegui import ui

        result_set = self._latest_result_set()
        with ui.column().classes("admet-panel w-full gap-3 p-4"):
            ui.label("Results").classes("text-lg font-semibold")
            if result_set is None:
                ui.label("No results yet. Run a workflow action to populate this area.").classes(
                    "admet-muted"
                )
                return
            self._render_stats(result_set)
            self._render_plot(result_set)
            self._render_record_table(result_set)

    def _render_stats(self, result_set: ResultSet) -> None:
        from nicegui import ui

        if not result_set.stats:
            return
        with ui.row().classes("w-full gap-2 flex-wrap"):
            for stat in result_set.stats:
                label = f"{stat.value} {stat.unit}".strip()
                with ui.column().classes("border border-gray-200 rounded-md px-3 py-2 gap-0"):
                    ui.label(stat.name.replace("_", " ").title()).classes("admet-muted text-xs")
                    ui.label(label).classes("text-lg font-semibold")

    def _render_plot(self, result_set: ResultSet) -> None:
        from nicegui import ui

        stats = [stat for stat in result_set.stats if isinstance(stat.value, int | float)]
        if not stats:
            return
        ui.echart(
            {
                "tooltip": {},
                "grid": {"left": 48, "right": 16, "top": 24, "bottom": 48},
                "xAxis": {"type": "category", "data": [stat.name for stat in stats]},
                "yAxis": {"type": "value"},
                "series": [
                    {
                        "type": "bar",
                        "data": [stat.value for stat in stats],
                        "itemStyle": {"color": "#2563eb"},
                    }
                ],
            }
        ).classes("w-full h-64")

    def _render_record_table(self, result_set: ResultSet) -> None:
        from nicegui import ui

        rows = [_record_to_row(index, record) for index, record in enumerate(result_set.records)]
        if not rows:
            rows = [{"id": 0, "sample_id": "", "engine": "", "status": "no records"}]
        keys = sorted({key for row in rows for key in row})
        columns = [{"name": key, "label": key.replace("_", " ").title(), "field": key} for key in keys]
        ui.table(columns=columns, rows=rows, row_key="id").classes("w-full")

    def _handle_control(self, stage: Stage, control: StageControl) -> None:
        self._activate_index(self.workflow.stages.index(stage), refresh=False)
        if control.action is not None:
            self._run_action(control.action, control.completes)
            return
        if control.skippable:
            self._skip_current()
            return
        if control.completes:
            self._complete_current()
            return
        self._refresh()

    def _run_action(self, action: str, complete_after: bool = False) -> None:
        if self.engine is None:
            self._set_status("No engine is attached.", "warning")
            self._refresh()
            return

        try:
            result = self.engine.run_action(
                action,
                self._engine_payload(),
                EngineContext(metadata={"workflow": self.workflow.id, "stage": self.workflow.current_stage(self.state).id}),
            )
        except Exception as exc:
            self._set_status(f"{action} failed: {exc}", "error")
            self._notify(str(exc), "negative")
            self._refresh()
            return

        self.last_result = result
        self._store_stage_result(result)
        self._set_status(f"{action} complete.", "success")
        if complete_after:
            self._complete_current(refresh=False)
        self._refresh()

    def _complete_current(self, refresh: bool = True) -> None:
        try:
            self.state = self.workflow.complete_current(self.state, confirmed=True)
            self._set_status("Stage complete.", "success")
        except Exception as exc:
            self._set_status(str(exc), "error")
            self._notify(str(exc), "negative")
        if refresh:
            self._refresh()

    def _skip_current(self) -> None:
        try:
            self.state = self.workflow.skip_current(self.state)
            self._set_status("Stage skipped.", "warning")
        except Exception as exc:
            self._set_status(str(exc), "error")
            self._notify(str(exc), "negative")
        self._refresh()

    def _previous_stage(self) -> None:
        if self.state.index > 0:
            self._activate_index(self.state.index - 1)

    def _next_stage(self) -> None:
        if self.state.index < len(self.workflow.stages) - 1:
            self._activate_index(self.state.index + 1)

    def _activate_index(self, index: int, *, refresh: bool = True) -> None:
        index = max(0, min(index, len(self.workflow.stages) - 1))
        statuses = dict(self.state.statuses)
        current = self.workflow.current_stage(self.state)
        if statuses.get(current.id) is StageStatus.ACTIVE:
            statuses[current.id] = StageStatus.PENDING
        target = self.workflow.stages[index]
        if statuses.get(target.id) is StageStatus.PENDING:
            statuses[target.id] = StageStatus.ACTIVE
        self.state = replace(self.state, index=index, statuses=statuses)
        if refresh:
            self._refresh()

    def _store_stage_result(self, result: EngineResult) -> None:
        data = dict(self.state.data)
        data[self.workflow.current_stage(self.state).id] = result
        self.state = replace(self.state, data=data)

    def _engine_payload(self) -> dict[str, Any]:
        if self.settings is None:
            return {}
        return {
            name: value
            for name, value in self.values.items()
            if name in self.engine_setting_names
        }

    def _latest_result_set(self) -> ResultSet | None:
        if self.last_result is not None:
            return self.last_result.result_set
        for value in reversed(list(self.state.data.values())):
            if isinstance(value, EngineResult):
                return value.result_set
        return None

    def _latest_metadata(self) -> dict[str, Any]:
        result_set = self._latest_result_set()
        if result_set is None:
            return {}
        return result_set.metadata

    def _handle_upload(self, event: Any) -> None:
        size = getattr(getattr(event, "content", None), "size", "")
        self.batch_files.append({"name": getattr(event, "name", "upload"), "size": size})
        self._set_status("Batch file added.", "success")
        self._refresh()

    def _set_status(self, message: str, kind: str) -> None:
        self.status = message
        self.status_kind = kind

    def _notify(self, message: str, kind: str) -> None:
        try:
            from nicegui import ui

            ui.notify(message, type=kind)
        except Exception:
            return

    def _refresh(self) -> None:
        if self._screen is not None:
            self._screen.refresh()

    def _default_controls(self, stage: Stage) -> tuple[StageControl, ...]:
        controls: list[StageControl] = []
        if stage.action:
            controls.append(StageControl("Run Stage", stage.action, completes=True))
        if stage.skippable:
            controls.append(StageControl("Skip", skippable=True, variant="secondary"))
        if not controls:
            controls.append(StageControl("Complete", completes=True, variant="success"))
        return tuple(controls)


def render_settings(settings: ParamSchema, values: dict[str, Any] | None = None) -> dict[str, Any]:
    from nicegui import ui

    target = values if values is not None else settings.defaults()
    for name, value in settings.defaults().items():
        target.setdefault(name, value)

    with ui.grid(columns=2).classes("w-full gap-3"):
        for param in settings.params:
            if param.kind is ParamKind.BOOLEAN:
                ui.checkbox(param.label, value=bool(target.get(param.name))).bind_value(
                    target,
                    param.name,
                )
            elif param.kind is ParamKind.CHOICE:
                options = {option.value: option.label for option in param.options}
                ui.select(
                    options,
                    label=param.label,
                    value=target.get(param.name),
                ).bind_value(target, param.name).classes("w-full")
            elif param.kind in {ParamKind.INTEGER, ParamKind.FLOAT}:
                ui.number(
                    label=param.label,
                    value=target.get(param.name),
                    min=param.minimum,
                    max=param.maximum,
                    step=param.step,
                ).bind_value(target, param.name).classes("w-full")
            else:
                ui.input(param.label, value=target.get(param.name) or "").bind_value(
                    target,
                    param.name,
                ).classes("w-full")
    return target


def _record_to_row(index: int, record: ResultRecord) -> dict[str, Any]:
    row = {"id": index, "sample_id": record.sample_id, "engine": record.engine}
    row.update(record.values)
    return row


def _badge_color(kind: str) -> str:
    return {
        "success": "color=green",
        "warning": "color=orange",
        "error": "color=red",
    }.get(kind, "color=blue")


def _status_badge(status: StageStatus) -> str:
    return {
        StageStatus.PENDING: "color=grey",
        StageStatus.ACTIVE: "color=blue",
        StageStatus.COMPLETE: "color=green",
        StageStatus.SKIPPED: "color=orange",
    }[status]


def _status_icon(status: StageStatus) -> str:
    return {
        StageStatus.PENDING: "radio_button_unchecked",
        StageStatus.ACTIVE: "play_arrow",
        StageStatus.COMPLETE: "check_circle",
        StageStatus.SKIPPED: "skip_next",
    }[status]


def _button_props(variant: str) -> str:
    if variant == "secondary":
        return "outline"
    if variant == "warning":
        return "color=orange"
    if variant == "danger":
        return "color=red"
    if variant == "success":
        return "color=green"
    return "color=blue"


def _control_icon(control: StageControl) -> str:
    if control.skippable:
        return "skip_next"
    if control.completes and control.action is None:
        return "check"
    if control.action and "refresh" in control.action:
        return "refresh"
    if control.action and "connect" in control.action:
        return "power"
    if control.action and "recording" in control.action:
        return "fiber_manual_record"
    if control.action and "pause" in control.action:
        return "pause"
    if control.action and "resume" in control.action:
        return "play_arrow"
    if control.action and "stop" in control.action:
        return "stop"
    return "play_arrow"
