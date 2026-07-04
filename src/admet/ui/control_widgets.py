from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNEL_LABELS
from admet.ui import theme as ui
from admet.ui.theme import Theme


PREVIEW_MIN_HEIGHT = 280
PREVIEW_MAX_HEIGHT = 520

VIDEO_TABLE_COLUMNS = (
    ("video", "Video"),
    ("acquisition_fps", "Acq FPS"),
    ("dimensions", "Dimensions"),
    ("converted_fps", "Converted FPS"),
    ("frames", "Frames"),
    ("duration", "Duration"),
)


class ChannelControlPanel(QFrame):
    def __init__(
        self,
        channels: list[Any],
        *,
        on_flow: Callable[[int, float], None],
        on_pressure: Callable[[int, float], None],
        on_stop: Callable[[int], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("InlinePanel")
        self._rows: list[ChannelControlRow] = []
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        for index, _channel in enumerate(channels[: len(FLUIDIC_CHANNEL_LABELS)]):
            row = ChannelControlRow(
                index,
                FLUIDIC_CHANNEL_LABELS[index],
                on_flow=on_flow,
                on_pressure=on_pressure,
                on_stop=on_stop,
            )
            self._rows.append(row)
            root.addWidget(row, 1)
        self.update_modes(channels)

    def update_modes(self, channels: list[Any]) -> None:
        for index, row in enumerate(self._rows):
            if index >= len(channels):
                row.set_status("missing")
                continue
            channel = channels[index]
            mode = getattr(channel, "mode", "off")
            owner = getattr(channel, "owner", "user")
            row.set_status(f"{mode} / {owner}")

    def update_from_snapshot(self, snapshot: Any) -> None:
        for index, row in enumerate(self._rows):
            row.update_values(
                _safe_list_value(snapshot.pressures, index),
                _safe_list_value(snapshot.flows, index),
                _safe_list_value(snapshot.volumes_ul, index),
                _safe_list_value(snapshot.stability, index, default=False),
            )


class ChannelControlRow(QWidget):
    def __init__(
        self,
        index: int,
        label: str,
        *,
        on_flow: Callable[[int, float], None],
        on_pressure: Callable[[int, float], None],
        on_stop: Callable[[int], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._index = index
        self.setObjectName("ChannelCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        name = QLabel(label)
        name.setObjectName("ChannelName")
        header.addWidget(name)
        header.addStretch()
        self.status = QLabel("off / user")
        self.status.setObjectName("MutedText")
        header.addWidget(self.status)
        layout.addLayout(header)

        self.live = QLabel("0.00 mbar | 0.000 uL/min | 0.000 uL | unstable")
        self.live.setObjectName("MutedText")
        layout.addWidget(self.live)

        flow_row = QHBoxLayout()
        flow_row.setContentsMargins(0, 0, 0, 0)
        flow_row.setSpacing(6)
        flow_label = QLabel("Flow")
        flow_label.setObjectName("FieldLabel")
        flow_row.addWidget(flow_label)
        self.flow = _small_double_box(0.0, 5000.0, " uL/min")
        self.flow.lineEdit().returnPressed.connect(lambda: on_flow(self._index, self.flow.value()))
        flow_row.addWidget(self.flow, 1)
        layout.addLayout(flow_row)

        pressure_row = QHBoxLayout()
        pressure_row.setContentsMargins(0, 0, 0, 0)
        pressure_row.setSpacing(6)
        pressure_label = QLabel("Pressure")
        pressure_label.setObjectName("FieldLabel")
        pressure_row.addWidget(pressure_label)
        self.pressure = _small_double_box(0.0, 2000.0, " mbar")
        self.pressure.lineEdit().returnPressed.connect(lambda: on_pressure(self._index, self.pressure.value()))
        pressure_row.addWidget(self.pressure, 1)
        layout.addLayout(pressure_row)

        stop_button = QPushButton("Stop")
        stop_button.clicked.connect(lambda: on_stop(self._index))
        layout.addWidget(stop_button)

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def update_values(self, pressure: float, flow: float, volume: float, stable: bool) -> None:
        state = "stable" if stable else "unstable"
        self.live.setText(f"{pressure:.2f} mbar | {flow:.3f} uL/min | {volume:.3f} uL | {state}")


class FluidicsMonitorTable(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("InlinePanel")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        self.status = QLabel("CSV: not recording")
        self.status.setObjectName("MutedText")
        root.addWidget(self.status)

        self.table = QTableWidget(0, 9)
        self.table.setObjectName("RawConfigTable")
        self.table.setHorizontalHeaderLabels(
            ("Channel", "Pressure", "Flow", "Mean", "Std", "Min", "Max", "Volume", "Stable")
        )
        self.table.verticalHeader().hide()
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        self.table.setShowGrid(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for column in range(1, self.table.columnCount()):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table)
        self.setup_channels(0)

    def setup_channels(self, channel_count: int) -> None:
        count = min(channel_count, len(FLUIDIC_CHANNEL_LABELS))
        self.table.setRowCount(count)
        for row in range(count):
            self._set_item(row, 0, FLUIDIC_CHANNEL_LABELS[row])
        self.table.resizeRowsToContents()
        fit_table_height(self.table)

    def update_from_snapshot(self, snapshot: Any) -> None:
        channel_count = min(
            len(getattr(snapshot, "flows", [])),
            len(FLUIDIC_CHANNEL_LABELS),
        )
        if self.table.rowCount() != channel_count:
            self.setup_channels(channel_count)
        for row in range(channel_count):
            pressure = _safe_list_value(snapshot.pressures, row)
            flow = _safe_list_value(snapshot.flows, row)
            stats = _safe_list_value(snapshot.flow_stats, row, default=None)
            volume = _safe_list_value(snapshot.volumes_ul, row)
            stable = _safe_list_value(snapshot.stability, row, default=False)
            self._set_item(row, 1, f"{pressure:.2f} mbar")
            self._set_item(row, 2, f"{flow:.3f} uL/min")
            self._set_item(row, 3, f"{getattr(stats, 'mean', 0.0):.3f}")
            self._set_item(row, 4, f"{getattr(stats, 'std', 0.0):.3f}")
            self._set_item(row, 5, f"{getattr(stats, 'min', 0.0):.3f}")
            self._set_item(row, 6, f"{getattr(stats, 'max', 0.0):.3f}")
            self._set_item(row, 7, f"{volume:.3f} uL")
            self._set_item(row, 8, "yes" if stable else "no")
        self.table.resizeRowsToContents()
        fit_table_height(self.table)

    def update_csv_status(self, filepath: str | None, row_count: int) -> None:
        if filepath:
            self.status.setText(f"CSV: {Path(filepath).name} | {row_count} rows")
        else:
            self.status.setText("CSV: not recording")

    def _set_item(self, row: int, column: int, text: str) -> None:
        item = self.table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, column, item)
        item.setText(text)


_MAX_PLOT_SAMPLES = 6000
_MAX_RENDERED_PLOT_POINTS = 1200
_PLOT_REFRESH_INTERVAL_S = 0.15
_VISIBLE_PLOT_WINDOW_S = 60.0
_PLOT_COLORS = (Theme.ACCENT, Theme.DANGER_HOVER, Theme.SUCCESS_HOVER)
_PLOT_LABELS = FLUIDIC_CHANNEL_LABELS


class LivePlot(QWidget):
    def __init__(self, title: str, y_unit: str, parent: QWidget | None = None):
        super().__init__(parent)
        import pyqtgraph as pg

        pg.setConfigOptions(antialias=False, background=Theme.BG_DARK, foreground=Theme.TEXT_MUTED)
        self._pg = pg
        self._times: deque[float] = deque(maxlen=_MAX_PLOT_SAMPLES)
        self._series: list[deque[float]] = [
            deque(maxlen=_MAX_PLOT_SAMPLES) for _ in range(3)
        ]
        self._curves = []
        self._auto_scroll = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._plot = pg.PlotWidget(title=title, labels={"left": y_unit, "bottom": "s"})
        self._plot.setBackground(Theme.BG_DARK)
        self._plot.showGrid(x=True, y=True, alpha=0.15)
        self._plot.setMouseEnabled(x=True, y=False)
        self._plot.enableAutoRange(axis="y")
        self._plot.setAutoVisible(y=True)
        self._plot.setXRange(0, _VISIBLE_PLOT_WINDOW_S, padding=0)
        self._plot.scene().sigMouseClicked.connect(self._on_click)
        self._plot.wheelEvent = self._wheel_event  # type: ignore[method-assign]
        self._plot.mouseDragEvent = self._mouse_drag_event  # type: ignore[method-assign]

        for index, label in enumerate(_PLOT_LABELS):
            curve = self._plot.plot(
                [],
                [],
                pen=pg.mkPen(_PLOT_COLORS[index], width=1.5),
                name=label,
                downsample=1,
                downsampleMethod="peak",
                clipToView=True,
            )
            self._curves.append(curve)
        layout.addWidget(self._plot)

    def ingest(self, t: float, values: list[float]) -> None:
        self._times.append(t)
        for index in range(3):
            self._series[index].append(values[index] if index < len(values) else 0.0)

    def refresh(self, bin_size: float = 0.0) -> None:
        if not self._times:
            return
        t_arr = np.asarray(self._times, dtype=float)
        t_arr, visible_slice = self._visible_time_slice(t_arr)
        for index, curve in enumerate(self._curves):
            y_arr = np.asarray(self._series[index], dtype=float)[visible_slice]
            t_out, y_out = _bin_arrays(t_arr, y_arr, bin_size)
            t_out, y_out = _limit_plot_points(t_out, y_out, _MAX_RENDERED_PLOT_POINTS)
            curve.setData(t_out, y_out)
        if self._auto_scroll:
            self._update_x_range()

    def clear_data(self) -> None:
        self._times.clear()
        for series in self._series:
            series.clear()
        for curve in self._curves:
            curve.setData([], [])
        self._auto_scroll = True
        self._plot.setXRange(0, _VISIBLE_PLOT_WINDOW_S, padding=0)

    def _on_click(self, event) -> None:
        if event.double():
            self._auto_scroll = True
            self._update_x_range()

    def _update_x_range(self) -> None:
        if self._times:
            x_max = self._times[-1]
            view_range = self._plot.viewRange()
            visible = view_range[0][1] - view_range[0][0]
            self._plot.setXRange(x_max - visible, x_max, padding=0)

    def _visible_time_slice(self, t_arr: np.ndarray) -> tuple[np.ndarray, slice]:
        if len(t_arr) <= _MAX_RENDERED_PLOT_POINTS:
            return t_arr, slice(None)
        if self._auto_scroll:
            x_max = t_arr[-1]
            x_min = x_max - _VISIBLE_PLOT_WINDOW_S
        else:
            view_range = self._plot.viewRange()
            x_min, x_max = view_range[0]
        start = int(np.searchsorted(t_arr, x_min, side="left"))
        stop = int(np.searchsorted(t_arr, x_max, side="right"))
        if stop <= start:
            return t_arr[-_MAX_RENDERED_PLOT_POINTS:], slice(-_MAX_RENDERED_PLOT_POINTS, None)
        return t_arr[start:stop], slice(start, stop)

    def _wheel_event(self, event) -> None:
        self._auto_scroll = False
        self._pg.PlotWidget.wheelEvent(self._plot, event)

    def _mouse_drag_event(self, event) -> None:
        self._auto_scroll = False
        self._pg.PlotWidget.mouseDragEvent(self._plot, event)


class PlotPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._last_refresh_at = 0.0
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(ui.spacing("control"))

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(Theme.SPACE_1, 0, Theme.SPACE_1, 0)
        toolbar.setSpacing(ui.spacing("control"))
        toolbar.addWidget(QLabel("Bin:"))
        self._bin_spin = QDoubleSpinBox()
        self._bin_spin.setRange(0.0, 10.0)
        self._bin_spin.setSingleStep(0.1)
        self._bin_spin.setDecimals(2)
        self._bin_spin.setSuffix(" s")
        self._bin_spin.setSpecialValueText("off")
        self._bin_spin.valueChanged.connect(lambda _value: self.refresh())
        toolbar.addWidget(self._bin_spin)
        toolbar.addStretch()
        root.addLayout(toolbar)

        plots = QVBoxLayout()
        plots.setContentsMargins(0, 0, 0, 0)
        plots.setSpacing(ui.spacing("control"))
        self._pressure = LivePlot("Pressure", "mbar")
        plots.addWidget(self._pressure)
        self._flow = LivePlot("Flow", "uL/min")
        plots.addWidget(self._flow)
        root.addLayout(plots, stretch=1)

    def ingest_from_snapshot(self, snapshot) -> None:
        self._pressure.ingest(snapshot.elapsed_s, snapshot.pressures)
        self._flow.ingest(snapshot.elapsed_s, snapshot.flows)

    def update_from_snapshot(self, snapshot) -> None:
        self.ingest_from_snapshot(snapshot)
        now = time.monotonic()
        if now - self._last_refresh_at < _PLOT_REFRESH_INTERVAL_S:
            return
        self.refresh()

    def refresh(self) -> None:
        self._last_refresh_at = time.monotonic()
        bin_size = self._bin_spin.value()
        self._pressure.refresh(bin_size=bin_size)
        self._flow.refresh(bin_size=bin_size)

    def clear(self) -> None:
        self._pressure.clear_data()
        self._flow.clear_data()


class NotificationCard(QFrame):
    closed = Signal()

    def __init__(
        self,
        parent: QWidget,
        text: str,
        kind: str,
        *,
        on_confirm: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_confirm = on_confirm
        self.setObjectName("NotificationCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        self.setMinimumWidth(220)
        self.setMaximumWidth(580)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(10)

        label = QLabel(text)
        label.setObjectName("NotificationText")
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.text_label = label
        layout.addWidget(label)

        if on_confirm is not None:
            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(0, 0, 0, 0)
            actions_layout.setSpacing(8)
            actions_layout.addStretch()

            cancel = QPushButton("Cancel")
            cancel.setObjectName("NotificationButton")
            cancel.clicked.connect(self.close)
            actions_layout.addWidget(cancel)

            confirm = QPushButton("Confirm")
            confirm.setObjectName("NotificationButton")
            confirm.clicked.connect(self._confirm)
            actions_layout.addWidget(confirm)
            layout.addWidget(actions)

        self.setStyleSheet(_notification_qss(kind))

    def fit_to_parent(self, available_width: int, available_height: int) -> None:
        width = min(580, max(220, available_width))
        self.setFixedWidth(width)
        label_width = max(120, width - 38)
        self.text_label.setFixedWidth(label_width)
        if self.text_label.hasHeightForWidth():
            self.text_label.setMinimumHeight(self.text_label.heightForWidth(label_width))
        layout = self.layout()
        if layout is not None:
            layout.activate()
        self.adjustSize()
        self.setFixedHeight(min(self.sizeHint().height(), available_height))

    def _confirm(self) -> None:
        callback = self._on_confirm
        self.close()
        if callback is not None:
            callback()

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)


class PreviewDisplay(QWidget):
    frame_painted = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.current_frame: np.ndarray | None = None
        self.frame_rect = QRect()
        self.message = "No camera frame"
        self._aspect = 480 / 640

    def set_frame(self, frame: np.ndarray | None) -> bool:
        if frame is None:
            return True
        if frame.dtype == np.uint16:
            frame = (frame >> 8).astype(np.uint8)
        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)
        self.current_frame = frame
        height, width = frame.shape[:2]
        if width > 0 and height > 0:
            self._aspect = height / width
            self.updateGeometry()
        self.message = ""
        self.update()
        return True

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return max(PREVIEW_MIN_HEIGHT, min(PREVIEW_MAX_HEIGHT, int(width * self._aspect)))

    def sizeHint(self) -> QSize:
        return QSize(760, self.heightForWidth(760))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)

        if self.message:
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.message)
            return

        if self.current_frame is None:
            return

        height, width = self.current_frame.shape[:2]
        if self.current_frame.ndim == 2:
            image = QImage(
                self.current_frame.data,
                width,
                height,
                width,
                QImage.Format.Format_Grayscale8,
            )
        elif self.current_frame.ndim == 3 and self.current_frame.shape[2] >= 3:
            image = QImage(
                self.current_frame.data,
                width,
                height,
                width * 3,
                QImage.Format.Format_RGB888,
            )
        else:
            return

        widget_rect = self.rect()
        scale_x = widget_rect.width() / width if width > 0 else 1
        scale_y = widget_rect.height() / height if height > 0 else 1
        scale = min(scale_x, scale_y)
        final_width = int(width * scale)
        final_height = int(height * scale)
        x = (widget_rect.width() - final_width) // 2
        y = (widget_rect.height() - final_height) // 2
        self.frame_rect = QRect(x, y, final_width, final_height)

        scaled = image.scaled(
            self.frame_rect.width(),
            self.frame_rect.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        painter.drawImage(self.frame_rect, scaled)
        self.frame_painted.emit()


def video_table(rows: list[dict[str, str]]) -> QTableWidget:
    table = QTableWidget(len(rows), len(VIDEO_TABLE_COLUMNS))
    table.setObjectName("RawConfigTable")
    table.setHorizontalHeaderLabels(tuple(label for _key, label in VIDEO_TABLE_COLUMNS))
    table.verticalHeader().hide()
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for column in range(1, len(VIDEO_TABLE_COLUMNS)):
        table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
    table.setShowGrid(True)
    for row_index, row in enumerate(rows):
        for column_index, (key, _label) in enumerate(VIDEO_TABLE_COLUMNS):
            item = QTableWidgetItem(row.get(key, ""))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row_index, column_index, item)
    table.resizeRowsToContents()
    fit_table_height(table)
    return table


def fit_table_height(table: QTableWidget, *, max_rows: int | None = None) -> None:
    height = table.horizontalHeader().height() + table.frameWidth() * 2
    row_count = table.rowCount() if max_rows is None else min(table.rowCount(), max_rows)
    height += sum(table.rowHeight(row) for row in range(row_count))
    table.setFixedHeight(height)


def widget_has_focus(widget: QWidget) -> bool:
    focus = QApplication.focusWidget()
    return focus is widget or bool(focus is not None and widget.isAncestorOf(focus))


def _small_double_box(minimum: float, maximum: float, suffix: str) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setDecimals(3)
    box.setSingleStep(1.0)
    box.setSuffix(suffix)
    box.setMaximumWidth(150)
    return box


def _safe_list_value(values: Any, index: int, *, default: Any = 0.0) -> Any:
    try:
        return values[index]
    except (IndexError, TypeError):
        return default


def _bin_arrays(x: np.ndarray, y: np.ndarray, bin_size: float) -> tuple[np.ndarray, np.ndarray]:
    if bin_size <= 0 or len(x) < 2:
        return x, y
    bin_index = ((x - x[0]) / bin_size).astype(np.intp)
    counts = np.bincount(bin_index)
    mask = counts > 0
    x_binned = np.bincount(bin_index, weights=x)[mask] / counts[mask]
    y_binned = np.bincount(bin_index, weights=y)[mask] / counts[mask]
    return x_binned, y_binned


def _limit_plot_points(x: np.ndarray, y: np.ndarray, limit: int) -> tuple[np.ndarray, np.ndarray]:
    if limit <= 0 or len(x) <= limit:
        return x, y
    step = max(1, int(np.ceil(len(x) / limit)))
    return x[::step], y[::step]


def _notification_qss(kind: str) -> str:
    colors = {
        "success": Theme.SUCCESS,
        "warning": Theme.WARNING,
        "danger": Theme.DANGER,
        "error": Theme.DANGER,
        "primary": Theme.ACCENT,
    }
    accent = colors.get(kind, Theme.ACCENT)
    return f"""
QFrame#NotificationCard {{
    background: {Theme.BG_CONTROL};
    border: 1px solid {accent};
    border-left: 5px solid {accent};
    border-radius: 2px;
}}
QLabel#NotificationText {{
    background: transparent;
    color: {Theme.TEXT_WHITE};
    font-size: 14px;
    font-weight: 600;
}}
QPushButton#NotificationButton {{
    background: {Theme.BG_CONTROL};
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: 2px;
    color: {Theme.TEXT_WHITE};
    min-height: 28px;
    max-height: 28px;
    padding: 0 14px;
    font-weight: 600;
}}
QPushButton#NotificationButton:hover {{
    background: {Theme.BG_CONTROL_HOVER};
}}
QPushButton#NotificationButton:pressed {{
    background: {Theme.BG_CONTROL_PRESSED};
}}
"""
