from __future__ import annotations

from admet.engines.acquisition.settings import (
    CAMERA_SETTINGS,
    CORRECTION_SETTINGS,
    FLUIGENT_SETTINGS,
)
from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNELS
from admet.core.engine import Param, ParamKind, ParamSchema

from .model import EditorSpec, ResultsSpec, Stage, StageAction, StageInstruction, StageSurface, Workflow


CAMERA_SURFACE_OPTIONS = {
    "camera_index_param": "camera_index",
    "groups": (
        {
            "title": "ROI",
            "icon": "crop",
            "params": (
                "camera_width",
                "camera_height",
                "camera_offset_x",
                "camera_offset_y",
                "camera_binning_h",
                "camera_binning_v",
            ),
        },
        {
            "title": "Acquisition",
            "icon": "speed",
            "params": (
                "camera_exposure_us",
                "camera_gain",
                "camera_pixel_format",
                "camera_readout",
            ),
        },
        {
            "title": "Frame Rate",
            "icon": "timer",
            "params": (
                "camera_framerate_enabled",
                "camera_framerate_hz",
                "camera_throughput_enabled",
                "camera_throughput_mbps",
            ),
        },
        {
            "title": "Capture",
            "icon": "fiber_manual_record",
            "params": (
                "camera_video_fps",
                "camera_preview_off_recording",
            ),
        },
    ),
}


CONTROL_LIVE_EDITOR = EditorSpec(
    "control_live",
    persistent=True,
    surfaces=(
        StageSurface(
            "camera",
            "Camera",
            settings=CAMERA_SETTINGS,
            options=CAMERA_SURFACE_OPTIONS,
        ),
        StageSurface("fluidics", "Fluidics"),
    ),
    options={"layout": "pressure-flow-camera"},
)

CONTROL_RESULTS = ResultsSpec(
    "control_records",
    "Records",
    options={"columns": ("label", "video", "fluidics", "fps", "dimensions", "created")},
)

CAMERA_MAIN_SETTINGS = (
    "camera_width",
    "camera_height",
    "camera_exposure_us",
    "camera_gain",
    "camera_pixel_format",
    "camera_readout",
)
FLUIDICS_MAIN_SETTINGS = ("simulated",)


# A channel is set by picking a liquid; the raw correction terms the profile writes
# stay visible underneath so the values are never hidden from the operator.
CORRECTION_PRIMARY_PARAMS = tuple(f"{prefix}_profile" for prefix, *_rest in FLUIDIC_CHANNELS)
CORRECTION_SECONDARY_PARAMS = tuple(
    name
    for prefix, _label, _calibration, _scale, _offset, _quadratic in FLUIDIC_CHANNELS
    for name in (
        f"{prefix}_calibration",
        f"{prefix}_scale",
        f"{prefix}_offset",
        f"{prefix}_quadratic",
    )
)


def protocol_stage(stage_id, label, *, builtin=""):
    from admet.workflows.operations import operation

    params = ()
    if builtin:
        params = tuple(p for p in operation("run_" + builtin).params if p.name != "tick_s")
        if builtin == "wash":
            from dataclasses import replace

            params = tuple(replace(p, default=1800.0) if p.name == "wash_pressure_mbar" else p for p in params)
        params += (Param("desktop_pressure_limit_mbar", "Pressure trip (mbar, optional)", ParamKind.FLOAT,
                         default=None, minimum=1.0),)
    options = {"builtin": builtin, "main": tuple(p.name for p in params)}
    return Stage(
        stage_id, label, pipeline=True,
        instructions=("Review the targets, build a plan, then Execute. Confirm each gate when ready.",),
        settings=ParamSchema(params),
        editor=CONTROL_LIVE_EDITOR, results=CONTROL_RESULTS,
        features=("fluidics", "json_protocol"),
        settings_options=options,
    )


def builtin_document(kind, values):
    from admet.workflows.protocols import (
        build_priming_protocol, build_wash_protocol,
    )
    from admet.workflows.json_protocol import normalize

    builder = {
        "priming": build_priming_protocol, "wash": build_wash_protocol,
    }[kind]
    steps = []
    for step in builder(values):
        params = step.trigger_params
        if step.trigger_type == "volume":
            nominal = params["target_volume_ul"] / step.sensor_setpoints[params["sensor_index"]] * 60
            timeout = 30 + 3 * nominal
        else:
            timeout = params["duration_s"] + 5
        steps.append({
            "name": step.name,
            "sensor_setpoints": {str(k): v for k, v in step.sensor_setpoints.items()},
            "pressure_setpoints": {str(k): v for k, v in step.pressure_setpoints.items()},
            "trigger_type": step.trigger_type, "trigger_params": dict(params),
            "timeout_s": timeout, "on_complete": "zero", "confirm_message": step.confirm_message,
        })
    return normalize({
        "name": kind,
        "pressure_limits_mbar": ({str(i): values["desktop_pressure_limit_mbar"] for i in range(3)}
                                 if values.get("desktop_pressure_limit_mbar") is not None else {}),
        "steps": steps,
    })


