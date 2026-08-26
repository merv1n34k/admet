from __future__ import annotations

from admet.engines.acquisition.settings import (
    CAMERA_SETTINGS,
    CORRECTION_SETTINGS,
    CHARACTERISE_SETTINGS,
    FLUIGENT_SETTINGS,
    GRAVIMETRY_SETTINGS,
    PROTOCOL_SETTINGS,
    RUN_SETTINGS,
    WASH_SETTINGS,
)
from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNELS

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
PRIMING_MAIN_SETTINGS = (
    "prime_oil_volume_ul",
    "prime_aqueous_volume_ul",
)
RUN_MAIN_SETTINGS = (
    "set_count",
    "replicate_count",
    "run_volume_ul",
    "run_oil_flow_ul_min",
    "run_aqueous_total_flow_ul_min",
)
CHARACTERISE_MAIN_SETTINGS = (
    "run_oil_flow_ul_min",
    "run_aqueous_total_flow_ul_min",
    "sweep_tolerance_ul_min",
    "sweep_window_s",
    "sweep_timeout_s",
)
GRAVIMETRY_MAIN_SETTINGS = (
    "gravimetric_target_ul",
    "gravimetric_flow_ul_min",
    "gravimetric_replicates",
)
WASH_MAIN_SETTINGS = (
    "wash_oil_flow_ul_min",
    "wash_aqueous_total_flow_ul_min",
    "wash_oil_volume_ul",
    "wash_pressure_mbar",
    "wash_pressure_duration_s",
)


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
                description="SDK import, device enumeration, optional simulated hardware, and polling.",
                instructions=(
                    "Connect Fluigent once for the project session.",
                    "Simulated mode must be selected before connecting when no device is attached.",
                ),
                instruction_cards=(
                    StageInstruction(
                        "Connect Fluigent. Use simulated mode only when no instrument is attached.",
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
            Stage(
                "priming",
                "4. Priming protocol",
                action="run_protocol",
                description="Run the priming pipeline and confirm gated steps as prompted.",
                instructions=("Run priming and press Proceed when the protocol asks for confirmation.",),
                settings=PROTOCOL_SETTINGS,
                actions=(
                    StageAction(
                        "Start Priming",
                        "run_protocol",
                        guard="fluidics_connected",
                        active_when="pipeline_running",
                        kind="pipeline",
                    ),
                    StageAction("Pause", "pause_protocol", guard="pipeline_running", variant="secondary"),
                    StageAction("Proceed", "confirm_protocol", guard="pipeline_waiting", variant="warning"),
                    StageAction("Skip Step", "skip_protocol", guard="pipeline_waiting", variant="secondary"),
                    StageAction(
                        "Continue",
                        completes=True,
                        guard="not pipeline_running",
                        suggest_when="pipeline_complete",
                        variant="warning",
                    ),
                ),
                editor=CONTROL_LIVE_EDITOR,
                results=CONTROL_RESULTS,
                features=("fluidics",),
                pipeline=True,
                skippable=True,
                settings_options={
                    "main": PRIMING_MAIN_SETTINGS,
                    "pipeline_name": "Priming",
                    "pipeline_label": "Start Priming",
                    "completion_message": "Priming complete. The lines are wet, so the checks can be run.",
                },
            ),
            Stage(
                "gravimetry",
                "5. System check: dispense",
                action="run_protocol",
                description="Dispense a weighed volume per channel to check what the sensors report.",
                instructions=(
                    "Optional. Run it when a channel's readings are suspect, or after changing a "
                    "liquid, to derive the scale its profile should carry.",
                ),
                instruction_cards=(
                    StageInstruction(
                        "Dispense check needed. Run the dispenses, or load the last one if "
                        "nothing about the setup has changed.",
                        "check_due",
                    ),
                    StageInstruction(
                        "Have the balance ready. Each replicate stops twice: once to place a tube "
                        "weighed empty, once to weigh it full.",
                    ),
                    StageInstruction(
                        "Enter both masses in the dispense table below as you go. The factor "
                        "it derives is what belongs in the liquid profile.",
                    ),
                ),
                settings=GRAVIMETRY_SETTINGS,
                actions=(
                    StageAction(
                        "Start Dispense",
                        "run_protocol",
                        guard="fluidics_connected and corrections_applied",
                        active_when="pipeline_running",
                        kind="pipeline",
                    ),
                    StageAction("Pause", "pause_protocol", guard="pipeline_running", variant="secondary"),
                    StageAction("Proceed", "confirm_protocol", guard="pipeline_waiting", variant="warning"),
                    StageAction("Skip Step", "skip_protocol", guard="pipeline_waiting", variant="secondary"),
                    StageAction(
                        "Load Last",
                        "load_last_check",
                        guard="check_recorded and not pipeline_running",
                        variant="secondary",
                    ),
                    StageAction(
                        "Continue",
                        completes=True,
                        guard="check_satisfied and not pipeline_running",
                        suggest_when="pipeline_complete",
                        variant="warning",
                    ),
                ),
                editor=CONTROL_LIVE_EDITOR,
                results=CONTROL_RESULTS,
                features=("fluidics",),
                pipeline=True,
                skippable=True,
                settings_options={
                    "sections": ("gravimetric",),
                    "main": GRAVIMETRY_MAIN_SETTINGS,
                    "pipeline_name": "Gravimetry",
                    "pipeline_label": "Start Dispense",
                    "completion_message": (
                        "Dispenses complete. The factors are in the table below, and the "
                        "check is stored with the project."
                    ),
                },
            ),
            Stage(
                "characterise",
                "6. System check: flow",
                action="run_protocol",
                description="Sweep the working flows to measure what the plumbing and chip cost.",
                instructions=(
                    "Optional. Run it after a chip, tubing or liquid change to learn whether the "
                    "target flows are reachable before spending reagent.",
                ),
                instruction_cards=(
                    StageInstruction(
                        "This setup cannot reach the target flows. Lower the flows, raise the "
                        "pressure limit, or shorten and widen the tubing -- the outlet first.",
                        "check_infeasible",
                    ),
                    StageInstruction(
                        "Flow check needed. Run the sweep, or load the last one if nothing "
                        "about the setup has changed.",
                        "check_due",
                    ),
                    StageInstruction(
                        "Apply the correction factors first, or the measured flows are not true flows.",
                        "not corrections_applied",
                    ),
                    StageInstruction(
                        "Start the sweep. Every channel is scaled together so the phase ratio holds; "
                        "each step settles before the next. Readings land in the table below.",
                    ),
                ),
                settings=CHARACTERISE_SETTINGS,
                actions=(
                    StageAction(
                        "Start Sweep",
                        "run_protocol",
                        guard="fluidics_connected and corrections_applied",
                        active_when="pipeline_running",
                        kind="pipeline",
                    ),
                    StageAction("Pause", "pause_protocol", guard="pipeline_running", variant="secondary"),
                    StageAction("Skip Step", "skip_protocol", guard="pipeline_waiting", variant="secondary"),
                    StageAction(
                        "Load Last",
                        "load_last_check",
                        guard="check_recorded and not pipeline_running",
                        variant="secondary",
                    ),
                    StageAction(
                        "Continue",
                        completes=True,
                        guard="check_satisfied and not pipeline_running",
                        suggest_when="pipeline_complete",
                        variant="warning",
                    ),
                ),
                editor=CONTROL_LIVE_EDITOR,
                results=CONTROL_RESULTS,
                features=("fluidics",),
                pipeline=True,
                skippable=True,
                settings_options={
                    "sections": ("layout", "system"),
                    "main": CHARACTERISE_MAIN_SETTINGS,
                    "pipeline_name": "Characterise",
                    "pipeline_label": "Start Sweep",
                    "completion_message": (
                        "Sweep complete. The readings are in the table below, and the "
                        "check is stored with the project."
                    ),
                },
            ),
            Stage(
                "runs",
                "7. Test runs",
                action="run_protocol",
                description="Recorded Drop-Seq or custom run protocol with CSV/video session output.",
                instructions=("Run the test protocol. Recording is managed by the protocol.",),
                instruction_cards=(
                    StageInstruction("Connect Fluigent before starting test runs.", "not fluidics_connected"),
                    StageInstruction(
                        "Set run count, Oil L volume, and total aqueous flow, then start test runs. "
                        "Camera recording starts automatically when live preview is active."
                    ),
                ),
                settings=RUN_SETTINGS,
                actions=(
                    StageAction(
                        "Start Run",
                        "run_protocol",
                        guard="camera_live and fluidics_connected",
                        active_when="pipeline_running",
                        kind="pipeline",
                    ),
                    StageAction("Pause", "pause_protocol", guard="pipeline_running", variant="secondary"),
                    StageAction("Proceed", "confirm_protocol", guard="pipeline_waiting", variant="warning"),
                    StageAction("Skip Step", "skip_protocol", guard="pipeline_waiting", variant="secondary"),
                    StageAction(
                        "Continue",
                        completes=True,
                        guard="not pipeline_running",
                        suggest_when="pipeline_complete",
                        variant="warning",
                    ),
                ),
                editor=CONTROL_LIVE_EDITOR,
                results=CONTROL_RESULTS,
                features=("fluidics",),
                pipeline=True,
                skippable=True,
                completion_gate="recording_confirmation",
                settings_options={
                    "sections": ("flow", "consumption"),
                    "main": RUN_MAIN_SETTINGS,
                    "pipeline_name": "Drop-Seq",
                    "pipeline_label": "Start Run",
                    "liquids": {"oil_l": "oil_l", "cells_m": "water_m", "beads_m": "water_m"},
                },
            ),
            Stage(
                "wash",
                "8. Post-process wash",
                action="run_protocol",
                description="Post-run wash and shutdown path.",
                instructions=("Run the wash protocol and confirm gated steps as prompted.",),
                instruction_cards=(
                    StageInstruction(
                        "Set wash volume and pressure duration, then follow each protocol prompt until shutdown is complete."
                    ),
                ),
                settings=WASH_SETTINGS,
                actions=(
                    StageAction(
                        "Start Wash",
                        "run_protocol",
                        guard="fluidics_connected",
                        active_when="pipeline_running",
                        kind="pipeline",
                    ),
                    StageAction("Pause", "pause_protocol", guard="pipeline_running", variant="secondary"),
                    StageAction("Proceed", "confirm_protocol", guard="pipeline_waiting", variant="warning"),
                    StageAction("Skip Step", "skip_protocol", guard="pipeline_waiting", variant="secondary"),
                    StageAction(
                        "Continue",
                        completes=True,
                        guard="not pipeline_running",
                        suggest_when="pipeline_complete",
                        variant="warning",
                    ),
                ),
                editor=CONTROL_LIVE_EDITOR,
                results=CONTROL_RESULTS,
                features=("fluidics",),
                pipeline=True,
                skippable=True,
                settings_options={
                    "main": WASH_MAIN_SETTINGS,
                    "pipeline_name": "Wash",
                    "pipeline_label": "Start Wash",
                    "liquids": {"oil_l": "ipa_l", "cells_m": "ipa_m", "beads_m": "ipa_m"},
                },
            ),
            Stage(
                "cleanup",
                "9. Cleanup",
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
