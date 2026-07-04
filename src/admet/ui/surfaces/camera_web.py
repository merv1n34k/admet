from __future__ import annotations

from typing import Any

from admet.ui.presenter import SurfaceVM


def render_camera(ui: Any, surface: SurfaceVM, runtime: Any) -> None:
    engine = getattr(getattr(runtime, "api", None), "engine", None)
    metadata = getattr(runtime, "last_metadata", {}) or {}
    live = bool(metadata.get("camera_live") or getattr(engine, "camera_live", False))
    connected = bool(metadata.get("camera_connected"))
    frame_shape = metadata.get("camera_frame_shape") or []
    with ui.column().classes("admet-video-placeholder"):
        ui.label(surface.title or "Camera").classes("text-sm font-medium")
        ui.label(
            f"connected={connected} live={live} frame={frame_shape or 'none'}"
        ).classes("admet-video-caption")
