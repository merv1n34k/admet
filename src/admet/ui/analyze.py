from __future__ import annotations

import base64
import csv
import html
import itertools
import json
import math
import mimetypes
import time
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any

from admet.core.discovery import ProjectRef, discover_projects, projects_root
from admet.core.engine import EngineRegistry, Param, ParamKind
from admet.core.project import ProjectStore
from admet.core.session import session_path
from admet.engines.cellpose.settings import CELLPOSE_SETTINGS
from admet.engines.opencv.settings import OPENCV_SETTINGS
from admet.ui import design
from admet.ui.scaffold import panel_specs
from admet.ui.window import structure_signature
from admet.ui.workflow_view import current_stage, guard_enabled, instruction_text, stage_by_id
from admet.workflows import StageStatus, Workflow, WorkflowState
from admet.workflows.analyze_runner import AnalyzeBatchReport, AnalyzeBatchRunner, AnalyzeTarget, infer_engine


VIDEO_SUFFIXES = {".avi", ".mp4", ".mov", ".mkv"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


@dataclass
class MatrixRow:
    uid: str
    project_path: str
    source_path: str
    engine: str
    sample_id: str
    cache_policy: str = "use"
    active: bool = True
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StoredRun:
    project_path: Path
    run_id: str
    raw_path: Path
    jobs: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class RawSummary:
    project: str
    run_id: str
    sample_id: str
    engine: str
    rows: int
    frames: int
    droplets: int
    mean_diameter: float
    median_diameter: float
    std_diameter: float
    cv_percent: float
    inclusions: int
    volume_nl: float
    true_count: float
    frequency_hz: float
    threshold: float
    diameters: tuple[float, ...] = ()
    detections: tuple[tuple[float, float, float, float, float], ...] = ()
    spans: tuple[tuple[float, float, float], ...] = ()
    inclusion_counts: tuple[int, ...] = ()


@dataclass(frozen=True)
class FluidicsRun:
    project: str
    recording_id: str
    rows: tuple[dict[str, float], ...]
    metadata: dict[str, Any]


def render_workflow(
    workflow: Workflow,
    state: WorkflowState,
    _settings: Any | None = None,
    *,
    registry: EngineRegistry | None = None,
    api: Any | None = None,
) -> None:
    from nicegui import ui

    if registry is None:
        engine = getattr(api, "engine", None)
        registry = EngineRegistry()
        if engine is not None:
            registry.register(engine.id, lambda: engine)

    ui.add_head_html(_style())
    AnalyzeWorkflowView(workflow, state, registry).render()


class AnalyzeWorkflowView:
    def __init__(
        self,
        workflow: Workflow,
        state: WorkflowState,
        registry: EngineRegistry,
    ) -> None:
        self.workflow = workflow
        self.state = state
        self.registry = registry
        root = projects_root()
        self.discovery_root = str(root)
        self.project_refs = discover_projects(root)
        self.project_path = str(root / f"admet_{time.strftime('%Y%m%d_%H%M%S')}.admetp")
        self.source_path = ""
        self.selected_uid = ""
        self.matrix: list[MatrixRow] = []
        self.settings: dict[str, Any] = self._default_settings()
        self.stage_progress: dict[str, int] = {
            "import": 0,
            "video": 0,
            "imaging": 0,
            "view": 0,
            "export": 0,
        }
        self.last_report: AnalyzeBatchReport | None = None
        self._run_progress = 0
        self.notice = "Create or select a project, then add files to the batch matrix."
        self.notice_kind = "primary"
        self.action_log: list[str] = ["Analyze UI ready."]
        self._mounted_signature: tuple[Any, ...] | None = None
        self._refs: dict[str, Any] = {}
        self._table_refs: dict[str, list[Any]] = {}
        self._button_refs: list[Any] = []

    def render(self) -> None:
        from nicegui import ui

        ui.query("body").classes("m-0")
        with ui.column().classes("shell w-full"):
            self._refs["topbar"] = ui.row().classes("topbar w-full items-center justify-between px-4 py-3")
            with ui.row().classes("w-full gap-4 flex-nowrap items-start"):
                self._refs["sidebar"] = ui.column().classes("left-rail shrink-0")
                self._refs["content"] = ui.column().classes("grow min-w-0 gap-3")
        self._mount_topbar()
        self._mount_sidebar()
        self._mount_content_shell()
        self._render_current_stage(force_mount=True)

    def _mount_topbar(self) -> None:
        from nicegui import ui

        topbar = self._refs.get("topbar")
        if topbar is None:
            return
        topbar.clear()
        with topbar:
            with ui.row().classes("items-baseline gap-3"):
                ui.label("admet analyze").classes("text-xl font-semibold")
                ui.label("Project analysis matrix").classes("muted text-sm")
                ui.label(f"root: {self.discovery_root}").classes("muted text-xs root-hint")
            with ui.row().classes("items-center gap-2"):
                options = self._project_options()
                selected = self.project_path if self.project_path in options else None
                self._refs["project_select"] = ui.select(
                    options,
                    label="Project",
                    value=selected,
                    on_change=lambda event: self._select_project(event.value),
                ).classes("project-select")
                ui.button("Refresh", on_click=self._refresh_projects).props("dense no-caps outline")
                ui.button("New Project", on_click=self._new_project).props("dense no-caps outline")
                ui.button("Load Project", on_click=self._open_project_browser).props("dense no-caps outline")

    def _mount_sidebar(self) -> None:
        sidebar = self._refs.get("sidebar")
        if sidebar is None:
            return
        sidebar.clear()
        with sidebar:
            self._render_toc()
            self._render_instruction_card()
            self._render_notice_card()

    def _mount_content_shell(self) -> None:
        from nicegui import ui

        content = self._refs.get("content")
        if content is None:
            return
        content.clear()
        with content:
            for spec in panel_specs():
                with ui.column().classes("admet-panel w-full gap-0") as box:
                    ui.label(spec.title).classes("admet-box-title")
                    body = ui.column().classes("admet-panel-body w-full gap-2")
                self._refs[f"{spec.key}_box"] = box
                self._refs[f"{spec.key}_body"] = body

    def _render_current_stage(self, *, force_mount: bool = False) -> None:
        if "main_body" not in self._refs:
            return
        signature = self._structure_signature()
        if force_mount or signature != self._mounted_signature:
            self._mount_stage()
            self._mounted_signature = signature
        self._sync_stage()

    def _structure_signature(self) -> tuple[Any, ...]:
        return structure_signature(
            self._stage_id(),
            tuple(str(ref.path) for ref in self.project_refs),
            id(self.last_report),
        )

    def _mount_stage(self) -> None:
        for key in ("action_box_body", "action_panel_body", "main_body", "results_body", "log_body"):
            body = self._refs.get(key)
            if body is not None:
                body.clear()
        # clear() deletes the elements inside these bodies; drop their refs so a
        # stale preview from a previous stage can't reach _sync_previews and get
        # written to a deleted element. Stages that need them re-create them below.
        for key in ("action_progress", "log", "opencv_preview", "cellpose_preview"):
            self._refs.pop(key, None)
        self._table_refs = {}
        self._button_refs = []
        self._mount_action_box()
        self._mount_action_panel()
        self._mount_main_window()
        self._mount_results()
        self._mount_log()

    def _sync_stage(self) -> None:
        self._sync_topbar()
        self._mount_sidebar()
        self._sync_action_box()
        self._sync_tables()
        self._sync_previews()
        self._sync_log()

    def _sync_topbar(self) -> None:
        select = self._refs.get("project_select")
        if select is not None:
            select.options = self._project_options()
            selected = self.project_path if self.project_path in select.options else None
            if getattr(select, "value", None) != selected:
                select.set_value(selected)
            select.update()

    def _mount_action_box(self) -> None:
        from nicegui import ui

        body = self._refs["action_box_body"]
        stage_id = self._stage_id()
        with body:
            with ui.column().classes("admet-process w-full gap-0 overflow-hidden"):
                self._refs["action_progress"] = ui.linear_progress(value=0.0).classes("w-full")
                with ui.row().classes("admet-transport w-full items-stretch gap-0"):
                    for index, spec in enumerate(self._action_specs(stage_id)):
                        if index:
                            ui.element("span").classes("admet-transport-separator")
                        button = ui.button(spec["label"], on_click=spec["handler"]).props(
                            "unelevated dense no-caps flat"
                        )
                        classes = "admet-transport-btn grow"
                        if spec.get("active"):
                            classes += " admet-transport-btn-active"
                        if spec.get("warning"):
                            classes += " admet-transport-btn-warning"
                        button.classes(classes)
                        self._button_refs.append(button)

    def _sync_action_box(self) -> None:
        progress = self._refs.get("action_progress")
        if progress is not None:
            progress.set_value(self.stage_progress.get(self._stage_id(), 0) / 100.0)
        specs = self._action_specs(self._stage_id())
        if len(specs) != len(self._button_refs):
            return
        for button, spec in zip(self._button_refs, specs, strict=True):
            button.set_text(spec["label"])
            button.set_enabled(bool(spec.get("enabled", True)))

    def _mount_action_panel(self) -> None:
        body = self._refs["action_panel_body"]
        with body:
            self._render_settings_panel()

    def _render_settings_panel(self) -> None:
        stage = self._stage()
        settings_panel = stage.settings_panel
        if settings_panel is None:
            return
        if settings_panel.kind == "matrix":
            source = str(settings_panel.options.get("source") or "")
            if source == "all_targets":
                self._render_matrix()
            elif source == "opencv_targets":
                self._render_engine_matrix("opencv")
            elif source == "cellpose_targets":
                self._render_engine_matrix("cellpose")
            return
        if settings_panel.kind == "none":
            self._render_view_panel()
            return
        if stage.id == "export":
            self._render_export_panel()
            return
        self._render_view_panel()

    def _render_view_panel(self) -> None:
        from nicegui import ui

        with ui.column().classes("panel w-full gap-2 p-2"):
            self._render_view_settings()

    def _render_export_panel(self) -> None:
        from nicegui import ui

        with ui.column().classes("panel w-full gap-2 p-2"):
            ui.label("Export will use stored raw analysis data.").classes("muted text-xs")

    def _mount_main_window(self) -> None:
        body = self._refs["main_body"]
        with body:
            self._render_editor()

    def _render_editor(self) -> None:
        editor = self._stage().editor
        kind = editor.kind if editor is not None else ""
        if kind == "import_inventory":
            self._render_import_inventory()
        elif kind == "opencv_video":
            self._render_video_stage()
        elif kind == "cellpose_editor":
            self._render_imaging_stage()
        elif kind == "analysis_results":
            self._render_view_results()
        else:
            self._render_analysis_runs()

    def _mount_results(self) -> None:
        from nicegui import ui

        body = self._refs["results_body"]
        with body:
            with ui.column().classes("panel w-full gap-2 p-3"):
                ui.label("Run summary").classes("section-title")
                rows = self._result_rows()
                table = ui.table(
                    columns=_result_columns(),
                    rows=rows,
                ).classes("w-full").props("dense flat hide-bottom")
                self._register_table("run_summary", table)
                if not rows:
                    ui.label("No results yet. Run OpenCV, Cellpose, or Run All.").classes("muted text-xs")

    def _mount_log(self) -> None:
        from nicegui import ui

        body = self._refs["log_body"]
        with body:
            self._refs["log"] = ui.html("").classes("log-text w-full")

    def _sync_tables(self) -> None:
        row_sources = {
            "matrix": lambda: [self._matrix_row(row) for row in self.matrix],
            "project_files": self._project_file_rows,
            "recording_inventory": self._recording_inventory_rows,
            "opencv_matrix": lambda: [
                self._schema_matrix_row(row, _matrix_params("opencv")) for row in self._targets("opencv")
            ],
            "cellpose_matrix": lambda: [
                self._schema_matrix_row(row, _matrix_params("cellpose")) for row in self._targets("cellpose")
            ],
            "opencv_summary": lambda: _view_summary_rows(
                [summary for summary in self._raw_summaries() if summary.engine == "opencv"]
            ),
            "cellpose_summary": lambda: _view_summary_rows(
                [summary for summary in self._raw_summaries() if summary.engine == "cellpose"]
            ),
            "view_fluidics": lambda: _fluidics_rows(self._fluidics_runs()),
            "view_summary": lambda: _view_summary_rows(self._raw_summaries()),
            "analysis_runs": self._analysis_run_rows,
            "run_summary": self._result_rows,
        }
        for key, tables in self._table_refs.items():
            source = row_sources.get(key)
            if source is None:
                continue
            rows = source()
            for table in tables:
                table.rows = rows
                table.update()

    def _sync_previews(self) -> None:
        target = self._selected_row_for_engine("opencv")
        preview = self._refs.get("opencv_preview")
        if target is not None and preview is not None:
            preview.content = self._opencv_preview_html(target)
        target = self._selected_row_for_engine("cellpose")
        preview = self._refs.get("cellpose_preview")
        if target is not None and preview is not None:
            preview.content = self._cellpose_preview_html(target)

    def _sync_log(self) -> None:
        log = self._refs.get("log")
        if log is not None:
            log.content = "<br>".join(_escape(line) for line in self.action_log[-80:]) or "No actions yet."

    def _register_table(self, key: str, table: Any) -> None:
        self._table_refs.setdefault(key, []).append(table)

    def _render_toc(self) -> None:
        from nicegui import ui

        with ui.column().classes("workflow-toc w-full"):
            ui.label("Workflow").classes("toc-title")
            for index, stage in enumerate(self.workflow.stages):
                selected = index == self.state.index
                status = self.state.statuses.get(stage.id, StageStatus.PENDING)
                dot = _dot_class(status, selected)
                with ui.row().classes("toc-row w-full items-center gap-2").on(
                    "click",
                    lambda _event, idx=index: self._activate(idx),
                ):
                    ui.element("span").classes(dot)
                    ui.label(stage.label).classes("text-sm" + (" font-semibold" if selected else ""))

    def _render_instruction_card(self) -> None:
        from nicegui import ui

        with ui.column().classes("notification-card w-full"):
            ui.label("Instructions").classes("notification-title")
            ui.label(self._instruction()).classes("notification-text")

    def _render_notice_card(self) -> None:
        from nicegui import ui

        classes = "notification-card w-full"
        if self.notice_kind != "primary":
            classes += f" notification-card-{self.notice_kind}"
        with ui.column().classes(classes):
            ui.label("Notification").classes("notification-title")
            ui.label(self.notice).classes("notification-text")

    def _render_view_settings(self) -> None:
        from nicegui import ui

        ui.input(
            "Cache root",
            value=self.settings["cache_root"],
            on_change=lambda event: self._set_setting("cache_root", event.value or ""),
        ).classes("w-full")

    def _render_matrix(self) -> None:
        from nicegui import ui

        with ui.column().classes("panel w-full gap-2 p-3"):
            ui.label("Batch matrix").classes("section-title")
            self._render_matrix_table(self.matrix)
            if not self.matrix:
                ui.label("No sources yet. Browse a video file or imaging folder.").classes("muted text-xs")

    def _render_matrix_table(self, rows: list[MatrixRow]) -> None:
        from nicegui import ui

        table = ui.table(
            columns=_matrix_columns(),
            rows=[self._matrix_row(row) for row in rows],
            row_key="uid",
        ).classes("slim-table matrix-table w-full").props("dense flat hide-bottom")
        self._register_table("matrix", table)
        self._wire_matrix_table(table)
        table.on("rowClick", self._select_row_event)

    def _wire_matrix_table(self, table: Any) -> None:
        table.add_slot("body-cell-active", _bool_cell_slot("active"))
        table.add_slot(
            "body-cell-project",
            """
            <q-td :props="props">
              <div class="source-cell-name">{{ props.row.project }}</div>
            </q-td>
            """,
        )
        table.add_slot(
            "body-cell-source",
            """
            <q-td :props="props">
              <div class="source-cell-name">{{ props.row.source }}</div>
            </q-td>
            """,
        )
        table.add_slot(
            "body-cell-engine",
            """
            <q-td :props="props">
              <div class="source-cell-name">{{ props.row.engine }}</div>
            </q-td>
            """,
        )
        table.add_slot(
            "body-cell-sample_id",
            """
            <q-td :props="props">
              <q-input dense outlined v-model="props.row.sample_id"
                @click.stop @mousedown.stop
                @blur="$parent.$emit('matrix-change', {uid: props.row.uid, field: 'sample_id', value: props.row.sample_id})"
                @keyup.enter="$event.target.blur()" />
            </q-td>
            """,
        )
        table.on("matrix-change", self._handle_matrix_change)

    def _render_import_inventory(self) -> None:
        from nicegui import ui

        with ui.column().classes("panel w-full gap-2 p-3"):
            ui.label("Project inventory").classes("section-title")
            with ui.element("div").classes("comparison-grid w-full"):
                with ui.column().classes("plot-card"):
                    ui.label("Analysis files").classes("text-sm font-semibold px-2 pt-1")
                    table = ui.table(
                        columns=[
                            {"name": "project", "label": "Project", "field": "project", "align": "left"},
                            {"name": "role", "label": "Role", "field": "role", "align": "left"},
                            {"name": "engine", "label": "Engine", "field": "engine", "align": "left"},
                            {"name": "path", "label": "Path", "field": "path", "align": "left"},
                        ],
                        rows=self._project_file_rows(),
                    ).classes("w-full").props("dense flat hide-bottom")
                    self._register_table("project_files", table)
                with ui.column().classes("plot-card"):
                    ui.label("Recording inventory").classes("text-sm font-semibold px-2 pt-1")
                    table = ui.table(
                        columns=[
                            {"name": "project", "label": "Project", "field": "project", "align": "left"},
                            {"name": "recordings", "label": "Recordings", "field": "recordings", "align": "right"},
                            {"name": "csv_rows", "label": "CSV rows", "field": "csv_rows", "align": "right"},
                            {"name": "duration_s", "label": "Duration (s)", "field": "duration_s", "align": "right"},
                        ],
                        rows=self._recording_inventory_rows(),
                    ).classes("w-full").props("dense flat hide-bottom")
                    self._register_table("recording_inventory", table)

    def _render_engine_matrix(self, engine: str) -> None:
        from nicegui import ui

        # Which files run is decided only by the Use checkbox on the import stage;
        # the engine stages just show the files tagged for use.
        rows = [row for row in self.matrix if row.active and row.engine == engine]
        params = _matrix_params(engine)
        with ui.column().classes("panel w-full gap-2 p-3"):
            ui.label(f"{engine.title()} files").classes("section-title")
            table = ui.table(
                columns=_schema_matrix_columns(params),
                rows=[self._schema_matrix_row(row, params) for row in rows],
                row_key="uid",
            ).classes("slim-table matrix-table w-full").props("dense flat hide-bottom")
            self._register_table(f"{engine}_matrix", table)
            self._wire_schema_matrix(table, params)
            table.on("rowClick", self._select_row_event)
            if not rows:
                ui.label(f"No active {engine} files.").classes("muted text-xs")

    def _render_video_stage(self) -> None:
        self._render_opencv_editor()
        self._render_engine_plots("opencv")

    def _render_imaging_stage(self) -> None:
        self._render_engine_matrix("cellpose")
        self._render_cellpose_editor()
        self._render_engine_plots("cellpose")

    def _render_view_results(self) -> None:
        from nicegui import ui

        summaries = self._raw_summaries()
        with ui.column().classes("panel w-full gap-2 p-3"):
            ui.label("1. Analysis summary").classes("section-title")
            table = ui.table(
                columns=_view_summary_columns(),
                rows=_view_summary_rows(summaries),
            ).classes("w-full").props("dense flat hide-bottom")
            self._register_table("view_summary", table)
            if not summaries:
                ui.label("No stored analysis yet. Run OpenCV or Cellpose first.").classes("muted text-xs")
        with ui.column().classes("panel w-full gap-2 p-3"):
            ui.label("2. Droplet plots").classes("section-title")
            self._render_droplet_plots(summaries)
        with ui.column().classes("panel w-full gap-2 p-3"):
            ui.label("3. Fluidics summary").classes("section-title")
            fluidics = self._fluidics_runs()
            with ui.element("div").classes("comparison-grid w-full"):
                _plot_card(_fluidics_chart(fluidics, "pressure"))
                _plot_card(_fluidics_chart(fluidics, "flow"))
            table = ui.table(
                columns=[
                    {"name": "project", "label": "Project", "field": "project", "align": "left"},
                    {"name": "recording", "label": "Recording", "field": "recording", "align": "left"},
                    {"name": "rows", "label": "Rows", "field": "rows", "align": "right"},
                    {"name": "duration_s", "label": "Duration (s)", "field": "duration_s", "align": "right"},
                    {"name": "mean_pressure", "label": "Mean pressure", "field": "mean_pressure", "align": "right"},
                    {"name": "mean_flow", "label": "Mean flow", "field": "mean_flow", "align": "right"},
                ],
                rows=_fluidics_rows(fluidics),
            ).classes("w-full").props("dense flat hide-bottom")
            self._register_table("view_fluidics", table)

    def _render_opencv_editor(self) -> None:
        from nicegui import ui

        target = self._selected_row_for_engine("opencv")
        with ui.column().classes("panel w-full gap-2 p-3"):
            ui.label("Video preview / crop and frame limits").classes("section-title")
            if target is None:
                ui.label("Select an OpenCV file.").classes("muted text-xs")
                return
            metadata = _video_metadata(self._resolve_media_path(target.source_path, target.project_path))
            frame_max = max(int(metadata.get("frames") or 500) - 1, 1)
            width_max = max(int(metadata.get("width") or 1280), 1)
            height_max = max(int(metadata.get("height") or 720), 1)
            preview = _read_video_frame(
                self._resolve_media_path(target.source_path, target.project_path),
                _preview_frame_index(target),
            )
            preview_ok = not bool(preview["error"])
            with ui.element("div").classes("media-editor-grid w-full"):
                with ui.column().classes("media-editor-video min-w-0 gap-2"):
                    ui.label(target.sample_id or Path(target.source_path).stem).classes("text-sm font-semibold")
                    ui.label(_compact_path(target.source_path)).classes("muted text-xs path-label").props(
                        f'title="{html.escape(target.source_path)}"'
                    )
                    self._refs["opencv_preview"] = ui.html(self._opencv_preview_html(target, preview)).classes(
                        "opencv-preview w-full"
                    )
                    with ui.row().classes("calibration-row w-full gap-2"):
                        self._number_editor(
                            target,
                            "microns_per_pixel",
                            "Microns / px",
                            float(self.settings["opencv_microns_per_pixel"]),
                            step=0.01,
                        )
                        self._number_editor(
                            target,
                            "fps",
                            "FPS",
                            int(self.settings["opencv_fps"]),
                            step=1,
                        )
                with ui.column().classes("media-editor-controls min-w-0 gap-3"):
                    if not preview_ok:
                        ui.label("Preview unavailable. Relocate the source or use the arm64 analyze environment before editing crop/frame values.").classes(
                            "editor-note"
                        )
                    self._slider_editor(target, "preview_frame", "Preview frame", 0, frame_max, 1, 0)
                    self._slider_editor(target, "start_frame", "Start frame", 0, frame_max, 1, 0)
                    self._slider_editor(target, "end_frame", "End frame", 0, frame_max + 1, 1, frame_max)
                    self._slider_editor(target, "roi_x", "ROI X", 0, width_max, 1, 0)
                    self._slider_editor(target, "roi_y", "ROI Y", 0, height_max, 1, 0)
                    self._slider_editor(target, "roi_width", "ROI W", 0, width_max, 1, 0)
                    self._slider_editor(target, "roi_height", "ROI H", 0, height_max, 1, 0)

    def _slider_editor(
        self,
        row: MatrixRow,
        key: str,
        label: str,
        minimum: float,
        maximum: float,
        step: float,
        default: Any,
    ) -> None:
        from nicegui import ui

        value = _row_float(row, key, default)
        value = max(minimum, min(maximum, value))
        if step >= 1:
            value = int(value)
        with ui.column().classes("slider-field w-full gap-0"):
            with ui.row().classes("slider-label-row w-full"):
                ui.label(label).classes("muted text-xs font-semibold")
                value_label = ui.label(_format_slider_value(value, step)).classes("slider-value")

            # Each drag tick updates the label + row object (cheap) and refreshes the
            # preview live, but throttled to ~8x/sec so we get smooth scrubbing without
            # the per-tick full-stage-sync + video-decode flood that used to lag/crash.
            # Only the preview is synced here (not the whole stage), and frame decodes
            # are lru-cached, so this stays light.
            last_preview = {"t": -1.0}

            def update_slider(event: Any, item: MatrixRow = row, name: str = key, use_int: bool = step >= 1) -> None:
                number = _numeric(event.value)
                if number is None:
                    number = 0
                value_label.set_text(_format_slider_value(number, step))
                item.settings[name] = int(number) if use_int else float(number)
                self.selected_uid = item.uid
                now = time.monotonic()
                if now - last_preview["t"] >= 0.12:
                    last_preview["t"] = now
                    self._sync_previews()

            def commit_slider(_event: Any, item: MatrixRow = row) -> None:
                last_preview["t"] = time.monotonic()
                self.selected_uid = item.uid
                self._sync_previews()

            slider = ui.slider(
                min=minimum,
                max=maximum,
                step=step,
                value=value,
                on_change=update_slider,
            ).props("dense").classes("w-full")
            slider.on("change", commit_slider)

    def _number_editor(
        self,
        row: MatrixRow,
        key: str,
        label: str,
        default: Any,
        *,
        step: float,
        maximum: float | None = None,
        enabled: bool = True,
    ) -> None:
        from nicegui import ui

        value = _row_float(row, key, default) if step < 1 else _row_int(row, key, default)
        field = ui.number(
            label,
            value=value,
            min=0,
            max=maximum,
            step=step,
            on_change=lambda event, item=row, name=key, use_int=step >= 1: self._set_row_number(
                item,
                name,
                event.value,
                integer=use_int,
            ),
        ).classes("flat-number grow")
        field.set_enabled(enabled)

    def _set_row_number(
        self,
        row: MatrixRow,
        key: str,
        value: Any,
        *,
        integer: bool,
    ) -> None:
        number = _numeric(value)
        if number is None:
            number = 0
        row.settings[key] = int(number) if integer else float(number)
        self.selected_uid = row.uid
        self._sync_previews()

    def _opencv_preview_html(self, target: MatrixRow, frame: dict[str, Any] | None = None) -> str:
        source = self._resolve_media_path(target.source_path, target.project_path)
        frame_index = _preview_frame_index(target)
        frame = frame or _read_video_frame(source, frame_index)
        if frame["error"]:
            return _viewer_error_html(target, source, str(frame["error"]))
        width_px = max(int(frame["width"] or 1), 1)
        height_px = max(int(frame["height"] or 1), 1)
        left = _percent(_row_int(target, "roi_x", 0), width_px)
        top = _percent(_row_int(target, "roi_y", 0), height_px)
        roi_width_raw = _row_int(target, "roi_width", 0)
        roi_height_raw = _row_int(target, "roi_height", 0)
        roi_width = roi_width_raw or width_px
        roi_height = roi_height_raw or height_px
        width = _percent(roi_width, width_px)
        height = _percent(roi_height, height_px)
        roi = ""
        if roi_width_raw or roi_height_raw:
            roi = f'<div class="admet-roi" style="left:{left}%; top:{top}%; width:{width}%; height:{height}%;"></div>'
        return f"""
        <div class="admet-viewer">
          <img class="admet-video-frame" src="{frame['src']}" alt="{html.escape(target.sample_id)} frame {frame_index}">
          {roi}
          <div class="admet-playhead">{html.escape(target.sample_id or Path(target.source_path).stem)} - frame {frame_index}</div>
        </div>
        """

    def _cellpose_preview_html(self, target: MatrixRow) -> str:
        source = self._resolve_media_path(target.source_path, target.project_path)
        image = _read_image_source(source, _row_int(target, "image_frame", 1))
        if image["error"]:
            return _viewer_error_html(target, source, str(image["error"]))
        return f"""
        <div class="admet-viewer">
          <img class="admet-video-frame" src="{image['src']}" alt="{html.escape(target.sample_id)} image {image['index']}">
          <div class="admet-playhead">{html.escape(target.sample_id or Path(target.source_path).stem)} - image {image['index']}</div>
        </div>
        """

    def _resolve_media_path(self, source: str, project_path: str | None = None) -> Path:
        path = Path(source)
        if path.is_absolute():
            return path
        project = session_path(project_path) if project_path else self._project_path()
        return project / path

    def _image_count(self, target: MatrixRow) -> int:
        source = self._resolve_media_path(target.source_path, target.project_path)
        if source.is_file() and source.suffix.lower() in IMAGE_SUFFIXES:
            return 1
        if not source.is_dir():
            return 48
        return max(1, len([item for item in source.iterdir() if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES]))

    def _render_cellpose_editor(self) -> None:
        from nicegui import ui

        target = self._selected_row_for_engine("cellpose")
        with ui.column().classes("panel w-full gap-2 p-3"):
            ui.label("Post-run correction editor").classes("section-title")
            if target is None:
                ui.label("Select a Cellpose file or folder.").classes("muted text-xs")
                return
            image_max = self._image_count(target)
            with ui.element("div").classes("media-editor-grid w-full"):
                with ui.column().classes("gap-2"):
                    self._refs["cellpose_preview"] = ui.html(self._cellpose_preview_html(target)).classes("w-full")
                    self._slider_editor(target, "image_frame", "Image frame", 1, image_max, 1, 1)
                with ui.column().classes("gap-2"):
                    ui.label(target.sample_id or Path(target.source_path).stem).classes("text-sm font-semibold")
                    ui.label(target.source_path).classes("muted text-xs path-label")
                    with ui.element("div").classes("editor-grid"):
                        self._number_editor(
                            target,
                            "px_to_um",
                            "px to um",
                            float(self.settings["cellpose_px_to_um"]),
                            step=0.01,
                        )
                        self._number_editor(
                            target,
                            "frame_limit",
                            "Frame limit",
                            0,
                            step=1,
                            maximum=max(image_max, 500),
                        )
                        self._bool_editor(
                            target,
                            "detect_inclusions",
                            "Detect inclusions",
                            bool(self.settings["cellpose_detect_inclusions"]),
                        )
                        self._bool_editor(target, "overlay_masks", "Show masks", True)
                        self._bool_editor(target, "overlay_inclusions", "Show inclusions", True)
                    with ui.element("div").classes("correction-grid"):
                        self._counter_editor(target, "disabled_droplets", "Disabled droplets")
                        self._counter_editor(target, "added_inclusions", "Added inclusions")
                    ui.label("Correction edits will apply to raw droplet rows in View Results.").classes(
                        "editor-note"
                    )

    def _render_droplet_plots(self, summaries: list[RawSummary]) -> None:
        from nicegui import ui

        with ui.element("div").classes("comparison-grid w-full"):
            _plot_card(_diameter_hist_chart(summaries))
            _plot_card(_diameter_chart(summaries))
            _plot_card(_cv_chart(summaries))
            _plot_card(_frequency_chart(summaries))
            _plot_card(_position_scatter(summaries))
            _plot_card(_track_timeline_chart(summaries))
            _plot_card(_perimeter_time_chart(summaries))
            _plot_card(_area_position_chart(summaries))
            if _has_inclusions(summaries):
                _plot_card(_inclusion_chart(summaries))

    def _render_engine_plots(self, engine: str) -> None:
        from nicegui import ui

        summaries = [summary for summary in self._raw_summaries() if summary.engine == engine]
        with ui.column().classes("panel w-full gap-2 p-3"):
            ui.label("Analysis plots").classes("section-title")
            self._render_droplet_plots(summaries)
            table = ui.table(
                columns=_view_summary_columns(),
                rows=_view_summary_rows(summaries),
            ).classes("w-full").props("dense flat hide-bottom")
            self._register_table(f"{engine}_summary", table)

    def _render_analysis_runs(self) -> None:
        from nicegui import ui

        with ui.column().classes("panel w-full gap-2 p-3"):
            ui.label("Stored analysis runs").classes("section-title")
            rows = self._analysis_run_rows()
            table = ui.table(
                columns=[
                    {"name": "project", "label": "Project", "field": "project", "align": "left"},
                    {"name": "run_id", "label": "Run", "field": "run_id", "align": "left"},
                    {"name": "jobs", "label": "Jobs", "field": "jobs", "align": "right"},
                    {"name": "raw", "label": "Raw", "field": "raw", "align": "left"},
                ],
                rows=rows,
            ).classes("w-full").props("dense flat hide-bottom")
            self._register_table("analysis_runs", table)
            if not rows:
                ui.label("No stored analysis runs found for the matrix projects.").classes("muted text-xs")

    def _render_results(self) -> None:
        from nicegui import ui

        with ui.column().classes("control-box w-full"):
            ui.label("Results").classes("control-box-title")
            with ui.column().classes("panel w-full gap-2 p-3"):
                ui.label("Run summary").classes("section-title")
                rows = self._result_rows()
                table = ui.table(
                    columns=[
                        {"name": "sample", "label": "Sample", "field": "sample", "align": "left"},
                        {"name": "engine", "label": "Engine", "field": "engine", "align": "left"},
                        {"name": "status", "label": "Status", "field": "status", "align": "left"},
                        {"name": "rows", "label": "Rows", "field": "rows", "align": "right"},
                        {"name": "frames", "label": "Frames", "field": "frames", "align": "right"},
                        {"name": "cache", "label": "Cache", "field": "cache", "align": "left"},
                    ],
                    rows=rows,
                ).classes("w-full").props("dense flat hide-bottom")
                self._register_table("run_summary", table)
                if not rows:
                    ui.label("No results yet. Run OpenCV, Cellpose, or Run All.").classes("muted text-xs")

    def _render_log(self) -> None:
        from nicegui import ui

        with ui.column().classes("control-box w-full"):
            ui.label("Action Log").classes("control-box-title")
            ui.html("<br>".join(self.action_log[-80:])).classes("log-text w-full")

    def _project_file_rows(self) -> list[dict[str, str]]:
        rows = []
        for project_path in self._project_paths():
            manifest = project_path / "manifest.json"
            if not manifest.is_file():
                continue
            try:
                store = ProjectStore(project_path)
            except Exception:
                continue
            for file in store.session.files:
                if file.role not in {"control_video", "analysis_video", "analysis_image_dir"}:
                    continue
                rows.append(
                    {
                        "project": project_path.name,
                        "role": file.role,
                        "engine": str(file.metadata.get("engine") or ""),
                        "path": file.path,
                    }
                )
        return rows

    def _recording_inventory_rows(self) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for run in self._fluidics_runs():
            row = grouped.setdefault(
                run.project,
                {"project": run.project, "recordings": 0, "csv_rows": 0, "duration_s": 0.0},
            )
            row["recordings"] += 1
            row["csv_rows"] += len(run.rows)
            row["duration_s"] += _fluidics_duration(run)
        return [
            {
                **row,
                "duration_s": f"{float(row['duration_s']):.1f}",
            }
            for row in grouped.values()
        ]

    def _stored_runs(self) -> list[StoredRun]:
        return _load_stored_runs(self._project_paths())

    def _raw_summaries(self) -> list[RawSummary]:
        summaries = []
        for run in self._stored_runs():
            rows = read_raw_rows(run.project_path, run.raw_path)
            summaries.extend(summarize_raw_rows(run, rows))
        return summaries

    def _fluidics_runs(self) -> list[FluidicsRun]:
        return _load_fluidics_runs(self._project_paths())

    def _project_paths(self) -> list[Path]:
        paths = {session_path(self.project_path)}
        paths.update(session_path(row.project_path) for row in self.matrix)
        return sorted(paths)

    def _selected_row_for_engine(self, engine: str) -> MatrixRow | None:
        selected = self._selected_row()
        if selected is not None and selected.engine == engine and selected.active:
            return selected
        return next((row for row in self.matrix if row.active and row.engine == engine), None)

    def _bool_editor(
        self,
        row: MatrixRow,
        key: str,
        label: str,
        default: Any,
    ) -> None:
        from nicegui import ui

        ui.checkbox(
            label,
            value=_row_bool(row, key, default),
            on_change=lambda event, item=row, name=key: self._set_row_setting(
                item,
                name,
                bool(event.value),
            ),
        ).classes("compact-checkbox")

    def _counter_editor(self, row: MatrixRow, key: str, label: str) -> None:
        from nicegui import ui

        with ui.column().classes("counter-card"):
            ui.label(label).classes("muted text-xs font-semibold")
            ui.label(str(_row_int(row, key, 0))).classes("counter-value")
            with ui.row().classes("w-full gap-1"):
                ui.button(
                    "-",
                    on_click=lambda item=row, name=key: self._increment_row_counter(item, name, -1),
                ).props("dense no-caps outline").classes("grow")
                ui.button(
                    "+",
                    on_click=lambda item=row, name=key: self._increment_row_counter(item, name, 1),
                ).props("dense no-caps outline").classes("grow")

    def _open_project_browser(self) -> None:
        start = self._project_path()
        root = start if start.is_dir() else projects_root(self.discovery_root)
        self._open_path_browser(
            title="Load project manifest",
            start=root,
            mode="project",
            row_uid=None,
        )

    def _open_source_browser(self) -> None:
        selected = self._selected_row()
        source = selected.source_path if selected is not None else self.source_path
        start = Path(source).parent if source else self._project_path().parent
        self._open_path_browser(
            title="Add sources",
            start=start,
            mode="source",
            row_uid=None,
            multi=True,
        )

    def _browse_matrix_source(self, event: Any) -> None:
        row = _event_row(event)
        uid = str(row.get("uid") or "")
        selected = next((item for item in self.matrix if item.uid == uid), None)
        start = Path(selected.source_path).parent if selected is not None else self._project_path().parent
        self._open_path_browser(title="Select source", start=start, mode="source", row_uid=uid or None)

    def _open_path_browser(
        self,
        *,
        title: str,
        start: Path,
        mode: str,
        row_uid: str | None,
        multi: bool = False,
    ) -> None:
        from nicegui import ui

        state = {"path": _existing_dir(start)}
        selected: set[str] = set()
        dialog = ui.dialog().classes("browser-dialog")
        with dialog, ui.card().classes("browser-card"):
            header = ui.label(title).classes("section-title")
            path_label = ui.label(str(state["path"])).classes("browser-path")
            rows = ui.column().classes("browser-list w-full gap-1")
            with ui.row().classes("w-full gap-2 justify-end items-center"):
                status = ui.label("").classes("muted text-xs mr-auto")
                if multi:
                    ui.button("Select All", on_click=lambda: select_all()).props("dense no-caps outline")
                    ui.button("Cancel", on_click=dialog.close).props("dense no-caps outline")
                    ui.button("Add & Close", on_click=lambda: finish()).props("dense no-caps")
                else:
                    if mode == "source":
                        ui.button(
                            "Use This Folder",
                            on_click=lambda: self._select_browser_path(state["path"], mode, row_uid, dialog),
                        ).props("dense no-caps outline")
                    ui.button("Cancel", on_click=dialog.close).props("dense no-caps outline")

        def toggle(path: Path) -> None:
            key = str(Path(path).expanduser().resolve())
            selected.discard(key) if key in selected else selected.add(key)
            status.set_text(f"{len(selected)} selected")
            render_entries()

        def select_all() -> None:
            for entry in _browser_entries(state["path"], mode):
                selected.add(str(entry.expanduser().resolve()))
            status.set_text(f"{len(selected)} selected")
            render_entries()

        def finish() -> None:
            count = sum(1 for key in sorted(selected) if self._append_source(Path(key)))
            dialog.close()
            if count:
                self._notify(f"added {count} source(s).", "success")
                self._refresh()

        def render_entries() -> None:
            current = state["path"]
            path_label.set_text(str(current))
            rows.clear()
            with rows:
                parent = current.parent
                if parent != current:
                    ui.button("⬆  ..", on_click=lambda path=parent: navigate(path)).props(
                        "dense no-caps flat"
                    ).classes("browser-row browser-name")
                entries = _browser_entries(current, mode)
                if not entries:
                    ui.label("No matching entries.").classes("muted text-xs")
                    return
                # Flat rows: a select toggle on the LEFT for every file and folder,
                # then the name (folders navigate, files are labels). Same add path
                # for everything via _append_source on Add & Close.
                for entry in entries:
                    is_dir = entry.is_dir()
                    is_selected = str(entry.expanduser().resolve()) in selected
                    with ui.row().classes("browser-row w-full items-center no-wrap gap-2"):
                        if multi:
                            ui.button(
                                "✓" if is_selected else "＋",
                                on_click=lambda path=entry: toggle(path),
                            ).props(
                                "dense no-caps " + ("unelevated color=primary" if is_selected else "outline")
                            ).classes("browser-select")
                        name = entry.name + ("/" if is_dir else "")
                        if is_dir:
                            ui.button(name, on_click=lambda path=entry: navigate(path)).props(
                                "dense no-caps flat"
                            ).classes("grow browser-name")
                        elif multi:
                            ui.label(name).classes("grow browser-name")
                        else:
                            ui.button(
                                name,
                                on_click=lambda path=entry: self._select_browser_path(path, mode, row_uid, dialog),
                            ).props("dense no-caps flat").classes("grow browser-name")

        def navigate(path: Path) -> None:
            state["path"] = _existing_dir(path)
            render_entries()

        header.set_text(title)
        render_entries()
        dialog.open()

    def _select_browser_path(
        self,
        path: Path,
        mode: str,
        row_uid: str | None,
        dialog: Any,
    ) -> None:
        dialog.close()
        if mode == "project":
            manifest = path if path.name == "manifest.json" else path / "manifest.json"
            self._load_project_from_manifest(manifest)
            return
        self._upsert_source(path, row_uid=row_uid)

    def _load_project_from_manifest(self, manifest: Path) -> None:
        if manifest.name != "manifest.json" or not manifest.is_file():
            self._notify("Select a project manifest.json.", "warning")
            self._refresh()
            return
        self._load_project(manifest.parent)

    def _upsert_source(self, source: Path, *, row_uid: str | None) -> None:
        source = source.expanduser().resolve()
        try:
            engine = infer_engine(source)
        except Exception as exc:
            self._notify(f"unsupported source: {exc}", "warning")
            self._refresh()
            return

        row = next((item for item in self.matrix if item.uid == row_uid), None)
        existing = next((item for item in self.matrix if Path(item.source_path) == source), None)
        if row is None and existing is not None:
            row = existing
        if row is None:
            row = MatrixRow(
                uid=_uid(),
                project_path=str(session_path(self.project_path)),
                source_path=str(source),
                engine=engine,
                sample_id=source.stem or f"sample_{len(self.matrix) + 1}",
            )
            self.matrix.append(row)
        else:
            row.source_path = str(source)
            row.engine = engine
            if not row.sample_id or row.sample_id.startswith("sample_"):
                row.sample_id = source.stem or row.sample_id
        self.source_path = row.source_path
        self.selected_uid = row.uid
        self._link_source(source, engine, row.sample_id)
        self.stage_progress["import"] = 100
        self._mark_stage("import", StageStatus.COMPLETE)
        self._notify(f"source ready: {source.name}", "success")
        self._log(f"matrix: source {source} -> {engine}")
        self._refresh()

    def _append_source(self, source: Path) -> bool:
        source = source.expanduser().resolve()
        try:
            engine = infer_engine(source)
        except Exception as exc:
            self._notify(f"unsupported source: {exc}", "warning")
            return False
        if any(Path(item.source_path) == source for item in self.matrix):
            return False
        sample_id = source.stem or f"sample_{len(self.matrix) + 1}"
        self.matrix.append(
            MatrixRow(
                uid=_uid(),
                project_path=str(session_path(self.project_path)),
                source_path=str(source),
                engine=engine,
                sample_id=sample_id,
            )
        )
        self._link_source(source, engine, sample_id)
        self.stage_progress["import"] = 100
        self._mark_stage("import", StageStatus.COMPLETE)
        self._log(f"matrix: appended {source} -> {engine}")
        return True

    def _link_source(self, source: Path, engine: str, sample_id: str) -> None:
        # Persist the attached file into the project manifest so it is remembered
        # across sessions. External files are linked by absolute path.
        try:
            path = session_path(self.project_path)
            store = ProjectStore(path) if (path / "manifest.json").is_file() else ProjectStore.create(path, path.stem)
            store.register_analysis_file(str(source), engine=engine, sample_id=sample_id)
            store.save()
            self.project_refs = discover_projects(self.discovery_root)
        except Exception as exc:
            self._log(f"project: could not link {source.name}: {exc}")

    def _action_specs(self, stage_id: str) -> list[dict[str, Any]]:
        stage = self._stage_by_id(stage_id)
        return [self._action_spec(action) for action in stage.actions]

    def _action_spec(self, action: Any) -> dict[str, Any]:
        handler = self._action_handler(action.action)
        spec = {
            "label": action.label,
            "handler": handler,
            "active": action.variant in {"primary", "success"},
            "warning": action.variant == "warning",
            "enabled": guard_enabled(action.guard, self._guard_value),
        }
        return spec

    def _action_handler(self, action: str | None) -> Any:
        if action == "browse_source":
            return self._open_source_browser
        if action == "reset_settings":
            return self._reset_settings
        if action == "clear_matrix":
            return self._clear_matrix
        if action == "analyze":
            stage_id = self._stage_id()
            engine = "opencv" if stage_id == "video" else "cellpose" if stage_id == "imaging" else None
            return lambda: self._run_engine(engine)
        if action == "analyze_all":
            return lambda: self._run_engine(None)
        if action == "refresh_view":
            return self._refresh_view
        if action == "export_later":
            return lambda: self._notify("Export is not wired yet.", "warning")
        return lambda: None

    def _guard_value(self, guard: str) -> bool:
        if guard == "project_ready":
            return (self._project_path() / "manifest.json").is_file()
        if guard == "has_matrix_rows":
            return bool(self.matrix)
        if guard == "has_opencv_targets":
            return bool(self._targets("opencv"))
        if guard == "has_cellpose_targets":
            return bool(self._targets("cellpose"))
        return True

    def _default_settings(self) -> dict[str, Any]:
        return {
            "opencv_microns_per_pixel": 1.0,
            "opencv_fps": 0.0,
            "cellpose_px_to_um": 1.14,
            "cellpose_frame_limit": 0,
            "cellpose_detect_inclusions": True,
            "cache_root": str(Path.home() / ".admet-cache" / "admet2"),
        }

    def _reset_settings(self) -> None:
        self.settings = self._default_settings()
        self._notify("Settings reset to defaults.", "success")
        self._log("settings: reset to defaults")
        self._refresh()

    def _clear_matrix(self) -> None:
        self.matrix = []
        self.selected_uid = ""
        self._notify("Batch matrix cleared.", "success")
        self._log("matrix: cleared")
        self._refresh()

    def _new_project(self) -> None:
        path = self._project_path()
        try:
            store = ProjectStore.create(path, path.stem)
        except Exception as exc:
            self._notify(f"project create failed: {exc}", "danger")
            self._refresh()
            return
        self.project_path = str(store.path)
        self.stage_progress["import"] = max(self.stage_progress["import"], 30)
        self._mark_stage("import", StageStatus.ACTIVE)
        self._notify(f"project ready: {store.path.name}", "success")
        self._log(f"project: created {store.path}")
        self.project_refs = discover_projects(self.discovery_root)
        self._render_current_stage(force_mount=True)

    def _load_project(self, path: Path | None = None) -> None:
        path = session_path(path) if path is not None else self._project_path()
        if path.name == "manifest.json":
            path = path.parent
        if not (path / "manifest.json").is_file():
            self._notify("Project manifest not found.", "warning")
            self._refresh()
            return
        try:
            store = ProjectStore(path)
        except Exception as exc:
            self._notify(f"project load failed: {exc}", "danger")
            self._refresh()
            return
        self.project_path = str(store.path)
        self._load_project_files(store)
        self.stage_progress["import"] = 100 if self.matrix else 45
        self._mark_stage("import", StageStatus.COMPLETE if self.matrix else StageStatus.ACTIVE)
        self._notify(f"project loaded: {store.path.name}", "success")
        self._log(f"project: loaded {store.path}")
        self.project_refs = discover_projects(self.discovery_root)
        self._render_current_stage(force_mount=True)

    def _load_project_files(self, store: ProjectStore) -> None:
        existing = {row.source_path for row in self.matrix}
        for file in store.session.files:
            if file.role not in {"control_video", "analysis_video", "analysis_image_dir"}:
                continue
            engine = str(file.metadata.get("engine") or ("cellpose" if file.role == "analysis_image_dir" else "opencv"))
            source_path = str(store.path / file.path) if not Path(file.path).is_absolute() else file.path
            if source_path in existing:
                continue
            self.matrix.append(
                MatrixRow(
                    uid=_uid(),
                    project_path=str(store.path),
                    source_path=source_path,
                    engine=engine,
                    sample_id=str(file.metadata.get("sample_id") or Path(file.path).stem),
                )
            )

    async def _run_engine(self, engine: str | None) -> None:
        from nicegui import run, ui

        targets = self._targets(engine)
        if not targets:
            label = engine or "analyze"
            self._notify(f"No active {label} files.", "warning")
            self._refresh()
            return
        jobs = tuple(self._target_to_run(row) for row in targets)
        # Run the batch off the event loop so heavy cellpose/opencv work does not
        # freeze the UI. A timer polls per-file progress reported by the runner and
        # drives the action-box progress bar live.
        self._run_progress = 0
        self._notify(f"Running {engine or 'analysis'} on {len(jobs)} file(s)…", "primary")
        self._log(f"analysis: running {engine or 'all'} ({len(jobs)} file(s))")
        self._refresh()
        progress = self._refs.get("action_progress")
        timer = ui.timer(
            0.25,
            lambda: progress.set_value(self._run_progress / 100.0) if progress is not None else None,
        )
        try:
            report = await run.io_bound(self._run_batch, jobs)
        except Exception as exc:
            self._notify(f"analysis failed: {exc}", "danger")
            self._log(f"analysis: {type(exc).__name__}: {exc}")
            self._refresh()
            return
        finally:
            timer.cancel()

        self.last_report = report
        engines = {job.engine for job in report.jobs}
        if "opencv" in engines:
            self.stage_progress["video"] = 100
            self._mark_stage("video", StageStatus.COMPLETE)
        if "cellpose" in engines:
            self.stage_progress["imaging"] = 100
            self._mark_stage("imaging", StageStatus.COMPLETE)
        self.stage_progress["view"] = 100
        self._mark_stage("view", StageStatus.ACTIVE)
        self._notify(f"analysis complete: {len(report.jobs)} job(s).", "success")
        self._log(f"analysis: complete {len(report.jobs)} job(s)")
        self._refresh()

    def _run_batch(self, jobs: tuple[AnalyzeTarget, ...]) -> AnalyzeBatchReport:
        def on_progress(percent: float) -> None:
            self._run_progress = percent

        runner = AnalyzeBatchRunner(self.registry, cache_root=self.settings["cache_root"])
        return runner.run(jobs, on_progress=on_progress)

    def _target_to_run(self, row: MatrixRow) -> AnalyzeTarget:
        settings = self._engine_settings(row)
        return AnalyzeTarget(
            project_path=Path(row.project_path),
            source_path=self._resolve_media_path(row.source_path, row.project_path),
            engine=row.engine,
            sample_id=row.sample_id,
            settings=settings,
            cache_policy=row.cache_policy,
        )

    def _engine_settings(self, row: MatrixRow) -> dict[str, Any]:
        if row.engine == "cellpose":
            frame_limit = _row_int(row, "frame_limit", self.settings["cellpose_frame_limit"])
            return {
                "px_to_um": _row_float(row, "px_to_um", self.settings["cellpose_px_to_um"]),
                "frame_limit": frame_limit or None,
                "detect_inclusions": _row_bool(
                    row,
                    "detect_inclusions",
                    self.settings["cellpose_detect_inclusions"],
                ),
            }
        end_frame = _row_int(row, "end_frame", 0)
        source = self._resolve_media_path(row.source_path, row.project_path)
        metadata = _video_metadata(source)
        roi_width = _row_int(row, "roi_width", 0)
        roi_height = _row_int(row, "roi_height", 0)
        settings = {
            "microns_per_pixel": _row_float(
                row,
                "microns_per_pixel",
                self.settings["opencv_microns_per_pixel"],
            ),
            "fps": _row_float(row, "fps", self.settings["opencv_fps"]),
            "start_frame": _row_int(row, "start_frame", 0),
            "end_frame": end_frame or None,
            "roi_x": 0,
            "roi_y": 0,
            "roi_width": 0,
            "roi_height": 0,
        }
        if roi_width or roi_height:
            settings.update(
                {
                    "roi_x": _row_int(row, "roi_x", 0),
                    "roi_y": _row_int(row, "roi_y", 0),
                    "roi_width": roi_width or max(int(metadata.get("width") or 0), 0),
                    "roi_height": roi_height or max(int(metadata.get("height") or 0), 0),
                }
            )
        return settings

    def _refresh_view(self) -> None:
        self.stage_progress["view"] = 100 if self._analysis_run_rows() else 0
        self._notify("view refreshed.", "success")
        self._render_current_stage(force_mount=True)

    def _analysis_run_rows(self) -> list[dict[str, Any]]:
        rows = []
        for project_path in sorted({row.project_path for row in self.matrix} | {self.project_path}):
            path = session_path(project_path)
            metadata_path = path / "analysis" / "metadata.json"
            if not metadata_path.is_file():
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for run in metadata.get("runs", []):
                if not isinstance(run, dict):
                    continue
                raw = str(run.get("raw_path") or "")
                rows.append(
                    {
                        "project": path.name,
                        "run_id": str(run.get("run_id") or ""),
                        "jobs": len((run.get("metadata") or {}).get("jobs", [])),
                        "raw": raw,
                    }
                )
        return rows

    def _result_rows(self) -> list[dict[str, Any]]:
        if self.last_report is None:
            return []
        rows = []
        for job in self.last_report.jobs:
            metadata = job.metadata
            true_stats = metadata.get("true_stats") if isinstance(metadata.get("true_stats"), dict) else {}
            mean_d = _numeric(metadata.get("mean_diameter_um"))
            std_d = _numeric(metadata.get("std_diameter_um"))
            cv = (std_d / mean_d * 100.0) if mean_d and std_d else None
            rows.append(
                {
                    "sample": job.sample_id,
                    "engine": job.engine,
                    "status": job.status,
                    "droplets": metadata.get("total_droplets", ""),
                    "mean_um": _fmt(mean_d),
                    "cv": _fmt(cv),
                    "speed": _fmt(metadata.get("mean_speed_mm_s")),
                    "freq": _fmt(metadata.get("frequency_hz")),
                    "volume_nl": _fmt(true_stats.get("droplet_volume_nl"), 3),
                    "threshold": metadata.get("threshold", ""),
                }
            )
        return rows

    def _matrix_row(self, row: MatrixRow) -> dict[str, Any]:
        return {
            "uid": row.uid,
            "project": Path(row.project_path).name,
            "project_path": row.project_path,
            "source": Path(row.source_path).name or row.source_path,
            "source_path": row.source_path,
            "engine": row.engine,
            "sample_id": row.sample_id,
            "active": row.active,
        }

    def _schema_matrix_row(self, row: MatrixRow, params: tuple[Param, ...]) -> dict[str, Any]:
        data = {
            "uid": row.uid,
            "sample_id": row.sample_id,
            "source": Path(row.source_path).name or row.source_path,
        }
        for param in params:
            data[param.name] = _schema_cell_value(row, param)
        return data

    def _wire_schema_matrix(self, table: Any, params: tuple[Param, ...]) -> None:
        table.add_slot("body-cell-sample_id", _text_cell_slot("sample_id"))
        for param in params:
            table.add_slot(f"body-cell-{param.name}", _schema_cell_slot(param))
        table.on("matrix-change", self._handle_matrix_change)

    def _targets(self, engine: str | None = None) -> list[MatrixRow]:
        return [
            row
            for row in self.matrix
            if row.active and (engine is None or row.engine == engine)
        ]

    def _selected_row(self) -> MatrixRow | None:
        return next((row for row in self.matrix if row.uid == self.selected_uid), None)

    def _select_row_event(self, event: Any) -> None:
        row = _event_row(event)
        uid = str(row.get("uid") or "")
        if uid:
            self.selected_uid = uid
            selected = self._selected_row()
            if selected is not None:
                self.source_path = selected.source_path
                self.project_path = selected.project_path
            self._refresh()

    def _handle_matrix_change(self, event: Any) -> None:
        payload = getattr(event, "args", {})
        if not isinstance(payload, dict):
            return
        uid = str(payload.get("uid") or "")
        field = str(payload.get("field") or "")
        value = payload.get("value")
        row = next((item for item in self.matrix if item.uid == uid), None)
        if row is None:
            return
        if field == "active":
            row.active = bool(value)
            return
        if field == "sample_id":
            row.sample_id = str(value or "")
            return
        kind = _FIELD_KINDS.get(field)
        if kind is None:
            return
        row.settings[field] = _cast_by_kind(kind, value)

    def _set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value

    def _set_row_setting(self, row: MatrixRow, key: str, value: Any) -> None:
        row.settings[key] = value
        self.selected_uid = row.uid
        self._refresh()

    def _increment_row_counter(self, row: MatrixRow, key: str, delta: int) -> None:
        row.settings[key] = max(0, _row_int(row, key, 0) + delta)
        self.selected_uid = row.uid
        self._refresh()

    def _project_options(self) -> dict[str, str]:
        return {str(ref.path): _project_ref_label(ref) for ref in self.project_refs}

    def _select_project(self, value: str | None) -> None:
        if not value:
            return
        self._load_project(session_path(value))

    def _refresh_projects(self) -> None:
        self.project_refs = discover_projects(self.discovery_root)
        self._notify(f"found {len(self.project_refs)} project(s).", "success")
        self._refresh()

    def _activate(self, index: int) -> None:
        index = max(0, min(index, len(self.workflow.stages) - 1))
        statuses = dict(self.state.statuses)
        current_id = self._stage_id()
        if statuses.get(current_id) is StageStatus.ACTIVE:
            statuses[current_id] = StageStatus.PENDING
        stage_id = self.workflow.stages[index].id
        if statuses.get(stage_id) is StageStatus.PENDING:
            statuses[stage_id] = StageStatus.ACTIVE
        self.state = replace(self.state, index=index, statuses=statuses)
        self._refresh()

    def _mark_stage(self, stage_id: str, status: StageStatus) -> None:
        statuses = dict(self.state.statuses)
        statuses[stage_id] = status
        self.state = replace(self.state, statuses=statuses)

    def _stage_id(self) -> str:
        return self._stage().id

    def _stage(self) -> Any:
        return current_stage(self.workflow, self.state)

    def _stage_by_id(self, stage_id: str) -> Any:
        return stage_by_id(self.workflow, self.state, stage_id)

    def _project_path(self) -> Path:
        value = str(self.project_path or "").strip()
        if not value:
            value = str(self._default_project_path())
            self.project_path = value
        return session_path(value)

    def _default_project_path(self) -> Path:
        return projects_root(self.discovery_root) / f"admet_{time.strftime('%Y%m%d_%H%M%S')}.admetp"

    def _instruction(self) -> str:
        return instruction_text(self._stage(), self._guard_value)

    def _notify(self, message: str, kind: str) -> None:
        self.notice = message
        self.notice_kind = kind

    def _log(self, message: str) -> None:
        self.action_log.append(f"{time.strftime('%H:%M:%S')} {message}")

    def _refresh(self) -> None:
        self._render_current_stage()


def _matrix_columns() -> list[dict[str, Any]]:
    return [
        {"name": "active", "label": "Use", "field": "active", "align": "center"},
        {"name": "project", "label": "Project", "field": "project", "align": "left"},
        {"name": "source", "label": "Source", "field": "source", "align": "left"},
        {"name": "engine", "label": "Engine", "field": "engine", "align": "left"},
        {"name": "sample_id", "label": "Sample ID", "field": "sample_id", "align": "left"},
    ]


# The per-engine matrix is generated from the engine's ParamSchema: one column per
# editable Param, its `kind` chooses the cell widget and the value cast. Declare a Param
# once in the engine settings and it appears, editable, in the matrix.
_ENGINE_SCHEMAS = {"opencv": OPENCV_SETTINGS, "cellpose": CELLPOSE_SETTINGS}
_MATRIX_HIDDEN_PARAMS = {"video_path", "input_dir"}
_FIELD_KINDS = {param.name: param.kind for schema in _ENGINE_SCHEMAS.values() for param in schema.params}


def _matrix_params(engine: str) -> tuple[Param, ...]:
    schema = _ENGINE_SCHEMAS.get(engine)
    if schema is None:
        return ()
    return tuple(param for param in schema.params if param.name not in _MATRIX_HIDDEN_PARAMS)


def _cast_by_kind(kind: ParamKind, value: Any) -> Any:
    if kind is ParamKind.BOOLEAN:
        return bool(value)
    if kind is ParamKind.INTEGER:
        return int(_numeric(value) or 0)
    if kind is ParamKind.FLOAT:
        return float(_numeric(value) or 0.0)
    return str(value or "")


def _schema_matrix_columns(params: tuple[Param, ...]) -> list[dict[str, Any]]:
    columns = [
        {"name": "sample_id", "label": "Sample", "field": "sample_id", "align": "left"},
        {"name": "source", "label": "Source", "field": "source", "align": "left"},
    ]
    for param in params:
        align = "right" if param.kind in {ParamKind.INTEGER, ParamKind.FLOAT} else "left"
        columns.append({"name": param.name, "label": param.label, "field": param.name, "align": align})
    return columns


def _schema_cell_value(row: MatrixRow, param: Param) -> Any:
    default = param.default
    if param.kind is ParamKind.BOOLEAN:
        return _row_bool(row, param.name, bool(default))
    if param.kind is ParamKind.INTEGER:
        return _row_int(row, param.name, int(default) if default is not None else 0)
    if param.kind is ParamKind.FLOAT:
        return _row_float(row, param.name, float(default) if default is not None else 0.0)
    return str(_row_setting(row, param.name, default if default is not None else ""))


def _numeric_cell_slot(field: str) -> str:
    return f"""
    <q-td :props="props">
      <q-input dense outlined type="number" input-class="matrix-num"
        v-model.number="props.row.{field}"
        @click.stop @mousedown.stop
        @blur="$parent.$emit('matrix-change', {{uid: props.row.uid, field: '{field}', value: props.row.{field}}})"
        @keyup.enter="$event.target.blur()" />
    </q-td>
    """


def _text_cell_slot(field: str) -> str:
    return f"""
    <q-td :props="props">
      <q-input dense outlined v-model="props.row.{field}"
        @click.stop @mousedown.stop
        @blur="$parent.$emit('matrix-change', {{uid: props.row.uid, field: '{field}', value: props.row.{field}}})"
        @keyup.enter="$event.target.blur()" />
    </q-td>
    """


def _bool_cell_slot(field: str) -> str:
    return f"""
    <q-td :props="props">
      <q-checkbox dense v-model="props.row.{field}"
        @click.stop @mousedown.stop
        @update:model-value="val => $parent.$emit('matrix-change', {{uid: props.row.uid, field: '{field}', value: val}})" />
    </q-td>
    """


def _schema_cell_slot(param: Param) -> str:
    if param.kind is ParamKind.BOOLEAN:
        return _bool_cell_slot(param.name)
    if param.kind in {ParamKind.INTEGER, ParamKind.FLOAT}:
        return _numeric_cell_slot(param.name)
    return _text_cell_slot(param.name)


def _project_ref_label(ref: ProjectRef) -> str:
    return (
        f"{ref.project_id} · {(ref.updated or 'unknown')[:10]} · "
        f"{ref.file_count} files / {ref.run_count} runs"
    )


def _load_stored_runs(project_paths: list[Path]) -> list[StoredRun]:
    runs = []
    for project_path in project_paths:
        metadata_path = project_path / "analysis" / "metadata.json"
        if not metadata_path.is_file():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for run in metadata.get("runs", []):
            if not isinstance(run, dict):
                continue
            raw_path = _resolve_project_path(project_path, str(run.get("raw_path") or ""))
            run_metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
            jobs = tuple(job for job in run_metadata.get("jobs", ()) if isinstance(job, dict))
            runs.append(
                StoredRun(
                    project_path=project_path,
                    run_id=str(run.get("run_id") or raw_path.parent.name),
                    raw_path=raw_path,
                    jobs=jobs,
                )
            )
    return runs


def read_raw_rows(project_path: Path, raw_path: Path, *, limit: int = 50_000) -> list[dict[str, Any]]:
    path = raw_path if raw_path.is_absolute() else project_path / raw_path
    if not path.is_file():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if len(rows) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _job_metadata_by_sample(run: StoredRun) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for job in run.jobs:
        engine = str(job.get("engine") or "")
        sample_id = str(job.get("sample_id") or "sample")
        result[(engine, sample_id)] = _as_dict(job.get("metadata"))
    return result


def summarize_raw_rows(run: StoredRun, rows: list[dict[str, Any]]) -> list[RawSummary]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    contexts: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        engine = str(row.get("engine") or "")
        sample_id = str(row.get("item_id") or row.get("sample_id") or row.get("file_id") or "sample")
        key = (engine, sample_id)
        if row.get("kind") == "run_context":
            contexts[key] = _row_values(row)
            continue
        groups.setdefault(key, []).append(row)

    job_meta = _job_metadata_by_sample(run)
    summaries = []
    seen: set[tuple[str, str]] = set()
    for key, group_rows in sorted(groups.items()):
        engine, sample_id = key
        seen.add(key)
        context = contexts.get(key, {})
        analysis = _as_dict(context.get("analysis"))
        meta = job_meta.get(key, {})
        true_stats = _as_dict(meta.get("true_stats")) or _as_dict(context.get("true_stats"))
        scale = _numeric(analysis.get("microns_per_pixel")) or _numeric(true_stats.get("scale_um_px")) or 1.0

        frames = {
            int(float(values.get("frame")))
            for values in (_row_values(row) for row in group_rows)
            if _is_number(values.get("frame"))
        }
        diameters: list[float] = []
        detections: list[tuple[float, float, float, float, float]] = []
        spans: dict[float, list[float]] = {}
        inclusion_counts: list[int] = []
        for row in group_rows:
            kind = row.get("kind")
            if kind not in {"detection", "droplet", "track"}:
                continue
            values = _row_values(row)
            diameter = _numeric(values.get("diameter_um"))
            if diameter is None:
                pixels = _numeric(values.get("equivalent_diameter") or values.get("diameter"))
                diameter = pixels * scale if pixels is not None else None
            if diameter is not None and math.isfinite(diameter) and diameter > 0:
                diameters.append(diameter)
            if kind == "droplet":
                inclusion_counts.append(int(_numeric(values.get("inclusions")) or 0))
            frame = _numeric(values.get("frame"))
            cx = _numeric(values.get("centroid_x"))
            cy = _numeric(values.get("centroid_y"))
            if frame is not None and cx is not None and cy is not None and len(detections) < 6000:
                area = _numeric(values.get("area")) or 0.0
                perimeter = _numeric(values.get("perimeter")) or 0.0
                detections.append(
                    (frame, round(cx, 1), round(cy, 1), round(area * scale * scale, 2), round(perimeter * scale, 2))
                )
            droplet_id = _numeric(values.get("droplet_id") or values.get("track_id"))
            if droplet_id is not None and frame is not None:
                bounds = spans.setdefault(droplet_id, [frame, frame])
                bounds[0] = min(bounds[0], frame)
                bounds[1] = max(bounds[1], frame)

        track_count = sum(1 for row in group_rows if row.get("kind") == "track")
        detection_count = sum(1 for row in group_rows if row.get("kind") in {"detection", "droplet"})
        droplets = int(_numeric(meta.get("total_droplets")) or 0) or track_count or detection_count
        inclusions = sum(inclusion_counts)
        mean_d = float(_numeric(meta.get("mean_diameter_um")) or (mean(diameters) if diameters else 0.0))
        median_d = median(diameters) if diameters else mean_d
        std_d = float(_numeric(meta.get("std_diameter_um")) or (pstdev(diameters) if len(diameters) > 1 else 0.0))
        summaries.append(
            RawSummary(
                project=run.project_path.name,
                run_id=run.run_id,
                sample_id=sample_id,
                engine=engine,
                rows=len(group_rows),
                frames=len(frames),
                droplets=droplets,
                mean_diameter=mean_d,
                median_diameter=median_d,
                std_diameter=std_d,
                cv_percent=(std_d / mean_d * 100.0) if mean_d else 0.0,
                inclusions=inclusions,
                volume_nl=float(_numeric(true_stats.get("droplet_volume_nl")) or 0.0),
                true_count=float(_numeric(true_stats.get("true_count")) or 0.0),
                frequency_hz=float(_numeric(meta.get("frequency_hz") or true_stats.get("true_frequency_hz")) or 0.0),
                threshold=float(_numeric(meta.get("threshold") or context.get("threshold")) or 0.0),
                diameters=tuple(diameters),
                detections=tuple(detections),
                spans=tuple((did, lo, hi) for did, (lo, hi) in sorted(spans.items())),
                inclusion_counts=tuple(inclusion_counts),
            )
        )

    for key, meta in sorted(job_meta.items()):
        if key in seen:
            continue
        engine, sample_id = key
        true_stats = _as_dict(meta.get("true_stats"))
        mean_d = float(_numeric(meta.get("mean_diameter_um")) or 0.0)
        std_d = float(_numeric(meta.get("std_diameter_um")) or 0.0)
        summaries.append(
            RawSummary(
                project=run.project_path.name,
                run_id=run.run_id,
                sample_id=sample_id,
                engine=engine,
                rows=int(_numeric(meta.get("row_count")) or 0),
                frames=int(_numeric(meta.get("frames_processed")) or 0),
                droplets=int(_numeric(meta.get("total_droplets") or meta.get("total_detections")) or 0),
                mean_diameter=mean_d,
                median_diameter=mean_d,
                std_diameter=std_d,
                cv_percent=(std_d / mean_d * 100.0) if mean_d else 0.0,
                inclusions=0,
                volume_nl=float(_numeric(true_stats.get("droplet_volume_nl")) or 0.0),
                true_count=float(_numeric(true_stats.get("true_count")) or 0.0),
                frequency_hz=float(_numeric(meta.get("frequency_hz") or true_stats.get("true_frequency_hz")) or 0.0),
                threshold=float(_numeric(meta.get("threshold")) or 0.0),
            )
        )
    return summaries


def _load_fluidics_runs(project_paths: list[Path]) -> list[FluidicsRun]:
    runs = []
    for project_path in project_paths:
        metadata_path = project_path / "records" / "metadata.json"
        if not metadata_path.is_file():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for recording in metadata.get("recordings", []):
            if not isinstance(recording, dict):
                continue
            csv_path = _resolve_project_path(project_path, str(recording.get("fluidics_csv") or ""))
            rows = tuple(_read_fluidics_csv(csv_path))
            runs.append(
                FluidicsRun(
                    project=project_path.name,
                    recording_id=str(recording.get("recording_id") or recording.get("video_prefix") or csv_path.stem),
                    rows=rows,
                    metadata=recording,
                )
            )
    return runs


def _read_fluidics_csv(csv_path: Path, *, limit: int = 25_000) -> list[dict[str, float]]:
    if not csv_path.is_file():
        return []
    rows = []
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if len(rows) >= limit:
                break
            rows.append(
                {
                    "elapsed_s": float_or_zero(row.get("elapsed_s")),
                    "pressure": _mean_columns(row, "pressure_"),
                    "flow": _mean_columns(row, "flow_"),
                }
            )
    return rows


def _fmt(value: Any, digits: int = 2) -> str:
    number = _numeric(value)
    if number is None or not math.isfinite(number) or number == 0:
        return ""
    return f"{number:.{digits}f}"


def _result_columns() -> list[dict[str, Any]]:
    return [
        {"name": "sample", "label": "Sample", "field": "sample", "align": "left"},
        {"name": "engine", "label": "Engine", "field": "engine", "align": "left"},
        {"name": "status", "label": "Status", "field": "status", "align": "left"},
        {"name": "droplets", "label": "Droplets", "field": "droplets", "align": "right"},
        {"name": "mean_um", "label": "Mean Ø µm", "field": "mean_um", "align": "right"},
        {"name": "cv", "label": "CV %", "field": "cv", "align": "right"},
        {"name": "speed", "label": "Speed mm/s", "field": "speed", "align": "right"},
        {"name": "freq", "label": "Freq Hz", "field": "freq", "align": "right"},
        {"name": "volume_nl", "label": "Vol nL", "field": "volume_nl", "align": "right"},
        {"name": "threshold", "label": "Thr", "field": "threshold", "align": "right"},
    ]


def _summary_table_rows(summaries: list[RawSummary]) -> list[dict[str, Any]]:
    return [
        {
            "sample": summary.sample_id,
            "rows": summary.rows,
            "frames": summary.frames,
            "droplets": summary.droplets,
            "mean_diameter": f"{summary.mean_diameter:.2f}" if summary.mean_diameter else "",
            "cv_percent": f"{summary.cv_percent:.2f}" if summary.cv_percent else "",
        }
        for summary in summaries
    ]


def _fluidics_rows(runs: list[FluidicsRun]) -> list[dict[str, Any]]:
    rows = []
    for run in runs:
        pressures = [row["pressure"] for row in run.rows if math.isfinite(row["pressure"])]
        flows = [row["flow"] for row in run.rows if math.isfinite(row["flow"])]
        rows.append(
            {
                "project": run.project,
                "recording": run.recording_id,
                "rows": len(run.rows),
                "duration_s": f"{_fluidics_duration(run):.1f}",
                "mean_pressure": f"{mean(pressures):.2f}" if pressures else "",
                "mean_flow": f"{mean(flows):.2f}" if flows else "",
            }
        )
    return rows


def _diameter_chart(summaries: list[RawSummary]) -> dict[str, Any]:
    return _bar_chart(
        "Mean diameter",
        [summary.sample_id for summary in summaries],
        [round(summary.mean_diameter, 3) for summary in summaries],
        design.PALETTE.accent,
    )


def _count_chart(summaries: list[RawSummary]) -> dict[str, Any]:
    return _bar_chart(
        "Droplet rows",
        [summary.sample_id for summary in summaries],
        [summary.droplets for summary in summaries],
        design.PALETTE.success_hover,
    )


def _cv_chart(summaries: list[RawSummary]) -> dict[str, Any]:
    return _bar_chart(
        "CV %",
        [summary.sample_id for summary in summaries],
        [round(summary.cv_percent, 3) for summary in summaries],
        design.PALETTE.danger_hover,
    )


def _frequency_chart(summaries: list[RawSummary]) -> dict[str, Any]:
    return _bar_chart(
        "Frequency (Hz)",
        [summary.sample_id for summary in summaries],
        [round(summary.frequency_hz, 2) for summary in summaries],
        design.PALETTE.warning,
    )


def _diameter_hist_chart(summaries: list[RawSummary]) -> dict[str, Any]:
    values = [value for summary in summaries for value in summary.diameters]
    if not values:
        return _bar_chart("Diameter distribution (µm)", [], [], design.PALETTE.accent)
    low = min(values)
    high = max(values)
    if high <= low:
        high = low + 1.0
    bins = 24
    width = (high - low) / bins
    counts = [0] * bins
    for value in values:
        index = min(bins - 1, int((value - low) / width))
        counts[index] += 1
    labels = [f"{low + (index + 0.5) * width:.1f}" for index in range(bins)]
    return {
        "title": {"text": "Diameter distribution (µm)", "left": 8, "top": 4, "textStyle": {"fontSize": 13}},
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 48, "right": 12, "top": 36, "bottom": 46},
        "xAxis": {"type": "category", "data": labels, "axisLabel": {"rotate": 45, "fontSize": 9}},
        "yAxis": {"type": "value", "name": "count"},
        "series": [{"type": "bar", "data": counts, "itemStyle": {"color": design.PALETTE.accent}}],
    }


