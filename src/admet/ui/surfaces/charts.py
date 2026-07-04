from __future__ import annotations

from typing import Any

from admet.ui.presenter import SurfaceVM


def render_charts(ui: Any, surface: SurfaceVM, runtime: Any) -> None:
    charts = runtime.surface_charts(surface)
    if not charts:
        with ui.column().classes("admet-surface-placeholder"):
            ui.label(surface.title or "Results").classes("text-sm font-medium")
            ui.label("Stored raw runs will appear here after analysis.").classes(
                "text-xs text-gray-500"
            )
        return
    with ui.grid(columns=2).classes("w-full gap-3"):
        for chart in charts:
            ui.echart(chart).classes("w-full h-64 admet-chart")
