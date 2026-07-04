from __future__ import annotations

from typing import Any

from admet.core.engine import EngineRegistry, ParamKind
from admet.ui.analyze_runtime import NiceGuiAnalyzeRuntime
from admet.ui.presenter import ActionBoxVM, FieldVM, ResultsVM, ScreenModel, SurfaceVM, build_screen
from admet.ui.surfaces import render_nicegui_surface
from admet.ui.theme_web import stylesheet
from admet.workflows import Workflow, WorkflowState


def render_workflow(
    workflow: Workflow,
    state: WorkflowState,
    _settings: Any | None = None,
    *,
    registry: EngineRegistry | None = None,
    api: Any | None = None,
) -> None:
    engine_registry = registry or EngineRegistry()
    engine = getattr(api, "engine", None)
    if engine is not None:
        engine_registry.register(engine.id, lambda: engine)
    NiceGuiWorkflowApp(NiceGuiAnalyzeRuntime(workflow, state, engine_registry)).render()


class NiceGuiWorkflowApp:
    def __init__(self, runtime: NiceGuiAnalyzeRuntime) -> None:
        self.runtime = runtime
        self._refreshable: Any | None = None

    def render(self) -> None:
        from nicegui import ui

        ui.add_head_html(stylesheet())
        self._refreshable = ui.refreshable(self._render_screen)
        self.runtime.refresh = self._refresh
        self._refreshable()

    def _render_screen(self) -> None:
        from nicegui import ui

        screen = build_screen(self.runtime.workflow, self.runtime.state, self.runtime)
        ui.query("body").classes("m-0")
        with ui.column().classes("admet-shell w-full"):
            self._render_topbar(ui, screen)
            with ui.row().classes("admet-body w-full items-start no-wrap"):
                self._render_sidebar(ui, screen)
                self._render_content(ui, screen)

    def _render_topbar(self, ui: Any, screen: ScreenModel) -> None:
        project = screen.project
        with ui.row().classes("admet-topbar w-full items-center justify-between"):
            with ui.row().classes("items-baseline gap-3"):
                ui.label("admet analyze").classes("text-xl font-semibold")
                if project.root:
                    ui.label(f"root: {project.root}").classes("text-xs text-gray-500")
            with ui.row().classes("items-center gap-2"):
                ui.select(
                    project.options,
                    label="Project",
                    value=project.selected if project.selected in project.options else None,
                    on_change=lambda event: self.runtime.select_project(event.value),
                ).classes("admet-project-select")
                ui.button("Refresh", on_click=self.runtime.refresh_projects).props(
                    "dense no-caps outline"
                )
                if project.can_create:
                    ui.button("New Project", on_click=self.runtime.new_project).props(
                        "dense no-caps outline"
                    )
                if project.can_load:
                    ui.button("Load Project", on_click=self.runtime.load_project).props(
                        "dense no-caps outline"
                    )

    def _render_sidebar(self, ui: Any, screen: ScreenModel) -> None:
        with ui.column().classes("admet-sidebar"):
            for index, step in enumerate(screen.steps):
                classes = f"admet-step admet-step-{step.status.value}"
                if step.current:
                    classes += " admet-step-current"
                ui.button(
                    step.label,
                    on_click=lambda _event=None, i=index: self.runtime.activate(i),
                ).props("flat no-caps dense").classes(classes)
            notice_kind = screen.notice.kind if screen.notice is not None else "primary"
            with ui.column().classes(f"admet-notice admet-notice-{notice_kind}"):
                ui.label(screen.title).classes("text-sm font-semibold")
                for instruction in screen.instructions:
                    ui.label(instruction).classes("text-xs text-gray-600")
                if screen.notice is not None:
                    ui.separator()
                    ui.label(screen.notice.message).classes("text-xs")

    def _render_content(self, ui: Any, screen: ScreenModel) -> None:
        with ui.column().classes("admet-content grow"):
            with ui.column().classes("admet-panel"):
                self._render_buttons(ui, screen.action_box)
            with ui.column().classes("admet-panel"):
                self._render_fields(ui, screen.settings.fields)
            with ui.column().classes("admet-panel"):
                for surface in screen.surfaces:
                    self._render_surface(ui, surface)
            with ui.column().classes("admet-panel"):
                self._render_results(ui, screen.results)
            with ui.column().classes("admet-panel"):
                ui.label("Action Log").classes("admet-panel-title")
                for line in screen.log:
                    ui.label(line).classes("text-xs text-gray-600")

    def _render_buttons(self, ui: Any, action_box: ActionBoxVM) -> None:
        with ui.row().classes("admet-actions w-full"):
            for button in action_box.buttons:
                props = "dense no-caps outline"
                if not button.enabled:
                    props += " disable"
                ui.button(
                    button.label,
                    on_click=lambda _event=None, command=button.command: self._handle_command(command),
                ).props(props).classes(f"admet-button admet-button-{button.variant}")

    def _render_fields(self, ui: Any, fields: tuple[FieldVM, ...]) -> None:
        if not fields:
            ui.label("No stage-level settings.").classes("text-xs text-gray-500")
            return
        with ui.grid(columns=2).classes("admet-field-grid w-full"):
            for field in fields:
                self._render_field(ui, field)

    def _render_field(self, ui: Any, field: FieldVM) -> None:
        value = field.value
        if field.kind is ParamKind.BOOLEAN:
            ui.checkbox(
                field.label,
                value=bool(value),
                on_change=lambda event: self.runtime.set_field(field, event.value),
            )
            return
        if field.kind is ParamKind.CHOICE:
            options = {option.value: option.label for option in field.options}
            ui.select(
                options,
                label=field.label,
                value=value,
                on_change=lambda event: self.runtime.set_field(field, event.value),
            )
            return
        if field.kind in {ParamKind.FLOAT, ParamKind.INTEGER}:
            ui.number(
                label=field.label,
                value=value,
                min=field.minimum,
                max=field.maximum,
                on_change=lambda event: self.runtime.set_field(field, event.value),
            ).classes("w-full")
            return
        ui.input(
            label=field.label,
            value="" if value is None else str(value),
            on_change=lambda event: self.runtime.set_field(field, event.value),
        ).classes("w-full")

    def _render_surface(self, ui: Any, surface: SurfaceVM) -> None:
        with ui.column().classes("admet-surface w-full"):
            ui.label(surface.title or surface.kind).classes("admet-panel-title")
            if surface.fields:
                self._render_fields(ui, surface.fields)
            if surface.buttons:
                with ui.row().classes("admet-actions w-full"):
                    for button in surface.buttons:
                        ui.button(
                            button.label,
                            on_click=lambda _event=None, command=button.command: self._handle_command(command),
                        ).props("dense no-caps outline")
            render_nicegui_surface(ui, surface, self.runtime)

    def _render_results(self, ui: Any, results: ResultsVM) -> None:
        ui.label(results.title).classes("admet-panel-title")
        rows = list(results.rows)
        if rows:
            columns = [
                {"name": key, "label": key.title(), "field": key, "align": "left"}
                for key in rows[0]
            ]
            ui.table(columns=columns, rows=rows).classes("w-full").props("dense flat wrap-cells")
            return
        ui.label(results.empty_message).classes("text-xs text-gray-500")

    def _handle_command(self, command: str) -> None:
        self.runtime.handle_command(command)
        self._refresh()

    def _refresh(self) -> None:
        if self._refreshable is not None:
            self._refreshable.refresh()
