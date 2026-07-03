from admet.engines.control.settings import (
    CAMERA_SETTINGS,
    CORRECTION_SETTINGS,
    FLUIGENT_SETTINGS,
    PROTOCOL_SETTINGS,
    RUN_SETTINGS,
    WASH_SETTINGS,
)

from .model import Stage, StageControl, StageSurface, Workflow


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