def _scatter_chart(title: str, xname: str, yname: str, summaries, project, *, invert_y: bool = False) -> dict[str, Any]:
    series = []
    for summary in summaries[:6]:
        data = [point for point in (project(detection) for detection in summary.detections) if point is not None]
        if data:
            series.append({"name": summary.sample_id, "type": "scatter", "symbolSize": 3, "data": data})
    chart: dict[str, Any] = {
        "title": {"text": title, "left": 8, "top": 4, "textStyle": {"fontSize": 13}},
        "tooltip": {},
        "legend": {"top": 4, "right": 8, "textStyle": {"fontSize": 9}},
        "grid": {"left": 52, "right": 12, "top": 36, "bottom": 40},
        "xAxis": {"type": "value", "name": xname},
        "yAxis": {"type": "value", "name": yname, "inverse": invert_y},
        "series": series,
    }
    return chart


def _position_scatter(summaries: list[RawSummary]) -> dict[str, Any]:
    return _scatter_chart(
        "Droplet positions (px)", "x", "y", summaries,
        lambda d: [d[1], d[2]], invert_y=True,
    )


def _perimeter_time_chart(summaries: list[RawSummary]) -> dict[str, Any]:
    return _scatter_chart(
        "Perimeter over time (µm)", "frame", "µm", summaries,
        lambda d: [d[0], d[4]] if d[4] else None,
    )


