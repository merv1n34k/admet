from __future__ import annotations

from admet.core.engine import Param, ParamKind, ParamSchema


CELLPOSE_SETTINGS = ParamSchema(
    (
        Param("input_dir", "Input Directory", ParamKind.PATH, default="", required=True),
        Param("config_path", "Config Path", ParamKind.PATH, default=""),
        Param("px_to_um", "Pixels To Microns", ParamKind.FLOAT, default=1.14, minimum=0.0),
        Param("frame_limit", "Frame Limit", ParamKind.INTEGER, default=None),
        Param("detect_inclusions", "Detect Inclusions", ParamKind.BOOLEAN, default=True),
    )
)
