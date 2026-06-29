from __future__ import annotations

import os

from admet.core.schema import Param, ParamKind, ParamOption, ParamSchema
from admet.engines.control.fluidics.config import PIPELINES, SENSOR_CALIBRATIONS


def merge_schemas(*schemas: ParamSchema) -> ParamSchema:
    params: dict[str, Param] = {}
    for schema in schemas:
        for param in schema.params:
            params[param.name] = param
    return ParamSchema(tuple(params.values()))


def _pipeline_options() -> tuple[ParamOption, ...]:
    return tuple(ParamOption(name, name) for name in sorted(PIPELINES))


def _calibration_options() -> tuple[ParamOption, ...]:
    return tuple(ParamOption(name, name) for name in SENSOR_CALIBRATIONS)


CAMERA_SETTINGS = ParamSchema(
    (
        Param("camera_index", "Camera Index", ParamKind.INTEGER, default=0, minimum=0),
        Param("pylon_camemu", "PYLON_CAMEMU", ParamKind.TEXT, default=os.environ.get("PYLON_CAMEMU", "")),
        Param("camera_width", "Width", ParamKind.INTEGER, default=640, minimum=16),
        Param("camera_height", "Height", ParamKind.INTEGER, default=480, minimum=1),
        Param("camera_offset_x", "Offset X", ParamKind.INTEGER, default=0, minimum=0, step=16),
        Param("camera_offset_y", "Offset Y", ParamKind.INTEGER, default=0, minimum=0, step=16),
        Param("camera_binning_h", "Binning H", ParamKind.INTEGER, default=1, minimum=1),
        Param("camera_binning_v", "Binning V", ParamKind.INTEGER, default=1, minimum=1),
        Param("camera_exposure_us", "Exposure", ParamKind.FLOAT, default=150.0, minimum=1.0, step=10.0),
        Param("camera_gain", "Gain", ParamKind.FLOAT, default=0.0, minimum=0.0),
        Param(
            "camera_pixel_format",
            "Pixel Format",
            ParamKind.CHOICE,
            default="Mono8",
            options=(
                ParamOption("Mono8", "Mono8"),
                ParamOption("Mono10", "Mono10"),
                ParamOption("Mono10p", "Mono10p"),
            ),
        ),
        Param(
            "camera_readout",
            "Sensor Readout",
            ParamKind.CHOICE,
            default="Fast",
            options=(ParamOption("Fast", "Fast"), ParamOption("Normal", "Normal")),
        ),
        Param("camera_framerate_enabled", "Limit Frame Rate", ParamKind.BOOLEAN, default=False),
        Param("camera_framerate_hz", "Frame Rate", ParamKind.FLOAT, default=30.0, minimum=1.0),
        Param("camera_throughput_enabled", "Limit Throughput", ParamKind.BOOLEAN, default=False),
        Param("camera_throughput_mbps", "Throughput", ParamKind.FLOAT, default=125.0, minimum=1.0),
        Param("camera_waterfall", "Waterfall", ParamKind.BOOLEAN, default=False),
        Param("preview_enabled", "Preview", ParamKind.BOOLEAN, default=True),
        Param("camera_flip_x", "Flip X", ParamKind.BOOLEAN, default=False),
        Param("camera_flip_y", "Flip Y", ParamKind.BOOLEAN, default=False),
        Param(
            "camera_rotation",
            "Rotation",
            ParamKind.CHOICE,
            default=0,
            options=(
                ParamOption(0, "0"),
                ParamOption(90, "90"),
                ParamOption(180, "180"),
                ParamOption(270, "270"),
            ),
        ),
        Param("camera_ruler_v", "Vertical Ruler", ParamKind.BOOLEAN, default=False),
        Param("camera_ruler_h", "Horizontal Ruler", ParamKind.BOOLEAN, default=False),
        Param("camera_ruler_radial", "Radial Ruler", ParamKind.BOOLEAN, default=False),
        Param("camera_selection_x", "Selection X", ParamKind.INTEGER, default=0, minimum=0),
        Param("camera_selection_y", "Selection Y", ParamKind.INTEGER, default=0, minimum=0),
        Param("camera_selection_w", "Selection W", ParamKind.INTEGER, default=0, minimum=0),
        Param("camera_selection_h", "Selection H", ParamKind.INTEGER, default=0, minimum=0),
        Param("camera_output_dir", "Output Path", ParamKind.PATH, default="output"),
        Param("camera_image_prefix", "Image Prefix", ParamKind.TEXT, default="img"),
        Param("camera_video_prefix", "Video Prefix", ParamKind.TEXT, default="vid"),
        Param("camera_video_fps", "Video FPS", ParamKind.FLOAT, default=24.0, minimum=1.0),
        Param("camera_preview_off_recording", "Disable Preview During Recording", ParamKind.BOOLEAN, default=True),
    )
)