def _area_position_chart(summaries: list[RawSummary]) -> dict[str, Any]:
    return _scatter_chart(
        "Area by x position (µm²)", "x", "µm²", summaries,
        lambda d: [d[1], d[3]] if d[3] else None,
    )


def _track_timeline_chart(summaries: list[RawSummary]) -> dict[str, Any]:
    data = []
    for summary in summaries[:3]:
        for droplet_id, first_frame, last_frame in summary.spans[:600]:
            data.append({"coords": [[first_frame, droplet_id], [last_frame, droplet_id]]})
    return {
        "title": {"text": "Droplet timeline (frame)", "left": 8, "top": 4, "textStyle": {"fontSize": 13}},
        "tooltip": {},
        "grid": {"left": 52, "right": 12, "top": 36, "bottom": 40},
        "xAxis": {"type": "value", "name": "frame"},
        "yAxis": {"type": "value", "name": "droplet id"},
        "series": [
            {
                "type": "lines",
                "coordinateSystem": "cartesian2d",
                "data": data,
                "lineStyle": {"width": 2, "color": design.PALETTE.accent, "opacity": 0.7},
            }
        ],
    }


def _inclusion_chart(summaries: list[RawSummary]) -> dict[str, Any]:
    counts: dict[int, int] = {}
    for summary in summaries:
        for value in summary.inclusion_counts:
            counts[value] = counts.get(value, 0) + 1
    keys = sorted(counts)
    return _bar_chart(
        "Inclusions per droplet",
        [str(key) for key in keys],
        [counts[key] for key in keys],
        design.PALETTE.success_hover,
    )


