from __future__ import annotations

from admet.core.engine import Param, ParamKind, ParamSchema


OPENCV_SETTINGS = ParamSchema(
    (
        Param("video_path", "Video Path", ParamKind.PATH, default="", required=True),
        Param("microns_per_pixel", "Microns Per Pixel", ParamKind.FLOAT, default=1.0),
        Param("fps", "FPS", ParamKind.FLOAT, default=0.0, minimum=0.0),
        Param("start_frame", "Start Frame", ParamKind.INTEGER, default=0, minimum=0),
        Param("end_frame", "End Frame", ParamKind.INTEGER, default=None, minimum=0),
        Param("roi_x", "ROI X", ParamKind.INTEGER, default=0, minimum=0),
        Param("roi_y", "ROI Y", ParamKind.INTEGER, default=0, minimum=0),
        Param("roi_width", "ROI Width", ParamKind.INTEGER, default=0, minimum=0),
        Param("roi_height", "ROI Height", ParamKind.INTEGER, default=0, minimum=0),
        Param("k_sigma", "Threshold, x noise", ParamKind.FLOAT, default=4.0, minimum=1.0,
              description="A pixel is droplet when darker than the background by this many times the noise"),
        Param("satellite_px", "Satellite Size", ParamKind.INTEGER, default=13, minimum=3,
              description="Shapes narrower than this many px are satellites, not droplets"),
        Param("min_area", "Min Area", ParamKind.FLOAT, default=800.0, minimum=0.0,
              description="Smallest droplet area kept, px²"),
        Param("skip_left_pct", "Skip Left", ParamKind.FLOAT, default=0.0, minimum=0.0, maximum=100.0,
              description="% of the width left out on the left, where droplets form"),
        Param("skip_right_pct", "Skip Right", ParamKind.FLOAT, default=0.0, minimum=0.0, maximum=100.0,
              description="% of the width left out on the right"),
        Param("gate_pct", "Gate", ParamKind.FLOAT, default=50.0, minimum=0.0, maximum=100.0,
              description="Where droplets are counted, % of the width; every droplet must pass it once"),
    )
)
