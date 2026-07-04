from __future__ import annotations

from dataclasses import dataclass
import re

from admet.workflows.control_settings import (
    CAMERA_SETTINGS,
    CORRECTION_SETTINGS,
    FLUIGENT_SETTINGS,
    PROTOCOL_SETTINGS,
    RUN_SETTINGS,
    WASH_SETTINGS,
)

from .model import Stage, StageControl, StageSurface, Workflow


PIPELINE_STAGE_IDS = frozenset({"priming", "runs", "wash"})

CAMERA_AUTO_APPLY_PARAMS = frozenset(
    {
        "camera_width",
        "camera_height",
        "camera_offset_x",
        "camera_offset_y",
        "camera_binning_h",
        "camera_binning_v",
        "camera_exposure_us",
        "camera_gain",
        "camera_pixel_format",
        "camera_readout",
        "camera_framerate_enabled",
        "camera_framerate_hz",
        "camera_throughput_enabled",
        "camera_throughput_mbps",
        "camera_waterfall",
    }
)

CAMERA_MAIN_SETTINGS = (
    "camera_width",
    "camera_height",
    "camera_exposure_us",
    "camera_gain",
    "camera_pixel_format",
    "camera_readout",
)

FLUIDICS_MAIN_SETTINGS = (
    "simulated",
)

PROJECT_REQUIRED_ACTIONS = frozenset(
    {
        "refresh_cameras",
        "connect_camera",
        "disconnect_camera",
        "start_camera_live",
        "stop_camera_live",
        "apply_camera_settings",
        "start_recording",
    }
)

FLUIGENT_REQUIRED_ACTIONS = frozenset(
    {
        "apply_corrections",
        "set_channel_flow",
        "set_channel_pressure",
        "stop_channel",
        "set_channel_response",
        "start_recording",
        "run_protocol",
        "pause_protocol",
        "resume_protocol",
        "confirm_protocol",
        "skip_protocol",
        "wash",
    }
)

CAMERA_LIVE_REQUIRED_ACTIONS = frozenset()


@dataclass(frozen=True)
class ControlRuntime:
    project_ready: bool = False
    camera_connected: bool = False
    camera_live: bool = False
    fluidics_connected: bool = False
    simulated: bool = False
    fluigent_instrument_count: int = 0
    pipeline_state: str = "idle"
    pending_confirmation: str = ""


@dataclass(frozen=True)
class ControlCommandSpec:
    label: str
    command: str
    action: str | None = None
    off_action: str | None = None
    state_key: str = ""
    enabled: bool = True
    checked: bool = False
    toggle: bool = False
    control: StageControl | None = None


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
                "camera_waterfall",
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
            "title": "Display & Selection",
            "icon": "select_all",
            "params": (
                "camera_flip_x",
                "camera_flip_y",
                "camera_rotation",
                "camera_ruler_v",
                "camera_ruler_h",
                "camera_ruler_radial",
                "camera_selection_x",
                "camera_selection_y",
                "camera_selection_w",
                "camera_selection_h",
            ),
        },
        {
            "title": "Capture",
            "icon": "fiber_manual_record",
            "params": (
                "camera_image_prefix",
                "camera_video_fps",
                "camera_preview_off_recording",
            ),
        },
    ),
}