FLUIGENT_SETTINGS = ParamSchema(
    (
        Param("simulated", "Simulated Hardware", ParamKind.BOOLEAN, default=False),
        Param("start_polling", "Start Polling", ParamKind.BOOLEAN, default=True),
    )
)

CORRECTION_SETTINGS = ParamSchema(
    (
        Param("oil_calibration", "Oil Calibration", ParamKind.CHOICE, default="IPA", options=_calibration_options()),
        Param("oil_scale_a", "Oil a", ParamKind.FLOAT, default=2.25),
        Param("oil_scale_b", "Oil b", ParamKind.FLOAT, default=0.0),
        Param("oil_scale_c", "Oil c", ParamKind.FLOAT, default=0.0),
        Param("cells_calibration", "Cells Calibration", ParamKind.CHOICE, default="H2O", options=_calibration_options()),
        Param("cells_scale_a", "Cells a", ParamKind.FLOAT, default=1.0),
        Param("cells_scale_b", "Cells b", ParamKind.FLOAT, default=0.0),
        Param("cells_scale_c", "Cells c", ParamKind.FLOAT, default=0.0),
        Param("beads_calibration", "Beads Calibration", ParamKind.CHOICE, default="H2O", options=_calibration_options()),
        Param("beads_scale_a", "Beads a", ParamKind.FLOAT, default=1.0),
        Param("beads_scale_b", "Beads b", ParamKind.FLOAT, default=0.0),
        Param("beads_scale_c", "Beads c", ParamKind.FLOAT, default=0.0),
    )
)

PROTOCOL_SETTINGS = ParamSchema(
    (
        Param("pipeline_name", "Pipeline", ParamKind.CHOICE, default="Priming", options=_pipeline_options()),
        Param("tick_s", "Pipeline Tick", ParamKind.FLOAT, default=0.2, minimum=0.001),
    )
)

RUN_SETTINGS = ParamSchema(
    (
        Param("pipeline_name", "Pipeline", ParamKind.CHOICE, default="Drop-Seq", options=_pipeline_options()),
        Param("set_count", "Sets", ParamKind.INTEGER, default=1, minimum=1),
        Param("replicate_count", "Replicates", ParamKind.INTEGER, default=1, minimum=1),
        Param("run_volume_ul", "Oil Volume", ParamKind.FLOAT, default=150.0, minimum=0.1),
        Param("log_dir", "Log Directory", ParamKind.PATH, default="logs"),
        Param("tick_s", "Pipeline Tick", ParamKind.FLOAT, default=0.2, minimum=0.001),
    )
)

WASH_SETTINGS = ParamSchema(
    (
        Param("tick_s", "Pipeline Tick", ParamKind.FLOAT, default=0.2, minimum=0.001),
    )
)

CONTROL_ENGINE_SETTINGS = merge_schemas(
    FLUIGENT_SETTINGS,
    CORRECTION_SETTINGS,
    RUN_SETTINGS,
    WASH_SETTINGS,
    PROTOCOL_SETTINGS,
    CAMERA_SETTINGS,
)
