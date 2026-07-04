from __future__ import annotations

from collections.abc import Callable
from typing import Any

from admet.ui.presenter import SurfaceVM

from .camera_web import render_camera
from .charts import render_charts
from .charts_qt import render_charts_qt
from .camera_qt import render_camera_qt
from .matrix import render_matrix
from .matrix_qt import render_matrix_qt
from .video_preview import render_video_preview
from .video_preview_qt import render_video_preview_qt

SurfaceRenderer = Callable[[Any, SurfaceVM, Any], None]
QtSurfaceRenderer = Callable[[Any, SurfaceVM, Any], None]

NICEGUI_SURFACES: dict[str, SurfaceRenderer] = {
    "charts": render_charts,
    "matrix": render_matrix,
    "video_preview": render_video_preview,
    "camera": render_camera,
}

QT_SURFACES: dict[str, QtSurfaceRenderer] = {
    "camera": render_camera_qt,
    "charts": render_charts_qt,
    "matrix": render_matrix_qt,
    "video_preview": render_video_preview_qt,
}


def render_nicegui_surface(ui: Any, surface: SurfaceVM, runtime: Any) -> None:
    renderer = NICEGUI_SURFACES.get(surface.kind, render_unknown)
    renderer(ui, surface, runtime)


def render_unknown(ui: Any, surface: SurfaceVM, runtime: Any) -> None:
    with ui.column().classes("admet-surface-placeholder"):
        ui.label(surface.title or surface.kind).classes("text-sm font-medium")
        ui.label(f"No renderer registered for {surface.kind!r}.").classes("text-xs text-gray-500")


def render_qt_surface(layout: Any, surface: SurfaceVM, runtime: Any) -> None:
    renderer = QT_SURFACES.get(surface.kind, render_unknown_qt)
    renderer(layout, surface, runtime)


def render_unknown_qt(layout: Any, surface: SurfaceVM, _runtime: Any) -> None:
    from PySide6.QtWidgets import QLabel

    label = QLabel(f"No renderer registered for {surface.kind!r}.")
    label.setWordWrap(True)
    layout.addWidget(label)