def create_control_workflow() -> Workflow:
    return Workflow(
        workflow_id="control",
        label="Acquisition",
        stages=(
            Stage(
                "scene",
                "1. Scene setup",
                description="Camera discovery, connection, preview, and acquisition geometry.",
                settings=CAMERA_SETTINGS,
                controls=(
                    StageControl("Scene Done", completes=True, variant="success"),
                ),
                surfaces=(
                    StageSurface(
                        "camera",
                        "Camera",
                        settings=CAMERA_SETTINGS,
                        controls=(
                            StageControl("Refresh", "refresh_cameras", variant="secondary"),
                            StageControl("Connect", "connect_camera"),
                            StageControl("Live", "start_camera_live"),
                            StageControl("Stop", "stop_camera_live", variant="danger"),
                        ),
                        options=CAMERA_SURFACE_OPTIONS,
                    ),
                ),
                show_settings=False,
            ),
            Stage(
                "fluigent",
                "2. Fluigent connect",
                action="connect_fluidics",
                description="SDK import, device enumeration, optional simulated hardware, and polling.",
                settings=FLUIGENT_SETTINGS,
                controls=(
                    StageControl("Connect", "connect_fluidics"),
                    StageControl("Disconnect", "disconnect_fluidics", variant="warning"),
                    StageControl("Continue", completes=True, variant="success"),
                ),
            ),
            Stage(
                "corrections",
                "3. Correction factors",
                description="Flow sensor calibration table and polynomial correction factors.",
                settings=CORRECTION_SETTINGS,
            ),
            Stage(
                "priming",
                "4. Priming protocol",
                action="run_protocol",
                description="Run the priming pipeline and confirm gated steps as prompted.",
                settings=PROTOCOL_SETTINGS,
            ),
            Stage(
                "runs",
                "5. Test runs",
                action="run_protocol",
                description="Recorded Drop-Seq or custom run protocol with CSV/video session output.",
                settings=RUN_SETTINGS,
            ),
            Stage(
                "wash",
                "6. Post-process wash",
                action="run_protocol",
                description="Post-run wash and shutdown path.",
                settings=WASH_SETTINGS,
            ),
        ),
    )


def stage_uses_camera(stage: Stage) -> bool:
    return any(surface.kind == "camera" for surface in stage.surfaces)


def stage_uses_fluidics(stage: Stage) -> bool:
    return stage.id != "scene"


def action_requires_camera_live(action: str) -> bool:
    return action in CAMERA_LIVE_REQUIRED_ACTIONS


def action_requires_project(action: str) -> bool:
    return action in PROJECT_REQUIRED_ACTIONS


def action_requires_fluigent(action: str) -> bool:
    return action in FLUIGENT_REQUIRED_ACTIONS


def action_enabled(action: str | None, runtime: ControlRuntime) -> bool:
    if action is None:
        return True
    if action_requires_camera_live(action) and not runtime.camera_live:
        return False
    if action == "connect_fluidics":
        if runtime.fluidics_connected:
            return False
        if runtime.simulated:
            return True
        return runtime.fluigent_instrument_count > 0
    if action == "disconnect_fluidics":
        return runtime.fluidics_connected
    if action_requires_fluigent(action) and not runtime.fluidics_connected:
        return False
    return True


def control_button_specs(stage: Stage, runtime: ControlRuntime) -> tuple[ControlCommandSpec, ...]:
    controls: list[ControlCommandSpec] = []
    if stage_uses_camera(stage):
        controls.extend(
            (
                ControlCommandSpec(
                    "Refresh",
                    "run_action",
                    action="refresh_cameras",
                    enabled=runtime.project_ready,
                ),
                ControlCommandSpec(
                    "Connect",
                    "run_action",
                    action="connect_camera",
                    enabled=runtime.project_ready and not runtime.camera_connected,
                ),
                ControlCommandSpec(
                    "Disconnect",
                    "run_action",
                    action="disconnect_camera",
                    enabled=runtime.project_ready and runtime.camera_connected,
                ),
                ControlCommandSpec(
                    "Live",
                    "toggle_action",
                    action="start_camera_live",
                    off_action="stop_camera_live",
                    state_key="camera_live",
                    enabled=runtime.project_ready and runtime.camera_connected,
                    checked=runtime.camera_live,
                    toggle=True,
                ),
            )
        )
    if stage.id in PIPELINE_STAGE_IDS:
        controls.extend(pipeline_button_specs(stage, runtime))
    for control in stage.controls or default_controls(stage):
        spec = stage_control_button_spec(stage, control, runtime)
        if spec is not None:
            controls.append(spec)
    controls.append(ControlCommandSpec("E-STOP", "emergency_stop"))
    return tuple(controls)


