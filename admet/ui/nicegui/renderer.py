from __future__ import annotations

import json
from dataclasses import replace
from functools import partial
from typing import Any

from admet.core.engine import EngineContext, EngineResult
from admet.core.schema import ParamKind, ParamSchema, ResultRecord, ResultSet
from admet.core.workflow import Stage, StageControl, StageStatus, StageSurface, Workflow, WorkflowState


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
        .admet-shell { min-height: 100vh; color: #17202a; font-size: 13px; }
        .admet-topbar { border-bottom: 1px solid #d9dee7; background: #ffffff; }
        .admet-sidebar { border-right: 1px solid #d9dee7; background: #ffffff; }
        .admet-panel { border: 1px solid #d9dee7; background: #ffffff; border-radius: 10px; }
        .admet-muted { color: #677383; }
        .admet-camera {
          background: #151a21;
          color: #dfe7f1;
          width: min(100%, 640px);
          height: clamp(180px, 36vw, 360px);
          max-width: 640px;
          max-height: 360px;
          object-fit: contain;
        }
        .admet-camera img { object-fit: contain; }
        .admet-camera-settings { border-top: 1px solid #d9dee7; background: #ffffff; }
        .admet-action-grid { grid-template-columns: repeat(auto-fit, minmax(88px, 1fr)); }
        .admet-action-grid .q-btn { width: 100%; }
        .admet-toc-button { min-height: 30px; border-radius: 999px; }
        .admet-shell .q-btn {
          min-height: 28px;
          padding: 2px 8px;
          border-radius: 999px;
          text-transform: none;
          font-size: 12px;
        }
        .admet-shell .q-badge { border-radius: 999px; font-size: 10px; padding: 2px 6px; }
        .admet-shell .q-field__control { min-height: 34px; border-radius: 8px; }
        .admet-shell .q-field, .admet-shell .q-checkbox { min-width: 0; }
        .admet-shell .q-checkbox__label { white-space: normal; line-height: 1.2; }
        .admet-shell .q-table th, .admet-shell .q-table td { padding: 4px 8px; }
        .admet-record-table .q-table th,
        .admet-record-table .q-table td {
          white-space: normal;
          overflow-wrap: anywhere;
          word-break: break-word;
          vertical-align: top;
        }
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
            for surface in stage.surfaces:
                self.values.update(surface.settings.defaults())
        self.engine_setting_names = {
            param.name for param in settings.params
        } if settings is not None else set()
        self.workflow_setting_names = {
            param.name
            for stage in workflow.stages
            for schema in (stage.settings, *(surface.settings for surface in stage.surfaces))
            for param in schema.params
        }
        self.status = "Ready"
        self.status_kind = "info"
        self.last_result: EngineResult | None = None
        self.batch_files: list[dict[str, Any]] = []
        self._selection_drag_start: tuple[int, int] | None = None
        self._show_camera_settings = False
        self._screen: Any | None = None

    def render(self) -> None:
        from nicegui import ui

        @ui.refreshable
        def screen() -> None:
            self._render_screen()

        self._screen = screen
        screen()
        ui.timer(1.0, self._poll_camera_status, active=True)

    def _render_screen(self) -> None:
        from nicegui import ui

        ui.query("body").classes("m-0")
        with ui.column().classes("admet-shell w-full gap-0"):
            self._render_topbar()
            with ui.row().classes("w-full flex-nowrap gap-0 grow"):
                with ui.column().classes("admet-sidebar w-80 shrink-0 gap-2 p-3"):
                    self._render_workflow_sidebar()
                with ui.column().classes("grow gap-3 p-3"):
                    stage = self.workflow.current_stage(self.state)
                    for surface in stage.surfaces:
                        self._render_surface(surface)
                    self._render_stage_panel()
                    self._render_batch_panel()
                    self._render_results_panel()

    def _render_topbar(self) -> None:
        from nicegui import ui

        with ui.row().classes("admet-topbar w-full items-center justify-between px-3 py-2"):
            with ui.column().classes("gap-0"):
                ui.label(self.workflow.label).classes("text-base font-semibold")
                engine_name = getattr(self.engine, "name", "No engine")
                ui.label(f"Engine: {engine_name}").classes("admet-muted text-xs")
            with ui.row().classes("items-center gap-2"):
                ui.badge(self.status_kind.upper()).props(_badge_color(self.status_kind))
                ui.label(self.status).classes("text-sm")

    def _render_workflow_sidebar(self) -> None:
        from nicegui import ui

        ui.label("Workflow").classes("text-sm font-semibold")
        for index, stage in enumerate(self.workflow.stages):
            status = self.state.statuses[stage.id]
            selected = index == self.state.index
            with ui.column().classes("gap-2"):
                button = ui.button(
                    stage.label,
                    icon=_status_icon(status),
                    on_click=partial(self._activate_index, index),
                ).props("dense no-caps flat align=left")
                button.classes(
                    "admet-toc-button w-full justify-start text-xs"
                    + (" bg-blue-50 text-blue-800" if selected else "")
                )
                with ui.row().classes("items-center gap-1 pl-2 flex-wrap"):
                    ui.badge(status.value).props(_status_badge(status))
            if selected:
                self._render_stage_controls(stage)
        with ui.row().classes("gap-1 pt-1"):
            ui.button("Previous", icon="chevron_left", on_click=self._previous_stage).props(
                "dense no-caps outline"
            )
            ui.button("Next", icon="chevron_right", on_click=self._next_stage).props(
                "dense no-caps outline"
            )

    def _render_stage_controls(self, stage: Stage) -> None:
        from nicegui import ui

        controls = stage.controls or self._default_controls(stage)
        with ui.column().classes("gap-1 pl-2"):
            for control in controls:
                props = _button_props(control.variant)
                ui.button(
                    control.label,
                    icon=_control_icon(control),
                    on_click=partial(self._handle_control, stage, control),
                ).props(props).classes("w-full justify-start")

    def _render_surface(self, surface: StageSurface) -> None:
        if surface.kind == "camera":
            self._render_camera_surface(surface)

    def _render_camera_surface(self, surface: StageSurface) -> None:
        from nicegui import ui

        metadata = self._latest_metadata()
        cameras = metadata.get("cameras") or metadata.get("camera", {}).get("cameras") or []
        transport_layers = metadata.get("camera_transport_layers") or []
        camera_message = metadata.get("camera_message", "Refresh cameras when a device is attached.")
        connected = bool(metadata.get("camera_connected"))
        live = bool(metadata.get("camera_live"))
        preview_src = metadata.get("camera_preview_src") or ""
        preview_width = int(metadata.get("camera_preview_width") or self.values.get("camera_width") or 640)
        preview_height = int(metadata.get("camera_preview_height") or self.values.get("camera_height") or 480)

        with ui.column().classes("admet-panel w-full gap-2 p-3"):
            with ui.column().classes("w-full gap-1"):
                ui.label(surface.title or "Camera").classes("text-sm font-semibold")
                self._render_camera_control_row(surface.controls)
            with ui.row().classes("w-full gap-3 items-start flex-wrap"):
                with ui.column().classes("grow min-w-0 gap-2"):
                    if preview_src:
                        ui.interactive_image(
                            preview_src,
                            content=self._camera_overlay(preview_width, preview_height),
                            on_mouse=self._handle_camera_mouse,
                            events=["mousedown", "mousemove", "mouseup"],
                            cross="#80eaff",
                        ).classes("admet-camera w-full rounded-md overflow-hidden")
                    else:
                        with ui.column().classes("admet-camera w-full items-center justify-center rounded-md p-3"):
                            ui.icon("videocam").classes("text-3xl")
                            ui.label("No live frame").classes("text-sm")
                            ui.label("Connect camera and start live.").classes("text-xs admet-muted")
                    self._render_camera_status_strip(metadata, preview_width, preview_height)
                with ui.column().classes("w-64 max-w-full shrink-0 gap-2"):
                    if cameras:
                        self._render_camera_connection_controls(cameras, surface)
                    ui.badge("connected" if connected else "not connected").props(
                        "color=green" if connected else "color=grey"
                    )
                    ui.badge("live" if live else "idle").props("color=green" if live else "color=grey")
                    ui.label(camera_message).classes("admet-muted text-sm")
                    if cameras:
                        ui.label("Detected cameras").classes("font-medium")
                        for camera in cameras:
                            ui.label(str(camera)).classes("text-sm")
                    if transport_layers and self._show_camera_settings:
                        ui.label("Transport layers").classes("font-medium")
                        for layer in transport_layers:
                            ui.label(str(layer)).classes("text-xs admet-muted")
        if self._show_camera_settings:
            self._render_camera_settings(surface)

    def _render_camera_control_row(self, controls: tuple[StageControl, ...]) -> None:
        from nicegui import ui

        stage = self.workflow.current_stage(self.state)
        with ui.grid().classes("admet-action-grid w-full gap-1"):
            for control in controls:
                ui.button(
                    control.label,
                    icon=_control_icon(control),
                    on_click=partial(self._handle_control, stage, control),
                ).props(_button_props(control.variant)).classes("w-full")
            ui.button(
                "Settings",
                icon="tune",
                on_click=self._toggle_camera_settings,
            ).props("dense no-caps outline").classes("w-full")

    def _render_camera_connection_controls(self, cameras: list[Any], surface: StageSurface) -> None:
        from nicegui import ui

        camera_index_param = surface.options.get("camera_index_param", "camera_index")
        options = {index: str(camera) for index, camera in enumerate(cameras)}
        if not options:
            options = {0: "No cameras detected"}
        ui.select(
            options,
            label=_param_by_name(surface.settings, camera_index_param).label,
            value=int(self.values.get(camera_index_param) or 0),
        ).bind_value(self.values, camera_index_param).classes("w-full")

    def _render_camera_settings(self, surface: StageSurface) -> None:
        from nicegui import ui

        with ui.column().classes("admet-camera-settings w-full gap-2 px-3 py-2"):
            with ui.column().classes("w-full gap-1"):
                ui.label("Advanced camera settings").classes("text-sm font-semibold")
                with ui.grid().classes("admet-action-grid w-full gap-1"):
                    ui.button(
                        "Apply",
                        icon="check",
                        on_click=partial(self._run_action, "apply_camera_settings", False),
                    ).props("dense no-caps outline color=green").classes("w-full")
                    ui.button(
                        "Disconnect",
                        icon="power_off",
                        on_click=partial(self._run_action, "disconnect_camera", False),
                    ).props("dense no-caps outline color=orange").classes("w-full")
            for group in surface.options.get("groups", ()):
                self._render_camera_setting_group(surface, group)

    def _render_camera_setting_group(self, surface: StageSurface, group: dict[str, Any]) -> None:
        from nicegui import ui

        schema = _schema_subset(surface.settings, group.get("params", ()))
        if not schema.params:
            return
        ui.label(group.get("title", "Settings")).classes("admet-muted text-xs font-medium")
        render_settings(schema, self.values, columns="auto")
        if "camera_selection_w" in {param.name for param in schema.params}:
            ui.button("Clear Selection", icon="backspace", on_click=self._clear_camera_selection).props(
                "dense no-caps outline"
            )

    def _render_camera_status_strip(
        self,
        metadata: dict[str, Any],
        preview_width: int,
        preview_height: int,
    ) -> None:
        from nicegui import ui

        fps = float(metadata.get("camera_fps") or 0.0)
        frames = int(metadata.get("camera_recorded_frames") or 0)
        elapsed = float(metadata.get("camera_record_elapsed") or 0.0)
        selection = self._selection_rect()
        roi = f"{preview_width}x{preview_height}" if preview_width and preview_height else "---"
        sel = f"{selection[2]}x{selection[3]}+{selection[0]}+{selection[1]}" if selection else "None"
        with ui.row().classes("gap-1 flex-wrap"):
            for label, value in (
                ("FPS", f"{fps:.1f}"),
                ("REC", "ON" if metadata.get("camera_recording") else "OFF"),
                ("FRAMES", str(frames)),
                ("TIME", f"{elapsed:.1f}s"),
                ("ROI", roi),
                ("SEL", sel),
            ):
                with ui.row().classes("items-center gap-1 border border-gray-200 rounded-full px-2 py-1"):
                    ui.label(label).classes("admet-muted text-[10px]")
                    ui.label(value).classes("text-xs font-medium")

    def _number(
        self,
        name: str,
        label: str,
        minimum: float | None = None,
        *,
        step: float | None = None,
    ) -> None:
        from nicegui import ui

        ui.number(
            label=label,
            value=self.values.get(name),
            min=minimum,
            step=step,
        ).bind_value(self.values, name).classes("w-full")

    def _camera_overlay(self, width: int, height: int) -> str:
        width = max(1, int(width or self.values.get("camera_width") or 640))
        height = max(1, int(height or self.values.get("camera_height") or 480))
        parts = []
        if self.values.get("camera_ruler_v"):
            step = max(1, width // 10)
            parts.extend(
                f'<line x1="{x}" y1="0" x2="{x}" y2="{height}" stroke="yellow" stroke-width="1" opacity="0.55" />'
                for x in range(0, width + 1, step)
            )
        if self.values.get("camera_ruler_h"):
            step = max(1, height // 10)
            parts.extend(
                f'<line x1="0" y1="{y}" x2="{width}" y2="{y}" stroke="yellow" stroke-width="1" opacity="0.55" />'
                for y in range(0, height + 1, step)
            )
        if self.values.get("camera_ruler_radial"):
            cx = width / 2
            cy = height / 2
            radius = (width ** 2 + height ** 2) ** 0.5 / 2
            for angle in range(0, 360, 30):
                import math

                rad = math.radians(angle)
                x = cx + radius * math.cos(rad)
                y = cy - radius * math.sin(rad)
                parts.append(
                    f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{x:.1f}" y2="{y:.1f}" stroke="yellow" stroke-width="1" opacity="0.45" />'
                )
        selection = self._selection_rect()
        if selection:
            x, y, w, h = selection
            parts.append(
                f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="rgba(0,120,255,0.16)" stroke="#00b4ff" stroke-width="2" stroke-dasharray="5 3" />'
            )
        return "".join(parts)

    def _handle_camera_mouse(self, event: Any) -> None:
        x = max(0, int(round(event.image_x)))
        y = max(0, int(round(event.image_y)))
        if event.type == "mousedown":
            self._selection_drag_start = (x, y)
            return
        if event.type == "mousemove" and self._selection_drag_start and event.buttons:
            self._set_camera_selection(self._selection_drag_start, (x, y))
            self._refresh()
            return
        if event.type == "mouseup" and self._selection_drag_start:
            self._set_camera_selection(self._selection_drag_start, (x, y))
            self._selection_drag_start = None
            self._refresh()

    def _set_camera_selection(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        x0, y0 = start
        x1, y1 = end
        x = min(x0, x1)
        y = min(y0, y1)
        w = abs(x1 - x0)
        h = abs(y1 - y0)
        if w <= 5 or h <= 5:
            self._clear_camera_selection(refresh=False)
            return
        self.values["camera_selection_x"] = x
        self.values["camera_selection_y"] = y
        self.values["camera_selection_w"] = w
        self.values["camera_selection_h"] = h

    def _selection_rect(self) -> tuple[int, int, int, int] | None:
        w = int(self.values.get("camera_selection_w") or 0)
        h = int(self.values.get("camera_selection_h") or 0)
        if w <= 0 or h <= 0:
            return None
        return (
            int(self.values.get("camera_selection_x") or 0),
            int(self.values.get("camera_selection_y") or 0),
            w,
            h,
        )

    def _clear_camera_selection(self, *, refresh: bool = True) -> None:
        self.values["camera_selection_x"] = 0
        self.values["camera_selection_y"] = 0
        self.values["camera_selection_w"] = 0
        self.values["camera_selection_h"] = 0
        self._selection_drag_start = None
        if refresh:
            self._refresh()

    def _render_stage_panel(self) -> None:
        from nicegui import ui

        stage = self.workflow.current_stage(self.state)
        with ui.column().classes("admet-panel w-full gap-3 p-3"):
            with ui.row().classes("w-full items-start justify-between"):
                with ui.column().classes("gap-1"):
                    ui.label(stage.label).classes("text-base font-semibold")
                    if stage.description:
                        ui.label(stage.description).classes("admet-muted")
                ui.badge(self.state.statuses[stage.id].value).props(
                    _status_badge(self.state.statuses[stage.id])
                )

            if stage.instructions:
                with ui.column().classes("gap-1"):
                    for instruction in stage.instructions:
                        ui.label(instruction).classes("text-sm")

            if stage.show_settings and stage.settings.params:
                ui.label("Section Settings").classes("text-sm font-semibold")
                render_settings(stage.settings, self.values)

            engine_settings = self._unclaimed_engine_settings()
            if engine_settings.params:
                with ui.expansion("Engine Settings", icon="tune").classes("w-full"):
                    render_settings(engine_settings, self.values)

    def _render_batch_panel(self) -> None:
        from nicegui import ui

        if self.workflow.id != "analyze":
            return
        with ui.column().classes("admet-panel w-full gap-2 p-3"):
            ui.label("Batch").classes("text-sm font-semibold")
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
        with ui.column().classes("admet-panel w-full gap-2 p-3"):
            ui.label("Results").classes("text-sm font-semibold")
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
                with ui.column().classes("border border-gray-200 rounded-md px-2 py-1 gap-0"):
                    ui.label(stat.name.replace("_", " ").title()).classes("admet-muted text-xs")
                    ui.label(label).classes("text-sm font-semibold")

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
        if len(rows) == 1:
            columns = [
                {
                    "name": "field",
                    "label": "Field",
                    "field": "field",
                    "align": "left",
                    "style": "width: 12rem; max-width: 35%; white-space: normal;",
                },
                {
                    "name": "value",
                    "label": "Value",
                    "field": "value",
                    "align": "left",
                    "style": "white-space: normal; overflow-wrap: anywhere; word-break: break-word;",
                },
            ]
            ui.table(
                columns=columns,
                rows=_single_record_field_rows(rows[0]),
                row_key="field",
            ).classes("admet-record-table w-full").props("wrap-cells dense flat")
            return
        keys = sorted({key for row in rows for key in row})
        columns = [{"name": key, "label": key.replace("_", " ").title(), "field": key} for key in keys]
        ui.table(columns=columns, rows=rows, row_key="id").classes("admet-record-table w-full").props(
            "wrap-cells dense"
        )

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

    def _poll_camera_status(self) -> None:
        if self.engine is None:
            return
        if not self._latest_metadata().get("camera_live"):
            return
        try:
            result = self.engine.run_action("camera_status", self._engine_payload())
        except Exception:
            return
        self.last_result = result
        self._store_stage_result(result)
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

    def _toggle_camera_settings(self) -> None:
        self._show_camera_settings = not self._show_camera_settings
        self._refresh()

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

    def _unclaimed_engine_settings(self) -> ParamSchema:
        if self.settings is None:
            return ParamSchema()
        return ParamSchema(
            tuple(param for param in self.settings.params if param.name not in self.workflow_setting_names)
        )


def render_settings(
    settings: ParamSchema,
    values: dict[str, Any] | None = None,
    *,
    columns: int | str = 2,
) -> dict[str, Any]:
    from nicegui import ui

    target = values if values is not None else settings.defaults()
    for name, value in settings.defaults().items():
        target.setdefault(name, value)

    grid_columns = columns if isinstance(columns, int) else "repeat(auto-fit, minmax(136px, 1fr))"
    with ui.grid(columns=grid_columns).classes("w-full gap-2"):
        for param in settings.params:
            if param.kind is ParamKind.BOOLEAN:
                ui.checkbox(param.label, value=bool(target.get(param.name))).bind_value(
                    target,
                    param.name,
                ).classes("min-w-0")
            elif param.kind is ParamKind.CHOICE:
                options = {option.value: option.label for option in param.options}
                ui.select(
                    options,
                    label=param.label,
                    value=target.get(param.name),
                ).bind_value(target, param.name).classes("w-full min-w-0")
            elif param.kind in {ParamKind.INTEGER, ParamKind.FLOAT}:
                ui.number(
                    label=param.label,
                    value=target.get(param.name),
                    min=param.minimum,
                    max=param.maximum,
                    step=param.step,
                ).bind_value(target, param.name).classes("w-full min-w-0")
            else:
                ui.input(param.label, value=target.get(param.name) or "").bind_value(
                    target,
                    param.name,
                ).classes("w-full min-w-0")
    return target


def _schema_subset(settings: ParamSchema, names: tuple[str, ...]) -> ParamSchema:
    wanted = set(names)
    return ParamSchema(tuple(param for param in settings.params if param.name in wanted))


def _param_by_name(settings: ParamSchema, name: str):
    for param in settings.params:
        if param.name == name:
            return param
    raise KeyError(name)


def _record_to_row(index: int, record: ResultRecord) -> dict[str, Any]:
    row = {"id": index, "sample_id": record.sample_id, "engine": record.engine}
    row.update({key: _table_value(value, key) for key, value in record.values.items()})
    return row


def _single_record_field_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"field": key.replace("_", " ").title(), "value": value}
        for key, value in row.items()
        if key != "id"
    ]


def _table_value(value: Any, key: str = "") -> Any:
    if key.endswith("_src") and isinstance(value, str) and value.startswith("data:"):
        return "[image]"
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, default=str)
    if isinstance(value, list | tuple | set):
        if not value:
            return ""
        return ", ".join(str(item) for item in value)
    return value


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
    base = "dense no-caps"
    if variant == "secondary":
        return f"{base} outline"
    if variant == "warning":
        return f"{base} outline color=orange"
    if variant == "danger":
        return f"{base} outline color=red"
    if variant == "success":
        return f"{base} outline color=green"
    return f"{base} outline color=blue"


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