def _has_inclusions(summaries: list[RawSummary]) -> bool:
    return any(summary.inclusion_counts for summary in summaries)


def _view_summary_columns() -> list[dict[str, Any]]:
    return [
        {"name": "sample", "label": "Sample", "field": "sample", "align": "left"},
        {"name": "engine", "label": "Engine", "field": "engine", "align": "left"},
        {"name": "droplets", "label": "Droplets", "field": "droplets", "align": "right"},
        {"name": "mean_um", "label": "Mean Ø µm", "field": "mean_um", "align": "right"},
        {"name": "median_um", "label": "Median µm", "field": "median_um", "align": "right"},
        {"name": "std_um", "label": "Std µm", "field": "std_um", "align": "right"},
        {"name": "cv", "label": "CV %", "field": "cv", "align": "right"},
        {"name": "freq", "label": "Freq Hz", "field": "freq", "align": "right"},
        {"name": "volume_nl", "label": "Vol nL", "field": "volume_nl", "align": "right"},
    ]


def _view_summary_rows(summaries: list[RawSummary]) -> list[dict[str, Any]]:
    return [
        {
            "sample": summary.sample_id,
            "engine": summary.engine,
            "droplets": summary.droplets,
            "mean_um": _fmt(summary.mean_diameter),
            "median_um": _fmt(summary.median_diameter),
            "std_um": _fmt(summary.std_diameter),
            "cv": _fmt(summary.cv_percent),
            "freq": _fmt(summary.frequency_hz),
            "volume_nl": _fmt(summary.volume_nl, 3),
        }
        for summary in summaries
    ]