def pipeline_button_specs(stage: Stage, runtime: ControlRuntime) -> tuple[ControlCommandSpec, ...]:
    state = runtime.pipeline_state
    active = pipeline_active(runtime)
    can_start = runtime.fluidics_connected and not active
    confirmation = runtime.pending_confirmation
    return (
        ControlCommandSpec(pipeline_start_label(stage), "pipeline_start", enabled=can_start),
        ControlCommandSpec(
            "Pause" if state != "paused" else "Resume",
            "pipeline_pause_toggle",
            enabled=state in {"running", "paused"},
            checked=state == "paused",
            toggle=True,
        ),
        ControlCommandSpec("Stop", "pipeline_stop", enabled=active),
        ControlCommandSpec("Skip", "pipeline_skip", enabled=state == "running"),
        ControlCommandSpec(
            "Proceed",
            "pipeline_confirm",
            enabled=bool(confirmation) and state == "running",
        ),
    )


def stage_control_button_spec(
    stage: Stage,
    control: StageControl,
    runtime: ControlRuntime,
) -> ControlCommandSpec | None:
    action = control.action
    if action in {"stop_camera_live", "stop_recording", "stop_protocol", "resume_protocol"}:
        return None
    if control.completes and action is None:
        return None
    if action == "run_protocol":
        return ControlCommandSpec(
            short_control_label(control.label),
            "pipeline_toggle",
            action="run_protocol",
            enabled=action_enabled("run_protocol", runtime),
            checked=pipeline_active(runtime),
            toggle=True,
            control=control,
        )
    if action == "pause_protocol":
        return ControlCommandSpec(
            "Pause",
            "pipeline_pause_toggle",
            action="pause_protocol",
            enabled=action_enabled("pause_protocol", runtime),
            checked=runtime.pipeline_state == "paused",
            toggle=True,
            control=control,
        )
    return ControlCommandSpec(
        short_control_label(control.label),
        "stage_control",
        action=action,
        enabled=action_enabled(action, runtime),
        control=control,
    )


def default_controls(stage: Stage) -> tuple[StageControl, ...]:
    if stage.id == "corrections" or stage.id in PIPELINE_STAGE_IDS:
        return ()
    controls: list[StageControl] = []
    if stage.action:
        controls.append(StageControl("Run Stage", stage.action, completes=True))
    if stage.skippable:
        controls.append(StageControl("Skip", skippable=True, variant="secondary"))
    if not controls:
        controls.append(StageControl("Complete", completes=True, variant="success"))
    return tuple(controls)


def pipeline_active(runtime: ControlRuntime) -> bool:
    return runtime.pipeline_state in {"running", "paused", "stopping"}


def short_control_label(label: str) -> str:
    replacements = {
        "Disconnect": "Disconnect",
        "Continue": "Next",
        "Run Priming": "Prime",
        "Confirm Step": "Confirm",
        "Priming Done": "Done",
        "Run Protocol": "Run",
        "Stop Protocol": "Stop",
        "Runs Done": "Done",
        "Run Wash": "Wash",
        "Skip Wash": "Skip",
        "Workflow Done": "Done",
    }
    return replacements.get(label, label)


def pipeline_start_label(stage: Stage) -> str:
    if stage.id == "priming":
        return "Run Priming"
    if stage.id == "runs":
        return "Start Runs"
    if stage.id == "wash":
        return "Run Wash"
    return "Start"


def run_start_label(message: str) -> str:
    match = re.search(r"\bStart\s+(set\d{2}_rep\d{2})\b", message)
    return match.group(1) if match else ""


def run_complete_label(message: str) -> str:
    match = re.search(r"\b(set\d{2}_rep\d{2})\s+complete\b", message)
    return match.group(1) if match else ""
