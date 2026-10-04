from __future__ import annotations

from admet.core.engine import Param, ParamKind, ParamSchema

from .model import EditorSpec, SettingsSpec, Stage, StageAction, Workflow


IMPORT_SETTINGS = SettingsSpec(
    "matrix",
    title="Batch Matrix",
    options={
        "source": "all_targets",
        "columns": (
            "project",
            "source",
            "engine",
            "sample_id",
            "active",
        ),
    },
)
VIDEO_SETTINGS = SettingsSpec(
    "matrix",
    title="OpenCV Settings",
    options={
        "source": "opencv_targets",
        "engine": "opencv",
        "columns": ("project", "source", "sample_id", "active", "settings"),
    },
)
IMAGING_SETTINGS = SettingsSpec(
    "matrix",
    title="Cellpose Settings",
    options={
        "source": "cellpose_targets",
        "engine": "cellpose",
        "columns": ("project", "source", "sample_id", "active", "settings"),
    },
)
VIEW_SETTINGS = SettingsSpec("none", title="View Settings")
EXPORT_SETTINGS = SettingsSpec(
    "params",
    title="Export Settings",
    schema=ParamSchema((Param("export_dir", "Export Directory", ParamKind.PATH, default="exports"),)),
)
IMPORT_EDITOR = EditorSpec("import_inventory")
VIDEO_EDITOR = EditorSpec("opencv_video", options={"engine": "opencv"})
IMAGING_EDITOR = EditorSpec("cellpose_editor", options={"engine": "cellpose"})
VIEW_EDITOR = EditorSpec("analysis_results", options={"charts": ("diameter", "cv", "count")})
EXPORT_EDITOR = EditorSpec("analysis_export")

def create_analyze_workflow() -> Workflow:
    return Workflow(
        workflow_id="analyze",
        label="Analysis",
        stages=(
            Stage(
                "import",
                "1. Import & Batch",
                description="Create or open projects and build the file-to-engine matrix.",
                instructions=(
                    "Add videos or imaging folders.",
                    "Each row declares its project, sample id, engine, and whether it is active.",
                ),
                settings=ParamSchema(
                    (
                        Param("project_path", "Project Path", ParamKind.PATH, default=""),
                        Param("source_path", "Source Path", ParamKind.PATH, default=""),
                    )
                ),
                settings_panel=IMPORT_SETTINGS,
                actions=(
                    StageAction("Browse File", "browse_source", guard="project_ready", variant="secondary"),
                    StageAction("Reset Settings", "reset_settings", guard="has_matrix_rows", variant="secondary"),
                    StageAction("Clear Matrix", "clear_matrix", guard="has_matrix_rows", variant="secondary"),
                ),
                editor=IMPORT_EDITOR,
            ),
            Stage(
                "video",
                "2. Video Analysis",
                action="analyze",
                description="Run OpenCV on active video matrix rows.",
                settings=ParamSchema(
                    (
                        Param("microns_per_pixel", "Microns Per Pixel", ParamKind.FLOAT, default=1.0),
                        Param("fps", "FPS", ParamKind.FLOAT, default=0.0, minimum=0.0),
                    )
                ),
                settings_panel=VIDEO_SETTINGS,
                actions=(
                    StageAction(
                        "Run OpenCV",
                        "analyze",
                        guard="has_opencv_targets",
                        completes=True,
                    ),
                ),
                editor=VIDEO_EDITOR,
            ),
            Stage(
                "imaging",
                "3. Imaging Analysis",
                action="analyze",
                description="Run Cellpose on active imaging matrix rows.",
                settings=ParamSchema(
                    (
                        Param("px_to_um", "Pixels To Microns", ParamKind.FLOAT, default=1.14, minimum=0.0),
                        Param("frame_limit", "Frame Limit", ParamKind.INTEGER, default=None),
                        Param("detect_inclusions", "Detect Inclusions", ParamKind.BOOLEAN, default=True),
                    )
                ),
                settings_panel=IMAGING_SETTINGS,
                actions=(
                    StageAction(
                        "Run Cellpose",
                        "analyze",
                        guard="has_cellpose_targets",
                        completes=True,
                    ),
                ),
                editor=IMAGING_EDITOR,
            ),
            Stage(
                "view",
                "4. View Results",
                description="Inspect stored raw analysis runs and execution summaries.",
                instructions=(
                    "Views use raw JSONL and project run metadata.",
                    "Stored project runs remain available for review.",
                ),
                settings_panel=VIEW_SETTINGS,
                actions=(
                    StageAction("Refresh View", "refresh_view", completes=True, variant="success"),
                    StageAction("Run All", "analyze_all", guard="has_matrix_rows"),
                ),
                editor=VIEW_EDITOR,
            ),
            Stage(
                "export",
                "5. Export",
                skippable=True,
                description="Export figures and derived tables from stored raw data.",
                settings=EXPORT_SETTINGS.schema,
                settings_panel=EXPORT_SETTINGS,
                actions=(StageAction("Export Later", "export_later", variant="warning"),),
                editor=EXPORT_EDITOR,
            ),
        ),
    )
