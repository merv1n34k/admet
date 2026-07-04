from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractScrollArea, QHeaderView, QTableWidget, QTableWidgetItem, QVBoxLayout

from admet.ui.presenter import SurfaceVM


def render_matrix_qt(layout: QVBoxLayout, surface: SurfaceVM, runtime: Any) -> None:
    rows = runtime.surface_rows(surface) if hasattr(runtime, "surface_rows") else []
    columns = _columns(surface, rows)
    table = QTableWidget(len(rows), len(columns))
    table.setHorizontalHeaderLabels([label for _name, label in columns])
    table.verticalHeader().hide()
    table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
    table.setShowGrid(True)
    for index, (name, _label) in enumerate(columns):
        mode = QHeaderView.ResizeMode.Stretch if index == 0 else QHeaderView.ResizeMode.ResizeToContents
        table.horizontalHeader().setSectionResizeMode(index, mode)
        for row_index, row in enumerate(rows):
            item = QTableWidgetItem(str(row.get(name, "")))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row_index, index, item)
    table.resizeRowsToContents()
    layout.addWidget(table)


def _columns(surface: SurfaceVM, rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    names = tuple(surface.options.get("columns") or ())
    if not names:
        names = tuple(rows[0].keys()) if rows else ("status",)
    return [(name, name.replace("_", " ").title()) for name in names]
