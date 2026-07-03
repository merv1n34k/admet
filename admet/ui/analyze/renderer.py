from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from admet.analyze import AnalyzeBatchReport, AnalyzeBatchRunner, AnalyzeTarget, infer_engine
from admet.core.engine import EngineRegistry
from admet.core.project import ProjectStore
from admet.core.session import session_path
from admet.core.workflow import StageStatus, Workflow, WorkflowState


WORKFLOW_STAGES = (
    ("import", "1. Import & Batch"),
    ("video", "2. Video Analysis"),
    ("imaging", "3. Imaging Analysis"),
    ("view", "4. View Results"),
    ("export", "5. Export"),
)


@dataclass
class MatrixRow:
    uid: str
    project_path: str
    source_path: str
    engine: str
    sample_id: str
    cache_policy: str = "use"
    active: bool = True


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
        self.project_path = str(Path.cwd() / f"admet_{time.strftime('%Y%m%d_%H%M%S')}.admetp")
        self.source_path = ""
        self.selected_uid = ""
        self.matrix: list[MatrixRow] = []
        self.settings: dict[str, Any] = {
            "opencv_microns_per_pixel": 1.0,
            "opencv_fps": 0.0,
            "opencv_max_frames": 0,
            "cellpose_config_path": "",
            "cellpose_px_to_um": 1.14,
            "cellpose_frame_limit": 0,
            "cellpose_use_cache": True,
            "cellpose_detect_inclusions": True,
            "cache_root": str(Path.home() / ".admet-cache" / "admet2"),
        }
        self.stage_progress: dict[str, int] = {
            "import": 0,
            "video": 0,
            "imaging": 0,
            "view": 0,
            "export": 0,
        }
        self.last_report: AnalyzeBatchReport | None = None
        self.notice = "Create or select a project, then add files to the batch matrix."
        self.notice_kind = "primary"
        self.action_log: list[str] = ["Analyze UI ready."]
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
        with ui.column().classes("shell w-full"):
            self._render_topbar()
            with ui.row().classes("w-full gap-4 flex-nowrap items-start"):
                with ui.column().classes("left-rail shrink-0"):
                    self._render_toc()
                    self._render_instruction_card()
                    self._render_notice_card()
                with ui.column().classes("grow min-w-0 gap-3"):
                    self._render_action_box()
                    self._render_action_panel()
                    self._render_main_window()
                    self._render_results()
                    self._render_log()

    def _render_topbar(self) -> None:
        from nicegui import ui

        with ui.row().classes("topbar w-full items-center justify-between px-4 py-3"):
            with ui.row().classes("items-baseline gap-3"):
                ui.label("admet analyze").classes("text-xl font-semibold")
                ui.label("Project analysis matrix").classes("muted text-sm")
            with ui.row().classes("items-center gap-2"):
                ui.input(
                    "Project",
                    value=self.project_path,
                ).bind_value(self, "project_path").classes("project-input")
                ui.button("New Project", on_click=self._new_project).props("dense no-caps outline")
                ui.button("Load Project", on_click=self._load_project).props("dense no-caps outline")

    def _render_toc(self) -> None:
        from nicegui import ui

        with ui.column().classes("workflow-toc w-full"):
            ui.label("Workflow").classes("toc-title")
            for index, (_stage_id, label) in enumerate(WORKFLOW_STAGES):
                selected = index == self.state.index
                status = self.state.statuses.get(_stage_id, StageStatus.PENDING)
                dot = _dot_class(status, selected)
                with ui.row().classes("toc-row w-full items-center gap-2").on(
                    "click",
                    lambda _event, idx=index: self._activate(idx),
                ):
                    ui.element("span").classes(dot)
                    ui.label(label).classes("text-sm" + (" font-semibold" if selected else ""))

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

    def _render_action_box(self) -> None:
        from nicegui import ui

        stage_id = self._stage_id()
        progress = self.stage_progress.get(stage_id, 0)
        with ui.column().classes("control-box w-full"):
            ui.label("Action Box").classes("control-box-title")
            with ui.column().classes("process-bar w-full gap-0 overflow-hidden"):
                ui.linear_progress(value=progress / 100.0).classes("w-full")
                with ui.row().classes("transport-buttons w-full items-center gap-0"):
                    for index, spec in enumerate(self._action_specs(stage_id)):
                        if index:
                            ui.element("span").classes("transport-separator")
                        button = ui.button(spec["label"], on_click=spec["handler"]).props(
                            "unelevated dense no-caps"
                        )
                        classes = "transport-btn grow"
                        if spec.get("active"):
                            classes += " transport-btn-active"
                        if spec.get("warning"):
                            classes += " transport-btn-warning"
                        button.classes(classes)

    def _render_action_panel(self) -> None:
        from nicegui import ui

        with ui.column().classes("control-box w-full"):
            ui.label("Action Panel").classes("control-box-title")
            with ui.column().classes("panel w-full gap-2 p-2"):
                if self._stage_id() == "import":
                    self._render_import_controls()
                elif self._stage_id() == "video":
                    self._render_opencv_settings()
                elif self._stage_id() == "imaging":
                    self._render_cellpose_settings()
                elif self._stage_id() == "view":
                    self._render_view_settings()
                else:
                    ui.label("Export will use stored raw analysis data.").classes("muted text-xs")

    def _render_import_controls(self) -> None:
        from nicegui import ui

        with ui.grid(columns="repeat(4, minmax(0, 1fr))").classes("w-full gap-2"):
            ui.input("Source", value=self.source_path).bind_value(self, "source_path").classes("col-span-2")
            ui.select(
                {"": "Auto", "opencv": "OpenCV", "cellpose": "Cellpose"},
                label="Engine",
                value=self._selected_engine_value(),
                on_change=lambda event: self._set_selected_field("engine", event.value),
            )
            ui.select(
                {"use": "Use cache", "discard": "Discard cache", "skip": "Skip"},
                label="Cache",
                value=self._selected_cache_policy(),
                on_change=lambda event: self._set_selected_field("cache_policy", event.value),
            )
        with ui.grid(columns="repeat(4, minmax(0, 1fr))").classes("w-full gap-2"):
            selected = self._selected_row()
            ui.input(
                "Project",
                value=selected.project_path if selected else self.project_path,
                on_change=lambda event: self._set_selected_or_project_path(event.value),
            ).classes("col-span-2")
            ui.input(
                "Sample ID",
                value=selected.sample_id if selected else "",
                on_change=lambda event: self._set_selected_field("sample_id", event.value),
            )
            ui.checkbox(
                "Active",
                value=selected.active if selected else True,
                on_change=lambda event: self._set_selected_field("active", bool(event.value)),
            )
        with ui.row().classes("w-full gap-2"):
            ui.button("Add Target", on_click=self._add_target).props("dense no-caps outline").classes("grow")
            ui.button("Remove Selected", on_click=self._remove_selected).props("dense no-caps outline color=red").classes(
                "grow"
            )

    def _render_opencv_settings(self) -> None:
        from nicegui import ui

        with ui.grid(columns="repeat(4, minmax(0, 1fr))").classes("w-full gap-2"):
            ui.number(
                "Microns / px",
                value=self.settings["opencv_microns_per_pixel"],
                min=0,
                step=0.01,
                on_change=lambda event: self._set_setting("opencv_microns_per_pixel", float(event.value or 0)),
            )
            ui.number(
                "FPS",
                value=self.settings["opencv_fps"],
                min=0,
                step=1,
                on_change=lambda event: self._set_setting("opencv_fps", float(event.value or 0)),
            )
            ui.number(
                "Max frames",
                value=self.settings["opencv_max_frames"],
                min=0,
                step=1,
                on_change=lambda event: self._set_setting("opencv_max_frames", int(float(event.value or 0))),
            )
            ui.label(f"{len(self._targets('opencv'))} active target(s)").classes("muted self-center")

    def _render_cellpose_settings(self) -> None:
        from nicegui import ui

        with ui.grid(columns="repeat(4, minmax(0, 1fr))").classes("w-full gap-2"):
            ui.input(
                "Config path",
                value=self.settings["cellpose_config_path"],
                on_change=lambda event: self._set_setting("cellpose_config_path", event.value or ""),
            )
            ui.number(
                "px to um",
                value=self.settings["cellpose_px_to_um"],
                min=0,
                step=0.01,
                on_change=lambda event: self._set_setting("cellpose_px_to_um", float(event.value or 0)),
            )
            ui.number(
                "Frame limit",
                value=self.settings["cellpose_frame_limit"],
                min=0,
                step=1,
                on_change=lambda event: self._set_setting("cellpose_frame_limit", int(float(event.value or 0))),
            )
            with ui.column().classes("gap-0"):
                ui.checkbox(
                    "Use cache",
                    value=bool(self.settings["cellpose_use_cache"]),
                    on_change=lambda event: self._set_setting("cellpose_use_cache", bool(event.value)),
                )
                ui.checkbox(
                    "Detect inclusions",
                    value=bool(self.settings["cellpose_detect_inclusions"]),
                    on_change=lambda event: self._set_setting("cellpose_detect_inclusions", bool(event.value)),
                )

    def _render_view_settings(self) -> None:
        from nicegui import ui

        ui.input(
            "Cache root",
            value=self.settings["cache_root"],
            on_change=lambda event: self._set_setting("cache_root", event.value or ""),
        ).classes("w-full")

    def _render_main_window(self) -> None:
        from nicegui import ui

        with ui.column().classes("control-box w-full"):
            ui.label("Main Window").classes("control-box-title")
            if self._stage_id() == "import":
                self._render_matrix()
            elif self._stage_id() == "video":
                self._render_engine_matrix("opencv")
            elif self._stage_id() == "imaging":
                self._render_engine_matrix("cellpose")
            else:
                self._render_analysis_runs()

    def _render_matrix(self) -> None:
        from nicegui import ui

        with ui.column().classes("panel w-full gap-2 p-3"):
            ui.label("Batch matrix").classes("section-title")
            table = ui.table(
                columns=_matrix_columns(),
                rows=[self._matrix_row(row) for row in self.matrix],
                row_key="uid",
            ).classes("slim-table w-full").props("dense flat hide-bottom")
            table.on("rowClick", self._select_row_event)
            if not self.matrix:
                ui.label("No targets. Add a source path in the Action Panel.").classes("muted text-xs")

    def _render_engine_matrix(self, engine: str) -> None:
        from nicegui import ui

        rows = [row for row in self.matrix if row.active and row.engine == engine]
        with ui.column().classes("panel w-full gap-2 p-3"):
            ui.label("Engine target matrix").classes("section-title")
            table = ui.table(
                columns=_matrix_columns(),
                rows=[self._matrix_row(row) for row in rows],
                row_key="uid",
            ).classes("slim-table w-full").props("dense flat hide-bottom")
            table.on("rowClick", self._select_row_event)
            if not rows:
                ui.label(f"No active {engine} targets.").classes("muted text-xs")
        with ui.column().classes("panel w-full gap-2 p-3"):
            ui.label("Run plan").classes("section-title")
            ui.table(
                columns=[
                    {"name": "step", "label": "Step", "field": "step", "align": "left"},
                    {"name": "source", "label": "Source", "field": "source", "align": "left"},
                    {"name": "output", "label": "Output", "field": "output", "align": "left"},
                ],
                rows=_run_plan_rows(engine),
            ).classes("w-full").props("dense flat hide-bottom")

    def _render_analysis_runs(self) -> None:
        from nicegui import ui

        with ui.column().classes("panel w-full gap-2 p-3"):
            ui.label("Stored analysis runs").classes("section-title")
            rows = self._analysis_run_rows()
            ui.table(
                columns=[
                    {"name": "project", "label": "Project", "field": "project", "align": "left"},
                    {"name": "run_id", "label": "Run", "field": "run_id", "align": "left"},
                    {"name": "jobs", "label": "Jobs", "field": "jobs", "align": "right"},
                    {"name": "raw", "label": "Raw", "field": "raw", "align": "left"},
                ],
                rows=rows,
            ).classes("w-full").props("dense flat hide-bottom")
            if not rows:
                ui.label("No stored analysis runs found for the matrix projects.").classes("muted text-xs")

    def _render_results(self) -> None:
        from nicegui import ui

        with ui.column().classes("control-box w-full"):
            ui.label("Results").classes("control-box-title")
            with ui.column().classes("panel w-full gap-2 p-3"):
                ui.label("Run summary").classes("section-title")
                rows = self._result_rows()
                ui.table(
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
                if not rows:
                    ui.label("No results yet. Run OpenCV, Cellpose, or Run All.").classes("muted text-xs")

    def _render_log(self) -> None:
        from nicegui import ui

        with ui.column().classes("control-box w-full"):
            ui.label("Action Log").classes("control-box-title")
            ui.html("<br>".join(self.action_log[-80:])).classes("log-text w-full")

    def _action_specs(self, stage_id: str) -> list[dict[str, Any]]:
        if stage_id == "import":
            return [
                {"label": "Create Project", "handler": self._new_project},
                {"label": "Load Project", "handler": self._load_project},
                {"label": "Add Target", "handler": self._add_target, "active": bool(self.source_path)},
            ]
        if stage_id == "video":
            return [{"label": "Run OpenCV", "handler": lambda: self._run_engine("opencv"), "active": True}]
        if stage_id == "imaging":
            return [{"label": "Run Cellpose", "handler": lambda: self._run_engine("cellpose"), "active": True}]
        if stage_id == "view":
            return [
                {"label": "Refresh View", "handler": self._refresh_view, "active": True},
                {"label": "Run All", "handler": lambda: self._run_engine(None)},
            ]
        return [{"label": "Export Later", "handler": lambda: self._notify("Export is not wired yet.", "warning")}]

    def _new_project(self) -> None:
        path = self._project_path()
        try:
            store = ProjectStore.create(path, path.stem, "combined")
        except Exception as exc:
            self._notify(f"project create failed: {exc}", "danger")
            self._refresh()
            return
        self.project_path = str(store.path)
        self.stage_progress["import"] = max(self.stage_progress["import"], 30)
        self._mark_stage("import", StageStatus.ACTIVE)
        self._notify(f"project ready: {store.path.name}", "success")
        self._log(f"project: created {store.path}")
        self._refresh()

    def _load_project(self) -> None:
        path = self._project_path()
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
        self._refresh()

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

    def _add_target(self) -> None:
        if not self.source_path:
            self._notify("Set a source path first.", "warning")
            self._refresh()
            return
        try:
            engine = infer_engine(self.source_path)
        except Exception:
            engine = "opencv"
        row = MatrixRow(
            uid=_uid(),
            project_path=str(session_path(self.project_path)),
            source_path=self.source_path,
            engine=engine,
            sample_id=Path(self.source_path).stem or f"sample_{len(self.matrix) + 1}",
        )
        self.matrix.append(row)
        self.selected_uid = row.uid
        self.stage_progress["import"] = 100
        self._mark_stage("import", StageStatus.COMPLETE)
        self._notify("target added.", "success")
        self._log(f"matrix: added {row.source_path} -> {row.engine}")
        self._refresh()

    def _remove_selected(self) -> None:
        if not self.selected_uid:
            return
        self.matrix = [row for row in self.matrix if row.uid != self.selected_uid]
        self.selected_uid = self.matrix[0].uid if self.matrix else ""
        self.stage_progress["import"] = 100 if self.matrix else 0
        self._refresh()

    def _run_engine(self, engine: str | None) -> None:
        targets = self._targets(engine)
        if not targets:
            label = engine or "analyze"
            self._notify(f"No active {label} targets.", "warning")
            self._refresh()
            return
        try:
            report = AnalyzeBatchRunner(
                self.registry,
                cache_root=self.settings["cache_root"],
            ).run(tuple(self._target_to_run(row) for row in targets))
        except Exception as exc:
            self._notify(f"analysis failed: {exc}", "danger")
            self._log(f"analysis: {type(exc).__name__}: {exc}")
            self._refresh()
            return

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

    def _target_to_run(self, row: MatrixRow) -> AnalyzeTarget:
        settings = self._engine_settings(row.engine)
        return AnalyzeTarget(
            project_path=Path(row.project_path),
            source_path=Path(row.source_path),
            engine=row.engine,
            sample_id=row.sample_id,
            settings=settings,
            cache_policy=row.cache_policy,
        )

    def _engine_settings(self, engine: str) -> dict[str, Any]:
        if engine == "cellpose":
            frame_limit = int(self.settings["cellpose_frame_limit"] or 0)
            return {
                "config_path": str(self.settings["cellpose_config_path"] or ""),
                "px_to_um": float(self.settings["cellpose_px_to_um"] or 0),
                "frame_limit": frame_limit or None,
                "use_cache": bool(self.settings["cellpose_use_cache"]),
                "detect_inclusions": bool(self.settings["cellpose_detect_inclusions"]),
            }
        max_frames = int(self.settings["opencv_max_frames"] or 0)
        return {
            "microns_per_pixel": float(self.settings["opencv_microns_per_pixel"] or 0),
            "fps": float(self.settings["opencv_fps"] or 0),
            "max_frames": max_frames or None,
        }

    def _refresh_view(self) -> None:
        self.stage_progress["view"] = 100 if self._analysis_run_rows() else 0
        self._notify("view refreshed.", "success")
        self._refresh()

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
            rows.append(
                {
                    "sample": job.sample_id,
                    "engine": job.engine,
                    "status": job.status,
                    "rows": metadata.get("row_count", ""),
                    "frames": metadata.get("frames_processed", ""),
                    "cache": Path(str(metadata.get("cache_dir") or "")).name,
                }
            )
        return rows

    def _matrix_row(self, row: MatrixRow) -> dict[str, Any]:
        return {
            "uid": row.uid,
            "project": Path(row.project_path).name,
            "source": Path(row.source_path).name or row.source_path,
            "engine": row.engine,
            "sample_id": row.sample_id,
            "cache": row.cache_policy,
            "active": "yes" if row.active else "no",
        }

    def _targets(self, engine: str | None = None) -> list[MatrixRow]:
        return [
            row
            for row in self.matrix
            if row.active and (engine is None or row.engine == engine)
        ]

    def _selected_row(self) -> MatrixRow | None:
        return next((row for row in self.matrix if row.uid == self.selected_uid), None)

    def _selected_engine_value(self) -> str:
        row = self._selected_row()
        return row.engine if row is not None else ""

    def _selected_cache_policy(self) -> str:
        row = self._selected_row()
        return row.cache_policy if row is not None else "use"

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

    def _set_selected_field(self, field: str, value: Any) -> None:
        row = self._selected_row()
        if row is None:
            return
        setattr(row, field, value)
        self._refresh()

    def _set_selected_or_project_path(self, value: str) -> None:
        row = self._selected_row()
        if row is not None:
            row.project_path = str(session_path(value))
        else:
            self.project_path = str(session_path(value))
        self._refresh()

    def _set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value

    def _activate(self, index: int) -> None:
        index = max(0, min(index, len(WORKFLOW_STAGES) - 1))
        statuses = dict(self.state.statuses)
        current_id = self._stage_id()
        if statuses.get(current_id) is StageStatus.ACTIVE:
            statuses[current_id] = StageStatus.PENDING
        stage_id = WORKFLOW_STAGES[index][0]
        if statuses.get(stage_id) is StageStatus.PENDING:
            statuses[stage_id] = StageStatus.ACTIVE
        self.state = replace(self.state, index=index, statuses=statuses)
        self._refresh()

    def _mark_stage(self, stage_id: str, status: StageStatus) -> None:
        statuses = dict(self.state.statuses)
        statuses[stage_id] = status
        self.state = replace(self.state, statuses=statuses)

    def _stage_id(self) -> str:
        index = max(0, min(self.state.index, len(WORKFLOW_STAGES) - 1))
        return WORKFLOW_STAGES[index][0]

    def _project_path(self) -> Path:
        value = str(self.project_path or "").strip()
        if not value:
            value = str(Path.cwd() / f"admet_{time.strftime('%Y%m%d_%H%M%S')}.admetp")
            self.project_path = value
        return session_path(value)

    def _instruction(self) -> str:
        stage_id = self._stage_id()
        if stage_id == "import":
            return "Create or load a project, add source paths, then confirm engine and cache policy per row."
        if stage_id == "video":
            return "Configure OpenCV values and run the active video rows."
        if stage_id == "imaging":
            return "Configure Cellpose values and run the active imaging rows."
        if stage_id == "view":
            return "Review stored raw runs and current execution summaries."
        return "Export will derive tables and figures from stored raw runs."

    def _notify(self, message: str, kind: str) -> None:
        self.notice = message
        self.notice_kind = kind

    def _log(self, message: str) -> None:
        self.action_log.append(f"{time.strftime('%H:%M:%S')} {message}")

    def _refresh(self) -> None:
        if self._screen is not None:
            self._screen.refresh()


def _matrix_columns() -> list[dict[str, Any]]:
    return [
        {"name": "project", "label": "Project", "field": "project", "align": "left"},
        {"name": "source", "label": "Source", "field": "source", "align": "left"},
        {"name": "engine", "label": "Engine", "field": "engine", "align": "left"},
        {"name": "sample_id", "label": "Sample ID", "field": "sample_id", "align": "left"},
        {"name": "cache", "label": "Cache", "field": "cache", "align": "left"},
        {"name": "active", "label": "Active", "field": "active", "align": "left"},
    ]


def _run_plan_rows(engine: str) -> list[dict[str, str]]:
    if engine == "cellpose":
        return [
            {"step": "Load images", "source": "matrix image directory", "output": "frame list"},
            {"step": "Segment droplets", "source": "Cellpose masks", "output": "droplet raw rows"},
            {"step": "Detect inclusions", "source": "droplet crops", "output": "inclusion counts"},
            {"step": "Store run", "source": "raw sink", "output": "analysis/runs/<run>/raw.jsonl"},
        ]
    return [
        {"step": "Load video", "source": "matrix video file", "output": "frames"},
        {"step": "Detect droplets", "source": "OpenCV frames", "output": "detection raw rows"},
        {"step": "Track droplets", "source": "detections", "output": "track raw rows"},
        {"step": "Store run", "source": "raw sink", "output": "analysis/runs/<run>/raw.jsonl"},
    ]


def _event_row(event: Any) -> dict[str, Any]:
    args = getattr(event, "args", {})
    if isinstance(args, list) and len(args) >= 2 and isinstance(args[1], dict):
        return args[1]
    if isinstance(args, dict):
        return args
    return {}


def _dot_class(status: StageStatus, selected: bool) -> str:
    if selected:
        return "toc-dot toc-dot-active"
    if status is StageStatus.COMPLETE:
        return "toc-dot toc-dot-done"
    if status is StageStatus.SKIPPED:
        return "toc-dot toc-dot-skipped"
    return "toc-dot"


def _uid() -> str:
    return f"target_{int(time.time() * 1000)}"


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
    .process-bar { background: #f0f5f8; border: 1px solid #d7e2ea; border-radius: 8px; min-height: 24px; }
    .process-bar .q-linear-progress { height: 18px; border-radius: 4px; }
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
    .project-input { min-width: 360px; }
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
    .muted { color: #52677a; }
    .section-title { color: #52677a; font-size: 12px; text-transform: uppercase; font-weight: 700; }
    .q-field__control { min-height: 34px; border-radius: 8px; }
    .q-btn { min-height: 30px; border-radius: 8px; text-transform: none; }
    .q-table__card { box-shadow: none; border: 1px solid #d7e2ea; }
    .q-table th, .q-table td { padding: 4px 8px; }
    </style>
    """
