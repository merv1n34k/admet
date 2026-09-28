"""Table widgets shared by the control window and its planning sections."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QTableWidget, QWidget

from admet.ui.theme import Theme


class GridTable(QTableWidget):
    """A table whose separators stop at the edge of the content.

    Qt's own grid draws a line after every cell, the last row and column
    included, so the outer lines are drawn twice over -- once by the grid, once
    by the frame -- and being straight they cannot follow the frame's rounded
    corners. Painting only the lines between cells leaves the frame as the sole
    boundary, free to round.
    """

    def __init__(self, rows: int = 0, columns: int = 0, parent: QWidget | None = None) -> None:
        super().__init__(rows, columns, parent)
        self.setShowGrid(False)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self.rowCount() < 1 or self.columnCount() < 1:
            return
        painter = QPainter(self.viewport())
        painter.setPen(QPen(QColor(Theme.BORDER_COOL), 1))
        width = self.viewport().width()
        height = self.viewport().height()
        for column in range(self.columnCount() - 1):
            x = self.columnViewportPosition(column) + self.columnWidth(column) - 1
            if 0 <= x < width:
                painter.drawLine(x, 0, x, height)
        for row in range(self.rowCount() - 1):
            y = self.rowViewportPosition(row) + self.rowHeight(row) - 1
            if 0 <= y < height:
                painter.drawLine(0, y, width, y)
        painter.end()


def fit_table_height(table: QTableWidget, *, max_rows: int | None = None) -> None:
    """Size a table to its rows, so it never leaves empty space below the last one.

    The header is measured by what it asks for rather than by what it currently
    occupies: called before the table has been laid out, its present height is
    whatever Qt started it at, and a header of two lines would then have its last
    row cut off.
    """
    header = table.horizontalHeader()
    height = max(header.height(), header.sizeHint().height()) + table.frameWidth() * 2
    row_count = table.rowCount() if max_rows is None else min(table.rowCount(), max_rows)
    height += sum(table.rowHeight(row) for row in range(row_count))
    table.setFixedHeight(height)
