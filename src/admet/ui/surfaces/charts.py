from __future__ import annotations

from typing import Any

from admet.ui.presenter import SurfaceVM


def render_charts(ui: Any, surface: SurfaceVM, runtime: Any) -> None:
    charts = runtime.surface_charts(surface)
    if not charts:
        with ui.column().classes("panel w-full p-3"):
            ui.label(surface.title or "Results").classes("section-title")
            ui.label("Stored raw runs will appear here after analysis.").classes("muted text-xs")
        return
    with ui.element("div").classes("comparison-grid w-full"):
        for chart in charts:
            with ui.column().classes("plot-card"):
                ui.echart(chart).classes("w-full h-64")
