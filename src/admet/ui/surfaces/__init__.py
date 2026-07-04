from __future__ import annotations

from collections.abc import Callable
from typing import Any

from admet.ui.presenter import SurfaceVM

from .charts import render_charts
from .matrix import render_matrix
from .video_preview import render_video_preview

SurfaceRenderer = Callable[[Any, SurfaceVM, Any], None]

NICEGUI_SURFACES: dict[str, SurfaceRenderer] = {
    "charts": render_charts,
    "matrix": render_matrix,
    "video_preview": render_video_preview,
}


def render_nicegui_surface(ui: Any, surface: SurfaceVM, runtime: Any) -> None:
    renderer = NICEGUI_SURFACES.get(surface.kind, render_unknown)
    renderer(ui, surface, runtime)


def render_unknown(ui: Any, surface: SurfaceVM, runtime: Any) -> None:
    with ui.column().classes("admet-surface-placeholder"):
        ui.label(surface.title or surface.kind).classes("text-sm font-medium")
        ui.label(f"No renderer registered for {surface.kind!r}.").classes("text-xs text-gray-500")