def _fluidics_chart(runs: list[FluidicsRun], field: str) -> dict[str, Any]:
    series = []
    for run in runs[:8]:
        points = [
            [round(row["elapsed_s"], 3), round(row[field], 3)]
            for row in run.rows
            if math.isfinite(row["elapsed_s"]) and math.isfinite(row[field])
        ]
        if len(points) > 600:
            step = max(1, len(points) // 600)
            points = points[::step]
        series.append({"name": run.recording_id, "type": "line", "showSymbol": False, "data": points})
    return {
        "title": {"text": field.title(), "left": 8, "top": 4, "textStyle": {"fontSize": 13}},
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 44, "right": 12, "top": 36, "bottom": 34},
        "xAxis": {"type": "value", "name": "s"},
        "yAxis": {"type": "value"},
        "series": series,
    }


def _bar_chart(title: str, labels: list[str], values: list[float | int], color: str) -> dict[str, Any]:
    return {
        "title": {"text": title, "left": 8, "top": 4, "textStyle": {"fontSize": 13}},
        "tooltip": {},
        "grid": {"left": 48, "right": 12, "top": 36, "bottom": 54},
        "xAxis": {"type": "category", "data": labels, "axisLabel": {"rotate": 25}},
        "yAxis": {"type": "value"},
        "series": [{"type": "bar", "data": values, "itemStyle": {"color": color}}],
    }


