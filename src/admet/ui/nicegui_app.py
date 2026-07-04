from __future__ import annotations

from typing import Any

from admet.core.engine import EngineRegistry, ParamKind
from admet.ui.analyze_runtime import NiceGuiAnalyzeRuntime
from admet.ui.presenter import ActionBoxVM, FieldVM, ResultsVM, ScreenModel, SurfaceVM, build_screen
from admet.ui.surfaces import render_nicegui_surface
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

        ui.add_head_html(_style())
        self._refreshable = ui.refreshable(self._render_screen)
        self.runtime.refresh = self._refresh
        self._refreshable()

    def _render_screen(self) -> None:
        from nicegui import ui

        screen = build_screen(self.runtime.workflow, self.runtime.state, self.runtime)
        ui.query("body").classes("m-0")
        with ui.column().classes("shell w-full"):
            self._render_topbar(ui, screen)
            with ui.row().classes("w-full gap-4 flex-nowrap items-start"):
                self._render_sidebar(ui, screen)
                self._render_content(ui, screen)

    def _render_topbar(self, ui: Any, screen: ScreenModel) -> None:
        project = screen.project
        with ui.row().classes("topbar w-full items-center justify-between px-4 py-3"):
            with ui.row().classes("items-baseline gap-3"):
                ui.label("admet analyze").classes("text-xl font-semibold")
                if project.root:
                    ui.label(f"root: {project.root}").classes("muted text-xs root-hint")
            with ui.row().classes("items-center gap-2"):
                ui.select(
                    project.options,
                    label="Project",
                    value=project.selected if project.selected in project.options else None,
                    on_change=lambda event: self.runtime.select_project(event.value),
                ).classes("project-select")
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
        with ui.column().classes("left-rail shrink-0"):
            with ui.column().classes("workflow-toc w-full"):
                ui.label("Workflow").classes("toc-title")
                for index, step in enumerate(screen.steps):
                    selected = step.current
                    dot = _dot_class(step.status.value, selected)
                    with ui.row().classes("toc-row w-full items-center gap-2").on(
                        "click",
                        lambda _event=None, i=index: self.runtime.activate(i),
                    ):
                        ui.element("span").classes(dot)
                        ui.label(step.label).classes("text-sm" + (" font-semibold" if selected else ""))
            with ui.column().classes("notification-card w-full"):
                ui.label("Instructions").classes("notification-title")
                ui.label(" ".join(screen.instructions) or screen.description).classes("notification-text")
            notice_kind = screen.notice.kind if screen.notice is not None else "primary"
            classes = "notification-card w-full"
            if notice_kind != "primary":
                classes += f" notification-card-{notice_kind}"
            with ui.column().classes(classes):
                ui.label("Notification").classes("notification-title")
                ui.label(screen.notice.message if screen.notice is not None else screen.title).classes(
                    "notification-text"
                )

    def _render_content(self, ui: Any, screen: ScreenModel) -> None:
        with ui.column().classes("grow min-w-0 gap-3"):
            with self._panel(ui, "Action Box"):
                self._render_buttons(ui, screen.action_box)
            with self._panel(ui, "Action Panel"):
                self._render_settings_table(ui, screen.settings.fields)
            with self._panel(ui, "Main Window"):
                for surface in screen.surfaces:
                    self._render_surface(ui, surface)
            with self._panel(ui, screen.results.title):
                self._render_results(ui, screen.results)
            with self._panel(ui, "Action Log"):
                ui.html("<br>".join(_escape(line) for line in screen.log) or "No actions yet.").classes(
                    "log-text w-full"
                )

    def _panel(self, ui: Any, title: str):
        with ui.column().classes("admet-panel w-full gap-0"):
            ui.label(title).classes("admet-box-title")
            return ui.column().classes("admet-panel-body w-full gap-2")

    def _render_buttons(self, ui: Any, action_box: ActionBoxVM) -> None:
        with ui.column().classes("admet-process w-full gap-0 overflow-hidden"):
            ui.linear_progress(value=max(0.0, min(1.0, action_box.progress))).classes("w-full")
        with ui.row().classes("admet-action-row w-full items-center gap-2"):
            for button in action_box.buttons:
                self._action_button(ui, button)

    def _action_button(self, ui: Any, button: Any) -> None:
        props = "unelevated dense no-caps"
        if not button.enabled:
            props += " disable"
        ui.button(
            button.label,
            on_click=lambda _event=None, command=button.command: self._handle_command(command),
        ).props(props).classes(f"admet-button admet-button-{_web_variant(button.variant)}")

    def _render_fields(self, ui: Any, fields: tuple[FieldVM, ...]) -> None:
        if not fields:
            ui.label("No stage-level settings.").classes("muted text-xs")
            return
        with ui.grid(columns=2).classes("admet-field-grid w-full"):
            for field in fields:
                self._render_field(ui, field)

    def _render_settings_table(self, ui: Any, fields: tuple[FieldVM, ...]) -> None:
        if not fields:
            ui.label("No stage-level settings.").classes("text-xs text-gray-500")
            return
        with ui.column().classes("panel w-full gap-0 slim-table"):
            for field in fields:
                with ui.row().classes("admet-settings-row w-full items-center no-wrap"):
                    ui.label(field.label).classes("admet-settings-label")
                    with ui.column().classes("admet-settings-value grow"):
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
        with ui.column().classes("w-full gap-2"):
            ui.label(surface.title or surface.kind).classes("section-title")
            if surface.fields:
                self._render_fields(ui, surface.fields)
            if surface.buttons:
                with ui.row().classes("admet-action-row w-full items-center gap-2"):
                    for button in surface.buttons:
                        self._action_button(ui, button)
            render_nicegui_surface(ui, surface, self.runtime)

    def _render_results(self, ui: Any, results: ResultsVM) -> None:
        rows = list(results.rows)
        if rows:
            columns = [
                {"name": key, "label": key.title(), "field": key, "align": "left"}
                for key in rows[0]
            ]
            ui.table(columns=columns, rows=rows).classes("w-full slim-table").props("dense flat wrap-cells")
            return
        ui.label(results.empty_message).classes("muted text-xs")

    def _handle_command(self, command: str) -> None:
        self.runtime.handle_command(command)
        self._refresh()

    def _refresh(self) -> None:
        if self._refreshable is not None:
            self._refreshable.refresh()


