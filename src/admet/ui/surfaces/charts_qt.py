from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout

from admet.ui.presenter import SurfaceVM


def render_charts_qt(layout: QVBoxLayout, _surface: SurfaceVM, runtime: Any) -> None:
    rows = runtime.result_rows() if hasattr(runtime, "result_rows") else []
    if not rows:
        label = QLabel("Stored raw runs will appear here after analysis.")
        label.setWordWrap(True)
        layout.addWidget(label)
        return
    columns = list(rows[0].keys())
    table = QTableWidget(len(rows), len(columns))
    table.setHorizontalHeaderLabels([column.title() for column in columns])
    for row_index, row in enumerate(rows):
        for column_index, column in enumerate(columns):
            table.setItem(row_index, column_index, QTableWidgetItem(str(row.get(column, ""))))
    table.resizeColumnsToContents()
    layout.addWidget(table)