def _plot_card(options: dict[str, Any]) -> None:
    from nicegui import ui

    with ui.column().classes("plot-card"):
        ui.echart(options).classes("w-full h-64")


def _video_metadata(path: Path) -> dict[str, int]:
    if not path.exists():
        return {"frames": 500, "width": 1280, "height": 720}
    try:
        import cv2
    except Exception:
        return {"frames": 500, "width": 1280, "height": 720}
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return {"frames": 500, "width": 1280, "height": 720}
        frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 500)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)
        return {"frames": max(frames, 1), "width": max(width, 1), "height": max(height, 1)}
    finally:
        capture.release()


def _read_video_frame(path: Path, frame_index: int) -> dict[str, Any]:
    if not path.exists():
        return {"error": f"Video file is not readable: {path}", "src": "", "width": 0, "height": 0}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return _decode_video_frame(str(path), mtime, int(frame_index))


@lru_cache(maxsize=128)
def _decode_video_frame(path_str: str, _mtime: float, frame_index: int) -> dict[str, Any]:
    path = Path(path_str)
    try:
        import cv2
    except Exception as exc:
        return {"error": f"OpenCV is unavailable in this environment: {exc}", "src": "", "width": 0, "height": 0}
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return {"error": f"OpenCV cannot open video: {path}", "src": "", "width": 0, "height": 0}
        if frame_index > 0:
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
        if not ok or frame is None:
            return {"error": f"OpenCV cannot read frame {frame_index} from {path}", "src": "", "width": 0, "height": 0}
        ok, encoded = cv2.imencode(".jpg", frame)
        if not ok:
            return {"error": f"OpenCV cannot encode frame {frame_index} from {path}", "src": "", "width": 0, "height": 0}
        height, width = frame.shape[:2]
        data = base64.b64encode(encoded.tobytes()).decode("ascii")
        return {"error": "", "src": f"data:image/jpeg;base64,{data}", "width": width, "height": height}
    finally:
        capture.release()


