from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from admet.core.discovery import discover_projects, projects_root
from admet.core.engine import EngineRegistry, Param
from admet.core.project import ProjectStore
from admet.core.session import session_path
from admet.ui.analyze_helpers import (
    FIELD_SETTING_KEYS,
    project_ref_label,
    uid,
    video_metadata,
    video_placeholder,
)
from admet.ui.presenter import FieldVM, SurfaceVM
from admet.ui.project import create_project, load_project
from admet.ui.render import MatrixRow, row_bool, row_float, row_int, row_setting
from admet.workflows import Stage, StageStatus, Workflow, WorkflowState
from admet.workflows.analyze import AnalyzeBatchReport, AnalyzeBatchRunner, AnalyzeTarget, infer_engine


class NiceGuiAnalyzeRuntime:
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
        self.values: dict[str, Any] = {}
        self.settings: dict[str, Any] = {
            "opencv_microns_per_pixel": 1.0,
            "opencv_fps": 0.0,
            "cellpose_config_path": "",
            "cellpose_px_to_um": 1.14,
            "cellpose_frame_limit": 0,
            "cellpose_use_cache": True,
            "cellpose_detect_inclusions": True,
            "cache_root": str(Path.home() / ".admet-cache" / "admet2"),
        }
        self.last_report: AnalyzeBatchReport | None = None
        self.notice = "Create or select a project, then add files to the matrix."
        self.notice_kind = "primary"
        self.action_log: list[str] = ["Analyze UI ready."]
        self.refresh: Callable[[], None] = lambda: None

    def value_for(self, field: Param) -> Any:
        if field.name == "project_path":
            return self.project_path
        if field.name == "source_path":
            return self.source_path
        return self.values.get(field.name, field.default)

    def button_enabled(self, command: str) -> bool:
        if command == "analyze":
            return bool(self.targets(self.engine_for_current_stage()))
        return True

    def button_active(self, command: str) -> bool:
        return False

    def instructions_for(self, stage: Stage) -> tuple[str, ...]:
        if stage.instructions:
            return stage.instructions
        if stage.description:
            return (stage.description,)
        return ()

    def surface_rows(self, surface: SurfaceVM) -> list[dict[str, Any]]:
        if surface.kind != "matrix":
            return []
        engine = surface.options.get("engine")
        rows = self.targets(str(engine)) if engine else list(self.matrix)
        return [self.matrix_row(row) for row in rows]

    def surface_html(self, surface: SurfaceVM) -> str:
        if surface.kind != "video_preview":
            return ""
        target = self.selected_row_for_engine(str(surface.options.get("engine") or ""))
        if target is None:
            return ""
        return video_placeholder(target)

    def surface_charts(self, surface: SurfaceVM) -> list[dict[str, Any]]:
        if surface.kind != "charts":
            return []
        rows = self.result_rows()
        if not rows:
            return []
        return [
            {
                "xAxis": {"type": "category", "data": [row["sample"] for row in rows]},
                "yAxis": {"type": "value"},
                "series": [{"type": "bar", "data": [row.get("rows") or 0 for row in rows]}],
            }
        ]

    def handle_command(self, command: str) -> None:
        if command == "back":
            self.state = self.workflow.rewind(self.state)
        elif command == "skip":
            self.state = self.workflow.skip_current(self.state)
        elif command in {"complete", "advance"}:
            self.complete_current()
        elif command == "analyze":
            self.run_engine(self.engine_for_current_stage())
        else:
            self.notify(f"{command} is not wired in the analyze adapter.", "warning")
            self.log(f"command: ignored {command}")

    def complete_current(self) -> None:
        self.apply_stage_fields()
        if self.source_path:
            self.upsert_source(Path(self.source_path), row_uid=self.selected_uid or None)
        self.state = self.workflow.complete_current(self.state, confirmed=True)

    def apply_stage_fields(self) -> None:
        self.project_path = str(session_path(self.values.get("project_path") or self.project_path))
        self.source_path = str(self.values.get("source_path") or self.source_path)

    def set_field(self, field: FieldVM, value: Any) -> None:
        self.values[field.name] = value
        target = FIELD_SETTING_KEYS.get(field.name)
        if field.name == "project_path":
            self.project_path = str(value or "")
        elif field.name == "source_path":
            self.source_path = str(value or "")
        elif target is not None:
            self.settings[target] = value

    def new_project(self) -> None:
        path = self.project_path_value()
        try:
            project = create_project(path)
        except Exception as exc:
            self.notify(f"project create failed: {exc}", "danger")
            self.refresh()
            return
        store = ProjectStore(project.path, project.session)
        self.project_path = str(store.path)
        self.values["project_path"] = self.project_path
        self.mark_stage(StageStatus.ACTIVE)
        self.notify(f"project ready: {store.path.name}", "success")
        self.log(f"project: created {store.path}")
        self.project_refs = discover_projects(self.discovery_root)
        self.refresh()

    def load_project(self, path: Path | None = None) -> None:
        path = session_path(path) if path is not None else self.project_path_value()
        if path.name == "manifest.json":
            path = path.parent
        if not (path / "manifest.json").is_file():
            self.notify("Project manifest not found.", "warning")
            self.refresh()
            return
        try:
            project = load_project(path)
        except Exception as exc:
            self.notify(f"project load failed: {exc}", "danger")
            self.refresh()
            return
        store = ProjectStore(project.path, project.session)
        self.project_path = str(store.path)
        self.values["project_path"] = self.project_path
        self.load_project_files(store)
        self.mark_stage(StageStatus.COMPLETE if self.matrix else StageStatus.ACTIVE)
        self.notify(f"project loaded: {store.path.name}", "success")
        self.log(f"project: loaded {store.path}")
        self.project_refs = discover_projects(self.discovery_root)
        self.refresh()

    def load_project_files(self, store: ProjectStore) -> None:
        existing = {row.source_path for row in self.matrix}
        for file in store.session.files:
            if file.role not in {"control_video", "analysis_video", "analysis_image_dir"}:
                continue
            engine = str(
                file.metadata.get("engine")
                or ("cellpose" if file.role == "analysis_image_dir" else "opencv")
            )
            source_path = str(store.path / file.path) if not Path(file.path).is_absolute() else file.path
            if source_path in existing:
                continue
            self.matrix.append(
                MatrixRow(
                    uid=uid(),
                    project_path=str(store.path),
                    source_path=source_path,
                    engine=engine,
                    sample_id=str(file.metadata.get("sample_id") or Path(file.path).stem),
                )
            )

    def upsert_source(self, source: Path, *, row_uid: str | None) -> None:
        source = source.expanduser().resolve()
        engine = infer_engine(source)
        row = next((item for item in self.matrix if item.uid == row_uid), None)
        existing = next((item for item in self.matrix if Path(item.source_path) == source), None)
        if row is None and existing is not None:
            row = existing
        if row is None:
            row = MatrixRow(
                uid=uid(),
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
        self.notify(f"source ready: {source.name}", "success")
        self.log(f"matrix: source {source} -> {engine}")

    def run_engine(self, engine: str | None) -> None:
        targets = self.targets(engine)
        if not targets:
            self.notify("No active files for this stage.", "warning")
            return
        try:
            report = AnalyzeBatchRunner(
                self.registry,
                cache_root=self.settings["cache_root"],
            ).run(tuple(self.target_to_run(row) for row in targets))
        except Exception as exc:
            self.notify(f"analysis failed: {exc}", "danger")
            self.log(f"analysis: {type(exc).__name__}: {exc}")
            return
        self.last_report = report
        self.mark_stage(StageStatus.COMPLETE)
        self.notify(f"analysis complete: {len(report.jobs)} job(s).", "success")
        self.log(f"analysis: complete {len(report.jobs)} job(s)")

    def target_to_run(self, row: MatrixRow) -> AnalyzeTarget:
        return AnalyzeTarget(
            project_path=Path(row.project_path),
            source_path=self.resolve_media_path(row.source_path, row.project_path),
            engine=row.engine,
            sample_id=row.sample_id,
            settings=self.engine_settings(row),
            cache_policy=row.cache_policy,
        )

    def engine_settings(self, row: MatrixRow) -> dict[str, Any]:
        if row.engine == "cellpose":
            frame_limit = row_int(row, "frame_limit", self.settings["cellpose_frame_limit"])
            return {
                "config_path": str(
                    row_setting(row, "config_path", self.settings["cellpose_config_path"]) or ""
                ),
                "px_to_um": row_float(row, "px_to_um", self.settings["cellpose_px_to_um"]),
                "frame_limit": frame_limit or None,
                "use_cache": row_bool(row, "use_cache", self.settings["cellpose_use_cache"]),
                "detect_inclusions": row_bool(
                    row,
                    "detect_inclusions",
                    self.settings["cellpose_detect_inclusions"],
                ),
            }
        end_frame = row_int(row, "end_frame", 0)
        metadata = video_metadata(self.resolve_media_path(row.source_path, row.project_path))
        roi_width = row_int(row, "roi_width", 0)
        roi_height = row_int(row, "roi_height", 0)
        settings = {
            "microns_per_pixel": row_float(
                row,
                "microns_per_pixel",
                self.settings["opencv_microns_per_pixel"],
            ),
            "fps": row_float(row, "fps", self.settings["opencv_fps"]),
            "start_frame": row_int(row, "start_frame", 0),
            "end_frame": end_frame or None,
            "roi_x": 0,
            "roi_y": 0,
            "roi_width": 0,
            "roi_height": 0,
        }
        if roi_width or roi_height:
            settings.update(
                {
                    "roi_x": row_int(row, "roi_x", 0),
                    "roi_y": row_int(row, "roi_y", 0),
                    "roi_width": roi_width or max(int(metadata.get("width") or 0), 0),
                    "roi_height": roi_height or max(int(metadata.get("height") or 0), 0),
                }
            )
        return settings

    def result_rows(self) -> list[dict[str, Any]]:
        if self.last_report is None:
            return []
        return [
            {
                "sample": job.sample_id,
                "engine": job.engine,
                "status": job.status,
                "rows": job.metadata.get("row_count", ""),
            }
            for job in self.last_report.jobs
        ]

    def matrix_row(self, row: MatrixRow) -> dict[str, Any]:
        return {
            "uid": row.uid,
            "project": Path(row.project_path).name,
            "source": Path(row.source_path).name or row.source_path,
            "engine": row.engine,
            "sample_id": row.sample_id,
            "cache": row.cache_policy,
            "active": row.active,
        }

    def targets(self, engine: str | None = None) -> list[MatrixRow]:
        return [
            row
            for row in self.matrix
            if row.active and (engine is None or row.engine == engine)
        ]

    def selected_row_for_engine(self, engine: str) -> MatrixRow | None:
        selected = next((row for row in self.matrix if row.uid == self.selected_uid), None)
        if selected is not None and (not engine or selected.engine == engine):
            return selected
        return next((row for row in self.matrix if not engine or row.engine == engine), None)

    def engine_for_current_stage(self) -> str | None:
        names = {field.name for field in self.workflow.current_stage(self.state).settings.params}
        if "config_path" in names:
            return "cellpose"
        if "microns_per_pixel" in names:
            return "opencv"
        return None

    def resolve_media_path(self, source: str, project_path: str | None = None) -> Path:
        path = Path(source)
        if path.is_absolute():
            return path
        project = session_path(project_path) if project_path else self.project_path_value()
        return project / path

    def project_options(self) -> dict[str, str]:
        return {str(ref.path): project_ref_label(ref) for ref in self.project_refs}

    def select_project(self, value: str | None) -> None:
        if value:
            self.load_project(session_path(value))

    def refresh_projects(self) -> None:
        self.project_refs = discover_projects(self.discovery_root)
        self.notify(f"found {len(self.project_refs)} project(s).", "success")
        self.refresh()

    def activate(self, index: int) -> None:
        index = max(0, min(index, len(self.workflow.stages) - 1))
        statuses = dict(self.state.statuses)
        current = self.workflow.current_stage(self.state)
        if statuses.get(current.id) is StageStatus.ACTIVE:
            statuses[current.id] = StageStatus.PENDING
        next_stage = self.workflow.stages[index]
        if statuses.get(next_stage.id) is StageStatus.PENDING:
            statuses[next_stage.id] = StageStatus.ACTIVE
        self.state = replace(self.state, index=index, statuses=statuses)
        self.refresh()

    def mark_stage(self, status: StageStatus) -> None:
        stage = self.workflow.current_stage(self.state)
        statuses = dict(self.state.statuses)
        statuses[stage.id] = status
        self.state = replace(self.state, statuses=statuses)

    def project_path_value(self) -> Path:
        value = str(self.project_path or "").strip()
        if not value:
            value = str(
                projects_root(self.discovery_root) / f"admet_{time.strftime('%Y%m%d_%H%M%S')}.admetp"
            )
            self.project_path = value
        return session_path(value)

    def notify(self, message: str, kind: str) -> None:
        self.notice = message
        self.notice_kind = kind

    def log(self, message: str) -> None:
        self.action_log.append(f"{time.strftime('%H:%M:%S')} {message}")

