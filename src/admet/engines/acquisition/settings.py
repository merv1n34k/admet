from __future__ import annotations

from admet.core.engine import Param, ParamKind, ParamOption, ParamSchema
from admet.engines.acquisition.fluidics.config import (
    FLUIDIC_CHANNEL_UNITS,
    FLUIDIC_CHANNELS,
    SENSOR_CALIBRATIONS,
)
from admet.engines.acquisition.fluidics.liquids import default_profile_id, profiles_for_unit

MANUAL_STOPS = ("none", "volume", "time", "flow_above", "flow_below", "pressure_above", "pressure_below")

CORRECTION_PARAM_NAMES = tuple(
    name
    for prefix, _label, _calibration, _scale, _offset, _quadratic in FLUIDIC_CHANNELS
    for name in (
        f"{prefix}_calibration",
        f"{prefix}_scale",
        f"{prefix}_offset",
        f"{prefix}_quadratic",
    )
)

LIQUID_PROFILE_PARAM_NAMES = tuple(
    f"{prefix}_profile" for prefix, *_rest in FLUIDIC_CHANNELS
)

# Rig values kept with the calibration but never sent to the flow units.
DEAD_VOLUME_PARAM_NAMES = tuple(
    f"{prefix}_dead_volume_ul" for prefix, *_rest in FLUIDIC_CHANNELS
)


def merge_schemas(*schemas: ParamSchema) -> ParamSchema:
    params: dict[str, Param] = {}
    for schema in schemas:
        for param in schema.params:
            params[param.name] = param
    return ParamSchema(tuple(params.values()))


def _calibration_options() -> tuple[ParamOption, ...]:
    return tuple(ParamOption(name, name) for name in SENSOR_CALIBRATIONS)


CAMERA_SETTINGS = ParamSchema(
    (
        Param("camera_index", "Camera Index", ParamKind.INTEGER, default=0, minimum=0),
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
        Param("camera_video_fps", "Video FPS", ParamKind.FLOAT, default=24.0, minimum=1.0),
        Param("camera_preview_off_recording", "Disable Preview During Recording", ParamKind.BOOLEAN, default=False),
    )
)

RECORDING_SETTINGS = ParamSchema(
    (
        Param("recording_root", "Recording Root", ParamKind.PATH, default=""),
        Param("recording_label", "Recording Label", ParamKind.TEXT, default="recording"),
        Param(
            "include_video",
            "Include Video",
            ParamKind.BOOLEAN,
            default=False,
            description="Record video as well as fluidics. Off unless asked for, so a "
            "camera that happens to be live cannot add a file nobody wanted",
        ),
        # Each frame is written as its own file, so an unattended recording can fill
        # the disk. Recording stops once either limit is reached; 0 disables it.
        Param("recording_max_frames", "Max Frames", ParamKind.INTEGER, default=100_000, minimum=0),
        Param("recording_max_seconds", "Max Duration", ParamKind.FLOAT, default=0.0, minimum=0.0),
    )
)

FLUIGENT_SETTINGS = ParamSchema(
    (
        Param("simulated", "Simulated Hardware", ParamKind.BOOLEAN, default=False),
        Param("start_polling", "Start Polling", ParamKind.BOOLEAN, default=True),
    )
)

def _profile_options(unit: str) -> tuple[ParamOption, ...]:
    return tuple(ParamOption(profile.id, profile.name) for profile in profiles_for_unit(unit))


CORRECTION_SETTINGS = ParamSchema(
    tuple(
        param
        for prefix, label, calibration, scale, offset, quadratic in FLUIDIC_CHANNELS
        for param in (
            Param(
                f"{prefix}_profile",
                f"{label} Liquid",
                ParamKind.CHOICE,
                default=default_profile_id(
                    prefix, FLUIDIC_CHANNEL_UNITS[prefix], calibration, scale
                ),
                options=_profile_options(FLUIDIC_CHANNEL_UNITS[prefix]),
            ),
            Param(
                f"{prefix}_calibration",
                f"{label} Calibration",
                ParamKind.CHOICE,
                default=calibration,
                options=_calibration_options(),
            ),
            Param(f"{prefix}_scale", f"{label} Scale", ParamKind.FLOAT, default=scale),
            Param(f"{prefix}_offset", f"{label} Offset", ParamKind.FLOAT, default=offset),
            Param(f"{prefix}_quadratic", f"{label} Quadratic", ParamKind.FLOAT, default=quadratic),
            Param(f"{prefix}_dead_volume_ul", f"{label} Dead volume, µL", ParamKind.FLOAT,
                  default=0.0, minimum=0.0,
                  description="Tubing volume from the source to the chip on this rig; 0 = not measured."),
        )
    )
)

CHANNEL_CONTROL_SETTINGS = ParamSchema(
    (
        Param("channel_index", "Channel Index", ParamKind.INTEGER, default=0, minimum=0),
        Param("channel_flow_ul_min", "Flow", ParamKind.FLOAT, default=0.0, minimum=0.0),
        Param("channel_pressure_mbar", "Pressure", ParamKind.FLOAT, default=0.0, minimum=0.0),
        Param("channel_response_s", "Response", ParamKind.INTEGER, default=2, minimum=2, maximum=3600),
        # An optional rule that ends a command set by hand: after a volume or a
        # time, or once the flow or pressure crosses a value.
        Param("channel_stop_after", "Stop after", ParamKind.CHOICE, default="none",
              options=tuple(ParamOption(kind, kind) for kind in MANUAL_STOPS)),
        Param("channel_stop_value", "Stop value", ParamKind.FLOAT, default=0.0, minimum=0.0),
    )
)

CONTROL_ENGINE_SETTINGS = merge_schemas(
    FLUIGENT_SETTINGS,
    CORRECTION_SETTINGS,
    CHANNEL_CONTROL_SETTINGS,
    RECORDING_SETTINGS,
    CAMERA_SETTINGS,
)
