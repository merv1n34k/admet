from __future__ import annotations

from typing import Any

from admet.ui.presenter import SurfaceVM


def render_matrix(ui: Any, surface: SurfaceVM, runtime: Any) -> None:
    rows = runtime.surface_rows(surface)
    columns = _columns(surface, rows)
    ui.table(columns=columns, rows=rows, row_key="uid").classes("w-full").props(
        "dense flat wrap-cells"
    )


def _columns(surface: SurfaceVM, rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    names = tuple(surface.options.get("columns") or ())
    if not names:
        names = tuple(rows[0].keys()) if rows else ("status",)
    return [
        {
            "name": name,
            "label": name.replace("_", " ").title(),
            "field": name,
            "align": "left",
        }
        for name in names
    ]