def _read_image_source(path: Path, index: int) -> dict[str, Any]:
    image_path = _select_image_path(path, index)
    if image_path is None:
        return {"error": f"No readable image found at {path}", "src": "", "index": index}
    try:
        data = image_path.read_bytes()
    except OSError as exc:
        return {"error": f"Cannot read image {image_path}: {exc}", "src": "", "index": index}
    media_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(data).decode("ascii")
    return {"error": "", "src": f"data:{media_type};base64,{encoded}", "index": index}


def _select_image_path(path: Path, index: int) -> Path | None:
    if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
        return path
    if not path.is_dir():
        return None
    images = sorted(item for item in path.iterdir() if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES)
    if not images:
        return None
    return images[max(0, min(index - 1, len(images) - 1))]


def _viewer_error_html(target: MatrixRow, source: Path, message: str) -> str:
    label = html.escape(target.sample_id or Path(target.source_path).stem or "target")
    detail = html.escape(_friendly_video_error(message))
    source_label = html.escape(str(source))
    return f"""
    <div class="admet-viewer admet-viewer-placeholder">
      <div class="admet-viewer-message">
        <div>
          <div class="admet-viewer-placeholder-title">Preview unavailable</div>
          <div>{label}</div>
          <div>{detail}</div>
          <div class="admet-viewer-placeholder-path">{source_label}</div>
        </div>
      </div>
    </div>
    """


