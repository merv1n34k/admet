from __future__ import annotations

from typing import Any

from admet.ui.presenter import SurfaceVM


def render_video_preview(ui: Any, surface: SurfaceVM, runtime: Any) -> None:
    html = runtime.surface_html(surface)
    if html:
        ui.html(html).classes("w-full")
        return
    with ui.column().classes("admet-surface-placeholder admet-video-placeholder"):
        ui.label(surface.title or "Video Preview").classes("text-sm font-medium")
        ui.label("Select a video row to preview frames and crop geometry.").classes(
            "text-xs text-gray-500"
        )