def _web_variant(variant: str) -> str:
    if variant in {"primary", "success", "danger", "warning", "secondary"}:
        return variant
    return "neutral"


def _dot_class(status: str, selected: bool) -> str:
    if selected:
        return "toc-dot toc-dot-active"
    if status == "complete":
        return "toc-dot toc-dot-done"
    if status == "skipped":
        return "toc-dot toc-dot-skipped"
    return "toc-dot"


def _escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _style() -> str:
    return """
    <style>
    body { background: #f7fafc; }
    .shell { min-height: 100vh; color: #16212b; font-size: 13px; padding: 18px 20px 20px; gap: 8px; }
    .left-rail { width: 246px; align-self: flex-start; gap: 8px; }
    .topbar, .workflow-toc {
      background: #ffffff;
      border: 1px solid #d7e2ea;
      border-radius: 8px;
    }
    .topbar { min-height: 58px; }
    .admet-panel {
      background: transparent;
      border: 0;
      border-radius: 0;
      gap: 4px;
    }
    .admet-box-title {
      font-size: 16px;
      line-height: 22px;
      font-weight: 650;
      color: #16212b;
      padding: 0;
    }
    .admet-panel-body {
      padding: 0;
    }
    .workflow-toc { padding: 10px 12px; gap: 3px; }
    .notification-card {
      background: #ffffff;
      border: 1px solid #225d82;
      border-left: 5px solid #225d82;
      border-radius: 2px;
      padding: 12px 14px;
      gap: 4px;
    }
    .notification-card-warning { border-color: #b7791f; border-left-color: #b7791f; }
    .notification-card-success { border-color: #185e49; border-left-color: #185e49; }
    .notification-card-danger { border-color: #742323; border-left-color: #742323; }
    .notification-title { color: #52677a; font-size: 11px; font-weight: 650; text-transform: uppercase; }
    .notification-text { color: #16212b; font-size: 13px; font-weight: 600; line-height: 1.35; white-space: normal; overflow-wrap: anywhere; }
    .control-box { background: transparent; border: 0; border-radius: 0; gap: 6px; }
    .control-box-title { font-size: 16px; line-height: 22px; font-weight: 650; color: #16212b; }
    .process-bar {
      background: #f0f5f8;
      border: 1px solid #d7e2ea;
      border-radius: 8px 8px 0 0;
      min-height: 24px;
    }
    .process-bar .q-linear-progress {
      height: 18px;
      border-radius: 8px 8px 0 0;
      overflow: hidden;
    }
    .admet-process {
      background: #f0f5f8;
      border: 1px solid #d7e2ea;
      border-radius: 8px 8px 0 0;
      min-height: 24px;
    }
    .admet-process .q-linear-progress {
      height: 18px;
      border-radius: 8px 8px 0 0;
      overflow: hidden;
    }
    .admet-transport {
      background: transparent;
      border: 0;
      border-radius: 0;
    }
    .admet-action-row { flex-wrap: wrap; gap: 8px; padding-top: 8px; }
    .admet-button {
      min-height: 32px !important;
      border-radius: 6px !important;
      padding: 0 16px !important;
      font-weight: 600 !important;
      text-transform: none !important;
      border: 1px solid transparent !important;
      box-shadow: none !important;
    }
    .admet-button-primary { background: #225d82 !important; color: #ffffff !important; }
    .admet-button-primary:hover { background: #1b4a68 !important; }
    .admet-button-success { background: #1b6b53 !important; color: #ffffff !important; }
    .admet-button-success:hover { background: #185e49 !important; }
    .admet-button-danger { background: #8b2b2b !important; color: #ffffff !important; }
    .admet-button-danger:hover { background: #742323 !important; }
    .admet-button-warning { background: #b7791f !important; color: #ffffff !important; }
    .admet-button-warning:hover { background: #9d661a !important; }
    .admet-button-secondary, .admet-button-neutral {
      background: #ffffff !important;
      color: #16212b !important;
      border-color: #d7e2ea !important;
    }
    .admet-button-secondary:hover, .admet-button-neutral:hover { background: #f0f5f8 !important; }
    .admet-button.disable, .admet-button[disabled] { opacity: 0.45 !important; }
    .admet-transport-btn {
      background: #ffffff !important;
      color: #16212b !important;
      border: 0 !important;
      border-radius: 0 !important;
      min-height: 22px !important;
      height: 22px !important;
      padding: 0 !important;
      font-weight: 500;
    }
    .admet-transport-btn:hover { background: #f0f5f8 !important; }
    .admet-transport-btn-active {
      color: #225d82 !important;
      background: #e6eef4 !important;
      font-weight: 650;
    }
    .admet-transport-btn-warning {
      color: #ffffff !important;
      background: #b7791f !important;
      font-weight: 700;
    }
    .admet-transport-separator {
      width: 1px;
      align-self: stretch;
      background: #d7e2ea;
    }
    .transport-buttons { background: transparent; border: 0; border-radius: 0; }
    .transport-btn {
      background: #ffffff !important;
      color: #16212b !important;
      border: 0 !important;
      border-radius: 0 !important;
      min-height: 22px !important;
      height: 22px !important;
      padding: 0 !important;
      font-weight: 500;
    }
    .transport-btn:hover { background: #f0f5f8 !important; }
    .transport-btn-active { color: #225d82 !important; background: #e6eef4 !important; font-weight: 650; }
    .transport-btn-warning { color: #ffffff !important; background: #b7791f !important; font-weight: 700; }
    .transport-separator { width: 1px; align-self: stretch; background: #d7e2ea; }
    .panel { background: #ffffff; border: 1px solid #d7e2ea; border-radius: 8px; }
    .toc-title { color: #52677a; font-size: 11px; font-weight: 650; text-transform: uppercase; }
    .toc-row { min-height: 24px; border-radius: 6px; padding: 2px 4px; cursor: pointer; }
    .toc-row:hover { background: #f0f5f8; }
    .toc-dot { width: 8px; height: 8px; border-radius: 999px; background: #9aa7b2; border: 1px solid #d7e2ea; }
    .toc-dot-active { background: #225d82; border-color: #225d82; }
    .toc-dot-done { background: #185e49; border-color: #185e49; }
    .toc-dot-skipped { background: #9aa7b2; border-color: #9aa7b2; }
    .project-select { min-width: 360px; }
    .root-hint {
      max-width: 360px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .source-picker {
      min-width: 0;
    }
    .source-display {
      min-height: 30px;
      padding: 5px 8px;
      border: 1px solid #d7e2ea;
      border-radius: 6px;
      color: #16212b;
      background: #ffffff;
      font-size: 12px;
      font-weight: 600;
      overflow-wrap: anywhere;
    }
    .source-project {
      min-width: max-content;
      padding-bottom: 6px;
    }
    .source-cell {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .source-cell-name {
      min-width: 0;
      overflow-wrap: anywhere;
      color: #16212b;
      font-weight: 600;
    }
    .browser-card {
      width: min(760px, 92vw);
      max-height: 82vh;
      border-radius: 8px;
      box-shadow: none;
      border: 1px solid #d7e2ea;
      gap: 8px;
    }
    .browser-path {
      color: #52677a;
      font-family: Menlo, Consolas, monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .browser-list {
      max-height: 56vh;
      overflow-y: auto;
      border-top: 1px solid #d7e2ea;
      border-bottom: 1px solid #d7e2ea;
      padding: 6px 0;
    }
    .browser-row {
      justify-content: flex-start !important;
      width: 100%;
      border-radius: 4px !important;
      color: #16212b !important;
      font-weight: 500 !important;
    }
    .plot-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }
    .comparison-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    @media (max-width: 1100px) {
      .plot-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 760px) {
      .plot-grid, .comparison-grid { grid-template-columns: 1fr; }
    }
    .plot-card {
      border: 1px solid #d7e2ea;
      border-radius: 8px;
      background: #ffffff;
      min-height: 260px;
      padding: 4px;
    }
    .admet-viewer {
      position: relative;
      min-height: clamp(180px, 24vw, 360px);
      overflow: hidden;
      border-radius: 8px;
      border: 1px solid #d7e2ea;
      background: #16212b;
    }
    .admet-viewer-placeholder {
      background:
        radial-gradient(circle at 18% 34%, rgba(214, 223, 230, 0.78) 0 24px, transparent 25px),
        radial-gradient(circle at 68% 28%, rgba(224, 231, 236, 0.86) 0 34px, transparent 35px),
        radial-gradient(circle at 42% 70%, rgba(218, 226, 232, 0.82) 0 28px, transparent 29px),
        #f4f7f9;
    }
    .admet-video-frame {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      object-fit: contain;
      background: #16212b;
    }
    .admet-viewer-message {
      position: absolute;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      text-align: center;
      padding: 14px;
      color: #52677a;
      background: rgba(244, 247, 249, 0.72);
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .admet-viewer-placeholder-title {
      font-weight: 700;
      text-transform: uppercase;
      font-size: 11px;
      letter-spacing: 0;
      color: #16212b;
      margin-bottom: 6px;
    }
    .admet-viewer-placeholder-path {
      color: #52677a;
      font-family: Menlo, Consolas, monospace;
      font-size: 11px;
      margin-top: 6px;
    }
    .admet-roi {
      position: absolute;
      border: 2px solid #e5f36a;
      box-shadow: 0 0 0 999px rgba(0,0,0,0.22);
    }
    .admet-playhead {
      position: absolute;
      left: 10px;
      bottom: 10px;
      background: rgba(12,18,24,0.76);
      color: white;
      border-radius: 6px;
      padding: 4px 7px;
      font-size: 12px;
    }
    .media-editor-grid {
      display: grid;
      grid-template-columns: minmax(0, 3fr) minmax(260px, 1fr);
      gap: 10px;
      align-items: start;
    }
    @media (max-width: 980px) {
      .media-editor-grid { grid-template-columns: 1fr; }
    }
    .editor-note {
      border: 1px solid #d7e2ea;
      border-left: 4px solid #225d82;
      border-radius: 4px;
      padding: 8px 10px;
      color: #52677a;
      background: #f7fafc;
      font-size: 12px;
      font-weight: 600;
    }
    .editor-control-row { align-items: center; gap: 8px; }
    .calibration-row > .flat-number {
      flex: 1 1 0;
      min-width: 0;
    }
    .editor-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 8px;
    }
    .editor-grid-roi {
      border-top: 1px solid #d7e2ea;
      padding-top: 8px;
    }
    .compact-number .q-field__control,
    .compact-checkbox .q-checkbox__inner {
      min-height: 30px;
    }
    .slider-field .q-slider {
      min-height: 24px;
      padding: 0 2px;
    }
    .slider-label-row {
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .slider-value {
      color: #16212b;
      font-family: Menlo, Consolas, monospace;
      font-size: 12px;
      font-weight: 650;
    }
    .slider-field .q-slider__pin,
    .shell .q-slider__pin {
      display: none !important;
    }
    .shell .q-field__bottom {
      display: none !important;
    }
    .shell .q-field__control {
      background: #ffffff !important;
      box-shadow: none !important;
    }
    .shell .q-field__control:after,
    .shell .q-field__control:before {
      display: none !important;
    }
    .path-label {
      max-width: 100%;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      line-height: 1.3;
    }
    .correction-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .counter-card {
      border: 1px solid #d7e2ea;
      border-radius: 8px;
      padding: 8px;
      background: #f7fafc;
      gap: 6px;
    }
    .counter-value {
      color: #16212b;
      font-size: 24px;
      line-height: 28px;
      font-weight: 650;
    }
    .log-text {
      background: #f0f5f8;
      border: 1px solid #d7e2ea;
      border-radius: 8px;
      padding: 8px 10px;
      color: #52677a;
      font-family: Menlo, Consolas, monospace;
      font-size: 12px;
    }
    .slim-table .q-table th, .slim-table .q-table td {
      height: 30px;
      padding: 2px 6px;
      font-size: 12px;
      white-space: normal;
      overflow-wrap: anywhere;
    }
    .matrix-table .q-table td { height: 42px; }
    .matrix-table .q-field__control { min-height: 28px; }
    .muted { color: #52677a; }
    .section-title { color: #52677a; font-size: 12px; text-transform: uppercase; font-weight: 700; }
    .q-field__control { min-height: 34px; border-radius: 8px; }
    .q-btn { min-height: 30px; border-radius: 8px; text-transform: none; }
    .q-table__card { box-shadow: none; border: 1px solid #d7e2ea; }
    .q-table th, .q-table td { padding: 4px 8px; }
    </style>
    """