def _preview_frame_index(row: MatrixRow) -> int:
    return max(0, _row_int(row, "start_frame", 0) + _row_int(row, "preview_frame", 0))


def _compact_path(value: str, *, max_parts: int = 4) -> str:
    path = Path(value)
    parts = path.parts
    if len(parts) <= max_parts:
        return value
    return str(Path("...").joinpath(*parts[-max_parts:]))


def _friendly_video_error(message: str) -> str:
    if "cannot open video" in message.lower():
        return "OpenCV cannot open this video. Re-locate the file, verify the codec, or run analyze from the arm64 environment."
    if "not readable" in message.lower():
        return "The video path is missing or not readable. Re-locate the source file."
    return message


def _percent(value: Any, denominator: int) -> float:
    number = _numeric(value) or 0.0
    return round(max(0.0, min(100.0, number / max(denominator, 1) * 100.0)), 2)


def _resolve_project_path(project_path: Path, stored_path: str) -> Path:
    path = Path(stored_path)
    return path if path.is_absolute() else project_path / path


def _row_values(row: dict[str, Any]) -> dict[str, Any]:
    values = row.get("values")
    return values if isinstance(values, dict) else {}


def _row_setting(row: MatrixRow, key: str, default: Any) -> Any:
    value = row.settings.get(key, default)
    return default if value in {None, ""} else value


def _row_int(row: MatrixRow, key: str, default: Any) -> int:
    return int(_numeric(_row_setting(row, key, default)) or 0)


def _row_float(row: MatrixRow, key: str, default: Any) -> float:
    return float(_numeric(_row_setting(row, key, default)) or 0.0)


def _row_bool(row: MatrixRow, key: str, default: Any) -> bool:
    value = _row_setting(row, key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _numeric(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_slider_value(value: Any, step: float) -> str:
    number = _numeric(value) or 0.0
    if step >= 1:
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _is_number(value: Any) -> bool:
    number = _numeric(value)
    return number is not None and math.isfinite(number)


def float_or_zero(value: Any) -> float:
    number = _numeric(value)
    return number if number is not None and math.isfinite(number) else 0.0


def _mean_columns(row: dict[str, Any], prefix: str) -> float:
    values = [
        float_or_zero(value)
        for key, value in row.items()
        if key.startswith(prefix) and value not in {None, ""}
    ]
    return mean(values) if values else 0.0


def _fluidics_duration(run: FluidicsRun) -> float:
    if run.rows:
        return max(row["elapsed_s"] for row in run.rows)
    return float_or_zero(run.metadata.get("duration_s"))


def _escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _event_row(event: Any) -> dict[str, Any]:
    args = getattr(event, "args", {})
    if isinstance(args, list) and len(args) >= 2 and isinstance(args[1], dict):
        return args[1]
    if isinstance(args, dict):
        return args
    return {}


def _existing_dir(path: Path) -> Path:
    try:
        candidate = path.expanduser().resolve()
    except (OSError, RuntimeError):
        candidate = Path.cwd()
    if candidate.is_file():
        candidate = candidate.parent
    while not candidate.is_dir() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate if candidate.is_dir() else Path.cwd()


def _browser_entries(path: Path, mode: str) -> list[Path]:
    try:
        entries = list(path.iterdir())
    except OSError:
        return []
    dirs = [entry for entry in entries if entry.is_dir()]
    if mode == "project":
        files = [entry for entry in entries if entry.is_file() and entry.name == "manifest.json"]
    else:
        allowed = VIDEO_SUFFIXES | IMAGE_SUFFIXES
        files = [entry for entry in entries if entry.is_file() and entry.suffix.lower() in allowed]
    return sorted(dirs, key=lambda item: item.name.lower()) + sorted(files, key=lambda item: item.name.lower())


def _dot_class(status: StageStatus, selected: bool) -> str:
    if selected:
        return "toc-dot toc-dot-active"
    if status is StageStatus.COMPLETE:
        return "toc-dot toc-dot-done"
    if status is StageStatus.SKIPPED:
        return "toc-dot toc-dot-skipped"
    return "toc-dot"


_uid_counter = itertools.count(1)


def _uid() -> str:
    # A process-unique, monotonic id. Must never collide: rows are keyed by uid in
    # the matrix table (row_key) and looked up by uid in _handle_matrix_change, so a
    # shared uid makes every checkbox toggle resolve to the first matching row.
    return f"target_{next(_uid_counter)}"


def _style() -> str:
    return "<style>:root{" + design.css_variables() + "}</style>" + """
    <style>
    body { background: var(--bg-app); }
    .shell { min-height: 100vh; color: var(--text); font-size: 13px; padding: 18px 20px 20px; gap: 8px; }
    .left-rail { width: 246px; align-self: flex-start; gap: 8px; }
    .topbar, .workflow-toc {
      background: var(--bg-control);
      border: 1px solid var(--border);
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
      color: var(--text);
      padding: 0;
    }
    .admet-panel-body {
      padding: 0;
    }
    .workflow-toc { padding: 10px 12px; gap: 3px; }
    .notification-card {
      background: var(--bg-control);
      border: 1px solid var(--accent);
      border-left: 5px solid var(--accent);
      border-radius: 2px;
      padding: 12px 14px;
      gap: 4px;
    }
    .notification-card-warning { border-color: var(--warning); border-left-color: var(--warning); }
    .notification-card-success { border-color: var(--success-hover); border-left-color: var(--success-hover); }
    .notification-card-danger { border-color: var(--danger-hover); border-left-color: var(--danger-hover); }
    .notification-title { color: var(--text-muted); font-size: 11px; font-weight: 650; text-transform: uppercase; }
    .notification-text { color: var(--text); font-size: 13px; font-weight: 600; line-height: 1.35; white-space: normal; overflow-wrap: anywhere; }
    .control-box { background: transparent; border: 0; border-radius: 0; gap: 6px; }
    .control-box-title { font-size: 16px; line-height: 22px; font-weight: 650; color: var(--text); }
    .process-bar {
      background: var(--bg-raised);
      border: 1px solid var(--border);
      border-radius: 8px 8px 0 0;
      min-height: 24px;
    }
    .process-bar .q-linear-progress {
      height: 18px;
      border-radius: 8px 8px 0 0;
      overflow: hidden;
    }
    .admet-process {
      background: var(--bg-raised);
      border: 1px solid var(--border);
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
    .admet-transport-btn {
      background: var(--bg-control) !important;
      color: var(--text) !important;
      border: 0 !important;
      border-radius: 0 !important;
      min-height: 22px !important;
      height: 22px !important;
      padding: 0 !important;
      font-weight: 500;
    }
    .admet-transport-btn:hover { background: var(--bg-raised) !important; }
    .admet-transport-btn-active {
      color: var(--accent) !important;
      background: var(--bg-control-pressed) !important;
      font-weight: 650;
    }
    .admet-transport-btn-warning {
      color: var(--bg-control) !important;
      background: var(--warning) !important;
      font-weight: 700;
    }
    .admet-transport-separator {
      width: 1px;
      align-self: stretch;
      background: var(--border);
    }
    .transport-buttons { background: transparent; border: 0; border-radius: 0; }
    .transport-btn {
      background: var(--bg-control) !important;
      color: var(--text) !important;
      border: 0 !important;
      border-radius: 0 !important;
      min-height: 22px !important;
      height: 22px !important;
      padding: 0 !important;
      font-weight: 500;
    }
    .transport-btn:hover { background: var(--bg-raised) !important; }
    .transport-btn-active { color: var(--accent) !important; background: var(--bg-control-pressed) !important; font-weight: 650; }
    .transport-btn-warning { color: var(--bg-control) !important; background: var(--warning) !important; font-weight: 700; }
    .transport-separator { width: 1px; align-self: stretch; background: var(--border); }
    .panel { background: var(--bg-control); border: 1px solid var(--border); border-radius: 8px; }
    .toc-title { color: var(--text-muted); font-size: 11px; font-weight: 650; text-transform: uppercase; }
    .toc-row { min-height: 24px; border-radius: 6px; padding: 2px 4px; cursor: pointer; }
    .toc-row:hover { background: var(--bg-raised); }
    .toc-dot { width: 8px; height: 8px; border-radius: 999px; background: var(--text-disabled); border: 1px solid var(--border); }
    .toc-dot-active { background: var(--accent); border-color: var(--accent); }
    .toc-dot-done { background: var(--success-hover); border-color: var(--success-hover); }
    .toc-dot-skipped { background: var(--text-disabled); border-color: var(--text-disabled); }
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
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text);
      background: var(--bg-control);
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
      color: var(--text);
      font-weight: 600;
    }
    .browser-card {
      width: min(760px, 92vw);
      max-height: 82vh;
      border-radius: 8px;
      box-shadow: none;
      border: 1px solid var(--border);
      gap: 8px;
    }
    .browser-path {
      color: var(--text-muted);
      font-family: Menlo, Consolas, monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .browser-list {
      max-height: 56vh;
      overflow-y: auto;
      border-top: 1px solid var(--border);
      border-bottom: 1px solid var(--border);
      padding: 6px 0;
    }
    .browser-row {
      justify-content: flex-start !important;
      width: 100%;
      border-radius: 4px !important;
      color: var(--text) !important;
      font-weight: 500 !important;
    }
    .browser-select {
      min-width: 34px !important;
      width: 34px;
      flex: 0 0 auto;
      padding: 0 !important;
    }
    .browser-name {
      justify-content: flex-start !important;
      text-align: left;
      overflow-wrap: anywhere;
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
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--bg-control);
      min-height: 260px;
      padding: 4px;
    }
    .admet-viewer {
      position: relative;
      width: 100%;
      max-width: 100%;
      height: clamp(220px, 42vh, 460px);
      overflow: hidden;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--text);
    }
    .opencv-preview {
      display: block;
      width: 100%;
    }
    .media-editor-controls { min-width: 0; }
    .media-editor-controls .slider-field { width: 100%; }
    .media-editor-controls .q-slider { width: 100% !important; }
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
      background: var(--text);
    }
    .admet-viewer-message {
      position: absolute;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      text-align: center;
      padding: 14px;
      color: var(--text-muted);
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
      color: var(--text);
      margin-bottom: 6px;
    }
    .admet-viewer-placeholder-path {
      color: var(--text-muted);
      font-family: Menlo, Consolas, monospace;
      font-size: 11px;
      margin-top: 6px;
    }
    .admet-roi {
      position: absolute;
      border: 2px solid var(--roi);
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
      grid-template-columns: 3fr 1fr;
      gap: 12px;
      align-items: start;
    }
    .media-editor-video { min-width: 0; }
    @media (max-width: 980px) {
      .media-editor-grid { grid-template-columns: 1fr; }
    }
    .editor-note {
      border: 1px solid var(--border);
      border-left: 4px solid var(--accent);
      border-radius: 4px;
      padding: 8px 10px;
      color: var(--text-muted);
      background: var(--bg-app);
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
      border-top: 1px solid var(--border);
      padding-top: 8px;
    }
    .compact-number .q-field__control,
    .compact-checkbox .q-checkbox__inner {
      min-height: 30px;
    }
    .slider-field {
      width: 100%;
    }
    .slider-field .q-slider {
      min-height: 24px;
      width: 100%;
      padding: 0 6px;
    }
    .slider-label-row {
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .slider-value {
      color: var(--text);
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
      background: var(--bg-control) !important;
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
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px;
      background: var(--bg-app);
      gap: 6px;
    }
    .counter-value {
      color: var(--text);
      font-size: 24px;
      line-height: 28px;
      font-weight: 650;
    }
    .log-text {
      background: var(--bg-raised);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 10px;
      color: var(--text-muted);
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
    .muted { color: var(--text-muted); }
    .section-title { color: var(--text-muted); font-size: 12px; text-transform: uppercase; font-weight: 700; }
    .q-field__control { min-height: 34px; border-radius: 8px; }
    .q-btn { min-height: 30px; border-radius: 8px; text-transform: none; }
    .q-table__card { box-shadow: none; border: 1px solid var(--border); }
    .q-table th, .q-table td { padding: 4px 8px; }
    </style>
    """