def create_control_workflow() -> Workflow:
    return Workflow(
        workflow_id="control",
        label="Acquisition",
        stages=(
            Stage(
                "scene",
                "1. Scene setup",
                description="Camera discovery, connection, preview, and acquisition geometry.",
                instructions=(
                    "Create or load an ADMET project before connecting the camera.",
                    "Select a camera in the main editor, connect it, and turn live preview on.",
                ),
                instruction_cards=(
                    StageInstruction(
                        "Create or select an admet project, then refresh and connect the camera.",
                        "not project_ready",
                    ),
                    StageInstruction(
                        "Refresh cameras, choose the camera in Settings, then connect it.",
                        "project_ready and not camera_connected",
                    ),
                    StageInstruction(
                        "Start Live to preview the connected camera and apply camera settings.",
                        "camera_connected and not camera_live",
                    ),
                    StageInstruction("Camera live preview is active. Adjust camera settings or move to Fluigent."),
                ),
                settings=CAMERA_SETTINGS,
                actions=(
                    StageAction("Refresh", "refresh_cameras", guard="project_ready", variant="secondary"),
                    StageAction(
                        "Connect",
                        "connect_camera",
                        guard="project_ready and not camera_connected",
                    ),
                    StageAction(
                        "Disconnect",
                        "disconnect_camera",
                        guard="camera_connected",
                        variant="warning",
                    ),
                    StageAction(
                        "Live",
                        "start_camera_live",
                        off_action="stop_camera_live",
                        guard="camera_connected",
                        active_when="camera_live",
                        kind="toggle",
                    ),
                    StageAction("Continue", completes=True, guard="camera_live", variant="warning"),
                ),
                editor=CONTROL_LIVE_EDITOR,
                results=CONTROL_RESULTS,
                features=("camera",),
                show_settings=False,
                settings_options={
                    "main": CAMERA_MAIN_SETTINGS,
                },
            ),
            Stage(
                "fluigent",
                "2. Fluigent connect",
                action="connect_fluidics",
                description="SDK import, device enumeration, connection and polling.",
                instructions=(
                    "Connect Fluigent once for the project session.",
                ),
                instruction_cards=(
                    StageInstruction(
                        "Connect Fluigent, then review detected channels.",
                        "not fluidics_connected",
                    ),
                    StageInstruction("Fluigent is connected. Review channel readings and continue when ready."),
                ),
                settings=FLUIGENT_SETTINGS,
                actions=(
                    StageAction("Connect", "connect_fluidics", guard="project_ready and not fluidics_connected"),
                    StageAction(
                        "Disconnect",
                        "disconnect_fluidics",
                        guard="fluidics_connected",
                        variant="warning",
                    ),
                    StageAction("Continue", completes=True, guard="fluidics_connected", variant="warning"),
                ),
                editor=CONTROL_LIVE_EDITOR,
                results=CONTROL_RESULTS,
                features=("fluidics", "fluidics_preflight"),
                settings_options={
                    "main": FLUIDICS_MAIN_SETTINGS,
                },
            ),
            Stage(
                "corrections",
                "3. Correction factors",
                description="Flow sensor calibration table and polynomial correction factors.",
                instructions=("Apply all correction factors after editing the calibration values.",),
                settings=CORRECTION_SETTINGS,
                actions=(
                    StageAction(
                        "Apply All Corrections",
                        "apply_corrections",
                        guard="fluidics_connected",
                    ),
                    StageAction(
                        "Continue",
                        completes=True,
                        guard="fluidics_connected and corrections_applied",
                        variant="warning",
                    ),
                ),
                editor=CONTROL_LIVE_EDITOR,
                results=CONTROL_RESULTS,
                features=("fluidics",),
                settings_options={
                    "primary": CORRECTION_PRIMARY_PARAMS,
                    "secondary": CORRECTION_SECONDARY_PARAMS,
                    "collapsed_count": 6,
                },
            ),
            protocol_stage("priming", "4. Priming", builtin="priming"),
            Stage(
                "checkup", "5. Checkup / chip layout",
                instructions=("Review your chip/tubing layout and calculations. Save the project to keep them. "
                              "Measurement runs are editable JSON protocols in Experiment steps; this page does not actuate.",),
                features=("checkup",),
                actions=(StageAction("Continue", completes=True),),
                settings_options={"sections": ("flow", "layout", "system", "gravimetric", "consumption")},
            ),
            protocol_stage("experiment_1", "6. Experiment 1"),
            protocol_stage("wash", "7. Wash", builtin="wash"),
            Stage(
                "calculations", "Calculations",
                instructions=("Choose a saved run and a calculation. Results stay with the recording in the project.",),
                features=("calculations",),
                actions=(StageAction("Continue", completes=True),),
                show_settings=False,
            ),
            Stage(
                "cleanup",
                "Cleanup",
                description="Disconnect devices and finish the control session.",
                instructions=(
                    "Disconnect devices, clean the chip area, check tubing, and confirm the desk is clear.",
                ),
                instruction_cards=(
                    StageInstruction(
                        "Disconnect devices, clean the chip area, check tubing, and confirm the desk is clear."
                    ),
                ),
                actions=(
                    StageAction("Disconnect Devices", "cleanup_shutdown", variant="warning"),
                    StageAction("Reset safety", "reset_safety", guard="not pipeline_running", variant="warning"),
                    StageAction(
                        "Continue",
                        completes=True,
                        suggest_when="devices_released",
                        variant="warning",
                    ),
                ),
                editor=CONTROL_LIVE_EDITOR,
                results=CONTROL_RESULTS,
                features=("camera", "fluidics"),
                show_settings=False,
            ),
        ),
    )
