from admet.core.schema import Param, ParamKind, ParamOption, ParamSchema
from admet.core.workflow import Stage, StageControl, Workflow
from admet.engines.control.fluidics.config import PIPELINES, SENSOR_CALIBRATIONS


def _pipeline_options() -> tuple[ParamOption, ...]:
    return tuple(ParamOption(name, name) for name in sorted(PIPELINES))


def _calibration_options() -> tuple[ParamOption, ...]:
    return tuple(ParamOption(name, name) for name in SENSOR_CALIBRATIONS)


def create_control_workflow() -> Workflow:
    return Workflow(
        workflow_id="control",
        label="Acquisition",
        stages=(
            Stage(
                "scene",
                "1. Scene setup",
                description="Camera discovery, connection, preview, and acquisition geometry.",
                settings=ParamSchema(
                    (
                        Param("camera_index", "Camera Index", ParamKind.INTEGER, default=0, minimum=0),
                        Param("camera_width", "Width", ParamKind.INTEGER, default=640, minimum=64),
                        Param("camera_height", "Height", ParamKind.INTEGER, default=240, minimum=1),
                        Param(
                            "camera_exposure_us",
                            "Exposure",
                            ParamKind.FLOAT,
                            default=100.0,
                            minimum=1.0,
                            step=10.0,
                        ),
                        Param("preview_enabled", "Preview", ParamKind.BOOLEAN, default=True),
                    )
                ),
                controls=(
                    StageControl("Refresh Cameras", "refresh_cameras", variant="secondary"),
                    StageControl("Connect Camera", "connect_camera"),
                    StageControl("Disconnect Camera", "disconnect_camera", variant="warning"),
                    StageControl("Scene Done", completes=True, variant="success"),
                ),
            ),
            Stage(
                "fluigent",
                "2. Fluigent connect",
                action="connect_fluidics",
                description="SDK import, device enumeration, optional simulated hardware, and polling.",
                settings=ParamSchema(
                    (
                        Param("simulated", "Simulated Hardware", ParamKind.BOOLEAN, default=False),
                        Param("start_polling", "Start Polling", ParamKind.BOOLEAN, default=True),
                    )
                ),
                controls=(
                    StageControl("Verify Backend", "verify_backend", variant="secondary"),
                    StageControl("Check Fluigent", "verify_fluigent", variant="secondary"),
                    StageControl("Connect", "connect_fluidics"),
                    StageControl("Disconnect", "disconnect_fluidics", variant="warning"),
                    StageControl("Continue", completes=True, variant="success"),
                ),
            ),
            Stage(
                "corrections",
                "3. Correction factors",
                description="Flow sensor calibration table and polynomial correction factors.",
                settings=ParamSchema(
                    (
                        Param(
                            "oil_calibration",
                            "Oil Calibration",
                            ParamKind.CHOICE,
                            default="IPA",
                            options=_calibration_options(),
                        ),
                        Param("oil_scale_a", "Oil a", ParamKind.FLOAT, default=2.25),
                        Param("oil_scale_b", "Oil b", ParamKind.FLOAT, default=0.0),
                        Param("oil_scale_c", "Oil c", ParamKind.FLOAT, default=0.0),
                        Param(
                            "cells_calibration",
                            "Cells Calibration",
                            ParamKind.CHOICE,
                            default="H2O",
                            options=_calibration_options(),
                        ),
                        Param("cells_scale_a", "Cells a", ParamKind.FLOAT, default=1.0),
                        Param("cells_scale_b", "Cells b", ParamKind.FLOAT, default=0.0),
                        Param("cells_scale_c", "Cells c", ParamKind.FLOAT, default=0.0),
                        Param(
                            "beads_calibration",
                            "Beads Calibration",
                            ParamKind.CHOICE,
                            default="H2O",
                            options=_calibration_options(),
                        ),
                        Param("beads_scale_a", "Beads a", ParamKind.FLOAT, default=1.0),
                        Param("beads_scale_b", "Beads b", ParamKind.FLOAT, default=0.0),
                        Param("beads_scale_c", "Beads c", ParamKind.FLOAT, default=0.0),
                    )
                ),
                controls=(StageControl("Mark Applied", completes=True, variant="success"),),
            ),
            Stage(
                "priming",
                "4. Priming protocol",
                action="run_protocol",
                description="Run the priming pipeline and confirm gated steps as prompted.",
                settings=ParamSchema(
                    (
                        Param(
                            "pipeline_name",
                            "Pipeline",
                            ParamKind.CHOICE,
                            default="Priming",
                            options=_pipeline_options(),
                        ),
                        Param("tick_s", "Pipeline Tick", ParamKind.FLOAT, default=0.2, minimum=0.001),
                    )
                ),
                controls=(
                    StageControl("Run Priming", "run_protocol"),
                    StageControl("Confirm Step", "confirm_protocol", variant="secondary"),
                    StageControl("Pause", "pause_protocol", variant="warning"),
                    StageControl("Resume", "resume_protocol", variant="secondary"),
                    StageControl("Stop", "stop_protocol", variant="danger"),
                    StageControl("Priming Done", completes=True, variant="success"),
                ),
            ),
            Stage(
                "chip_setup",
                "5. Chip setup",
                description="Manual chip positioning and flow tuning before recorded runs.",
                instructions=(
                    "Place the chip in view and verify the generation junction.",
                    "Tune flows manually until droplets are stable.",
                    "Continue only after the visual scene is ready for acquisition.",
                ),
                controls=(
                    StageControl("Chip Setup Done", completes=True, variant="success"),
                ),
            ),
            Stage(
                "runs",
                "6. Test runs",
                action="run_protocol",
                description="Recorded Drop-Seq or custom run protocol with CSV/video session output.",
                settings=ParamSchema(
                    (
                        Param(
                            "pipeline_name",
                            "Pipeline",
                            ParamKind.CHOICE,
                            default="Drop-Seq",
                            options=_pipeline_options(),
                        ),
                        Param("set_count", "Sets", ParamKind.INTEGER, default=1, minimum=1),
                        Param("replicate_count", "Replicates", ParamKind.INTEGER, default=1, minimum=1),
                        Param("run_volume_ul", "Oil Volume", ParamKind.FLOAT, default=150.0, minimum=0.1),
                        Param("log_dir", "Log Directory", ParamKind.PATH, default="logs"),
                        Param("tick_s", "Pipeline Tick", ParamKind.FLOAT, default=0.2, minimum=0.001),
                    )
                ),
                controls=(
                    StageControl("Start Recording", "start_recording", variant="secondary"),
                    StageControl("Run Protocol", "run_protocol"),
                    StageControl("Pause", "pause_protocol", variant="warning"),
                    StageControl("Resume", "resume_protocol", variant="secondary"),
                    StageControl("Stop Protocol", "stop_protocol", variant="danger"),
                    StageControl("Stop Recording", "stop_recording", variant="warning"),
                    StageControl("Runs Done", completes=True, variant="success"),
                ),
            ),
            Stage(
                "wash",
                "7. Post-process wash",
                action="wash",
                skippable=True,
                confirmation_required=True,
                description="Post-run wash and shutdown path.",
                settings=ParamSchema(
                    (
                        Param("tick_s", "Pipeline Tick", ParamKind.FLOAT, default=0.2, minimum=0.001),
                    )
                ),
                controls=(
                    StageControl("Run Wash", "wash", variant="warning"),
                    StageControl("Confirm Step", "confirm_protocol", variant="secondary"),
                    StageControl("Stop", "stop_protocol", variant="danger"),
                    StageControl("Skip Wash", skippable=True, variant="secondary"),
                    StageControl("Workflow Done", completes=True, variant="success"),
                ),
            ),
        ),
    )
