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
    )
)
