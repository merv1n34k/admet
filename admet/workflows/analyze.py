from admet.core.schema import Param, ParamKind, ParamSchema
from admet.core.workflow import Stage, StageControl, Workflow


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
                    "Each row declares its project, sample id, engine, and cache policy.",
                ),
                settings=ParamSchema(
                    (
                        Param("project_path", "Project Path", ParamKind.PATH, default=""),
                        Param("source_path", "Source Path", ParamKind.PATH, default=""),
                    )
                ),
                controls=(StageControl("Add Target", completes=True, variant="success"),),
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
                        Param("max_frames", "Max Frames", ParamKind.INTEGER, default=None),
                    )
                ),
                controls=(StageControl("Run OpenCV", "analyze", completes=True),),
            ),
            Stage(
                "imaging",
                "3. Imaging Analysis",
                action="analyze",
                description="Run Cellpose on active imaging matrix rows.",
                settings=ParamSchema(
                    (
                        Param("config_path", "Config Path", ParamKind.PATH, default=""),
                        Param("px_to_um", "Pixels To Microns", ParamKind.FLOAT, default=1.14, minimum=0.0),
                        Param("frame_limit", "Frame Limit", ParamKind.INTEGER, default=None),
                        Param("use_cache", "Use Cache", ParamKind.BOOLEAN, default=True),
                        Param("detect_inclusions", "Detect Inclusions", ParamKind.BOOLEAN, default=True),
                    )
                ),
                controls=(StageControl("Run Cellpose", "analyze", completes=True),),
            ),
            Stage(
                "view",
                "4. View Results",
                description="Inspect stored raw analysis runs and execution summaries.",
                instructions=(
                    "Views use raw JSONL and project run metadata.",
                    "Skipped rows remain available through prior stored project runs.",
                ),
                controls=(StageControl("Refresh View", completes=True, variant="success"),),
            ),
            Stage(
                "export",
                "5. Export",
                skippable=True,
                description="Export figures and derived tables from stored raw data.",
                settings=ParamSchema(
                    (
                        Param("export_dir", "Export Directory", ParamKind.PATH, default="exports"),
                    )
                ),
                controls=(
                    StageControl("Skip Export", skippable=True, variant="secondary"),
                    StageControl("Export Done", completes=True, variant="success"),
                ),
            ),
        ),
    )
