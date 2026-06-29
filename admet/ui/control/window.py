from __future__ import annotations

import time
from typing import Any

import numpy as np
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from admet.core.api import AdmetAPI
from admet.core.schema import ParamKind
from admet.workflows import create_control_workflow
from admet.ui.control import theme as ui
from admet.ui.control.theme import Theme, text_qss


class ControlWindow(QMainWindow):
    def __init__(self, api: AdmetAPI) -> None:
        super().__init__()
        self.api = api
        self.workflow = create_control_workflow()
        self.workflow_state = self.workflow.initial_state()
        self.settings = api.settings.defaults()
        self.last_metadata: dict[str, Any] = {}
        self.stage_buttons = []
        self.toc_rows: dict[str, tuple[QLabel, QLabel, QProgressBar]] = {}
        self._last_frame_id = 0
        self._last_status_poll = 0.0
        self._qt_frame: np.ndarray | None = None

        self.setWindowTitle("admet control")
        self.resize(1520, 920)
        self.setMinimumSize(980, 680)

        self.stack = QStackedWidget()
        self.status = QLabel("Idle")
        self.status.setStyleSheet(text_qss("primary"))
        self._build_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(100)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(*ui.box_padding("panel"))
        root.setSpacing(ui.spacing("group"))

        root.addWidget(self._build_top_bar())

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(Theme.SPLITTER_HANDLE_WIDTH)
        split.addWidget(self.stack)
        split.addWidget(self._build_camera_pane())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([740, 760])
        root.addWidget(split, 1)

        self.stack.addWidget(self._build_scene_page())
        self.stack.addWidget(self._build_protocol_page())
        self.stack.addWidget(self._build_calibration_page())
        self._select_stage(0)

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(*ui.box_padding("none"))
        layout.setSpacing(ui.spacing("control"))

        title = QLabel("admet control")
        title.setStyleSheet(text_qss("default", font_size=18, bold=True))
        layout.addWidget(title)
        layout.addSpacing(ui.spacing("panel"))

        for index, name in enumerate(("Scene", "Fluigent", "Runs", "Wash", "Calibration")):
            button = ui.stage_button(name)
            button.clicked.connect(lambda checked=False, idx=index: self._select_stage(idx))
            self.stage_buttons.append(button)
            layout.addWidget(button)

        layout.addStretch()
        resume = ui.button("Resume", variant="success")
        resume.clicked.connect(lambda: self._run("resume_protocol"))
        skip = ui.button("Skip", variant="warning")
        skip.clicked.connect(lambda: self._run("skip_protocol"))
        estop = ui.button("E-STOP", variant="danger")
        estop.clicked.connect(self._emergency_stop)
        layout.addWidget(resume)
        layout.addWidget(skip)
        layout.addWidget(estop)
        layout.addWidget(self.status)
        return bar

    def _build_scene_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(*ui.box_padding("none"))
        layout.setSpacing(ui.spacing("group"))

        self.camera_combo = ui.combo_box()
        self.camera_refresh = ui.button("Refresh")
        self.camera_connect = ui.button("Connect", variant="success")
        self.camera_disconnect = ui.button("Disconnect", variant="danger")
        self.camera_live = ui.button("Live", variant="primary", checkable=True)
        self.camera_width = ui.int_box(minimum=16, maximum=10000, value=640, width=84)
        self.camera_height = ui.int_box(minimum=1, maximum=10000, value=480, width=84)
        self.camera_exposure = ui.double_box(
            minimum=1.0,
            maximum=1_000_000.0,
            value=150.0,
            step=10.0,
            suffix=" us",
            width=105,
        )
        self.camera_readout = ui.combo_box(("Fast", "Normal"), width=96)
        self.camera_apply = ui.button("Apply")

        self.camera_refresh.clicked.connect(self._refresh_cameras)
        self.camera_connect.clicked.connect(self._connect_camera)
        self.camera_disconnect.clicked.connect(lambda: self._run("disconnect_camera"))
        self.camera_live.clicked.connect(self._toggle_live)
        self.camera_apply.clicked.connect(self._apply_camera_settings)

        group, group_layout = ui.section("Scene Setup")
        group_layout.addWidget(
            ui.control_row("Camera", self.camera_combo, self.camera_refresh, label_width=80)
        )
        group_layout.addWidget(
            ui.button_row(
                self.camera_connect,
                self.camera_disconnect,
                self.camera_live,
                align="left",
            )
        )
        group_layout.addWidget(
            ui.field_row(
                QLabel("Width"),
                self.camera_width,
                QLabel("Height"),
                self.camera_height,
                QLabel("Exposure"),
                self.camera_exposure,
                QLabel("Readout"),
                self.camera_readout,
                self.camera_apply,
            )
        )
        layout.addWidget(group)

        advanced, advanced_layout = ui.section("Advanced Camera")
        self.camera_offset_x = ui.int_box(minimum=0, maximum=10000, value=0, step=16, width=84)
        self.camera_offset_y = ui.int_box(minimum=0, maximum=10000, value=0, step=16, width=84)
        self.camera_binning_h = ui.int_box(minimum=1, maximum=16, value=1, width=84)
        self.camera_binning_v = ui.int_box(minimum=1, maximum=16, value=1, width=84)
        self.camera_gain = ui.double_box(minimum=0.0, maximum=48.0, value=0.0, width=84)
        self.camera_pixel_format = ui.combo_box(("Mono8", "Mono10", "Mono10p"), width=105)
        advanced_layout.addWidget(
            ui.field_row(
                QLabel("Offset X"),
                self.camera_offset_x,
                QLabel("Offset Y"),
                self.camera_offset_y,
                QLabel("Bin H"),
                self.camera_binning_h,
                QLabel("Bin V"),
                self.camera_binning_v,
            )
        )
        advanced_layout.addWidget(
            ui.field_row(
                QLabel("Gain"),
                self.camera_gain,
                QLabel("Pixel"),
                self.camera_pixel_format,
            )
        )
        layout.addWidget(advanced)
        layout.addStretch()
        return page

    def _build_camera_pane(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(*ui.box_padding("none"))
        layout.setSpacing(ui.spacing("group"))

        header = ui.toolbar("Camera")
        self.camera_state = QLabel("Disconnected")
        self.camera_state.setStyleSheet(text_qss("muted", bold=True))
        header.layout().addWidget(self.camera_state)
        layout.addWidget(header)

        self.preview = QLabel("No camera frame")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(320)
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.preview.setStyleSheet(
            f"background: {Theme.BG_BLACK}; color: {Theme.TEXT_MUTED}; border-radius: {Theme.RADIUS}px;"
        )
        layout.addWidget(self.preview, 1)
        layout.addWidget(self._build_toc_panel())
        return pane

    def _build_toc_panel(self) -> QWidget:
        panel, layout = ui.section("Workflow")
        for stage in self.workflow.stages:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(*ui.box_padding("none"))
            row_layout.setSpacing(ui.spacing("control"))
            label = QLabel(stage.label)
            label.setMinimumWidth(150)
            progress = QProgressBar()
            progress.setRange(0, 1000)
            progress.setValue(0)
            progress.setTextVisible(False)
            state = QLabel("Pending")
            state.setFixedWidth(70)
            row_layout.addWidget(label)
            row_layout.addWidget(progress, 1)
            row_layout.addWidget(state)
            layout.addWidget(row)
            self.toc_rows[stage.id] = (label, state, progress)
        self._sync_toc()
        return panel

    def _build_protocol_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(*ui.box_padding("none"))
        layout.setSpacing(ui.spacing("group"))

        self.protocol_title = QLabel("Fluigent Setup")
        self.protocol_title.setStyleSheet(text_qss("default", font_size=16, bold=True))
        self.simulated = ui.check_box("Simulated", checked=bool(self.settings.get("simulated", False)))
        connect = ui.button("Connect", variant="success")
        disconnect = ui.button("Disconnect", variant="danger")
        verify = ui.button("Check", variant="primary")
        prime = ui.button("Run Priming", variant="primary")
        chip_done = ui.button("Chip Setup Done", variant="success")
        self.set_count = ui.int_box(minimum=1, maximum=1000, value=1, width=70)
        self.replicate_count = ui.int_box(minimum=1, maximum=1000, value=1, width=70)
        self.pipeline_name = ui.combo_box(self._pipeline_options(), width=128)
        self.tick_s = ui.double_box(minimum=0.001, maximum=10.0, value=0.2, decimals=3, width=82)
        start_runs = ui.button("Run Protocol", variant="success")
        start_recording = ui.button("Record", variant="primary")
        stop_recording = ui.button("Stop Record", variant="warning")
        wash = ui.button("Run Wash", variant="warning")
        pause = ui.button("Pause", variant="warning")
        resume = ui.button("Resume", variant="success")
        stop = ui.button("Stop", variant="danger")
        confirm = ui.button("Confirm", variant="primary")
        skip = ui.button("Skip", variant="warning")

        connect.clicked.connect(self._connect_fluigent)
        disconnect.clicked.connect(lambda: self._run("disconnect_fluidics"))
        verify.clicked.connect(lambda: self._run("verify_fluigent", {"simulated": self.simulated.isChecked()}))
        prime.clicked.connect(lambda: self._run("run_protocol", {"pipeline_name": "Priming", "tick_s": self.tick_s.value()}))
        chip_done.clicked.connect(self._complete_current_stage)
        start_runs.clicked.connect(self._run_protocol)
        start_recording.clicked.connect(lambda: self._run("start_recording", {"log_dir": "logs"}))
        stop_recording.clicked.connect(lambda: self._run("stop_recording"))
        wash.clicked.connect(lambda: self._run("wash", {"tick_s": self.tick_s.value()}))
        pause.clicked.connect(lambda: self._run("pause_protocol"))
        resume.clicked.connect(lambda: self._run("resume_protocol"))
        stop.clicked.connect(lambda: self._run("stop_protocol"))
        confirm.clicked.connect(lambda: self._run("confirm_protocol"))
        skip.clicked.connect(lambda: self._run("skip_protocol"))

        group, group_layout = ui.section("Protocol")
        group_layout.addWidget(self.protocol_title)
        group_layout.addWidget(ui.field_row(self.simulated, verify, connect, disconnect, prime, chip_done))
        group_layout.addWidget(
            ui.field_row(
                QLabel("Pipeline"),
                self.pipeline_name,
                QLabel("Tick"),
                self.tick_s,
                QLabel("Sets"),
                self.set_count,
                QLabel("Reps"),
                self.replicate_count,
            )
        )
        group_layout.addWidget(
            ui.button_row(
                start_recording,
                start_runs,
                wash,
                pause,
                resume,
                stop,
                confirm,
                skip,
                stop_recording,
                align="left",
            )
        )
        layout.addWidget(group)

        self.protocol_progress = QProgressBar()
        self.protocol_progress.setRange(0, 1000)
        self.protocol_progress.setValue(0)
        layout.addWidget(self.protocol_progress)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        return page

    def _build_calibration_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(*ui.box_padding("none"))
        layout.setSpacing(ui.spacing("group"))

        corrections, corrections_layout = ui.section("Correction Factors")
        self.correction_widgets: dict[str, Any] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(ui.spacing("control"))
        grid.setVerticalSpacing(ui.spacing("control"))
        for column, title in enumerate(("Channel", "Calibration", "a", "b", "c")):
            grid.addWidget(QLabel(title), 0, column)
        rows = (
            ("Oil", "oil"),
            ("Cells", "cells"),
            ("Beads", "beads"),
        )
        for row, (label, prefix) in enumerate(rows, start=1):
            grid.addWidget(QLabel(label), row, 0)
            calibration = self._choice_widget(f"{prefix}_calibration")
            a = ui.double_box(value=float(self.settings.get(f"{prefix}_scale_a", 1.0)), width=90)
            b = ui.double_box(value=float(self.settings.get(f"{prefix}_scale_b", 0.0)), width=90)
            c = ui.double_box(value=float(self.settings.get(f"{prefix}_scale_c", 0.0)), width=90)
            self.correction_widgets[f"{prefix}_calibration"] = calibration
            self.correction_widgets[f"{prefix}_scale_a"] = a
            self.correction_widgets[f"{prefix}_scale_b"] = b
            self.correction_widgets[f"{prefix}_scale_c"] = c
            grid.addWidget(calibration, row, 1)
            grid.addWidget(a, row, 2)
            grid.addWidget(b, row, 3)
            grid.addWidget(c, row, 4)
        corrections_layout.addLayout(grid)
        apply_corrections = ui.button("Apply", variant="primary")
        apply_corrections.clicked.connect(self._complete_current_stage)
        corrections_layout.addWidget(ui.button_row(apply_corrections, align="left"))
        layout.addWidget(corrections)

        table_group, table_layout = ui.section("Fluigent Dead Volume")
        self.calibration_table = QTableWidget(9, 7)
        self.calibration_table.setHorizontalHeaderLabels(
            ("Channel", "Rep", "Empty g", "Full g", "Net g", "Volume ul", "Relative")
        )
        channels = ("Oil", "Cells", "Beads")
        for row in range(9):
            self.calibration_table.setItem(row, 0, QTableWidgetItem(channels[row // 3]))
            self.calibration_table.setItem(row, 1, QTableWidgetItem(str(row % 3 + 1)))
        table_layout.addWidget(self.calibration_table)
        layout.addWidget(table_group, 1)
        return page

    def _select_stage(self, index: int) -> None:
        if index == 0:
            self.stack.setCurrentIndex(0)
        elif index == 4:
            self.stack.setCurrentIndex(2)
        else:
            self.stack.setCurrentIndex(1)
        for button_index, button in enumerate(self.stage_buttons):
            ui.apply_button_style(
                button,
                variant="primary" if button_index == index else "neutral",
                size="stage",
            )
            button.setChecked(button_index == index)
        if hasattr(self, "protocol_title"):
            self.protocol_title.setText(
                {
                    1: "Fluigent Setup",
                    2: "Run Tests",
                    3: "Post-process Wash",
                }.get(index, "Protocol")
            )

    def _refresh_cameras(self) -> None:
        result = self._run("refresh_cameras")
        if result is not None:
            self._populate_cameras(result.result_set.metadata)

    def _connect_camera(self) -> None:
        camera_index = self.camera_combo.currentData()
        if camera_index is None:
            camera_index = self.camera_combo.currentIndex()
        self._run("connect_camera", {"camera_index": int(max(0, camera_index))})

    def _toggle_live(self, checked: bool) -> None:
        self._run("start_camera_live" if checked else "stop_camera_live")

    def _apply_camera_settings(self) -> None:
        self._run(
            "apply_camera_settings",
            {
                "camera_width": self.camera_width.value(),
                "camera_height": self.camera_height.value(),
                "camera_offset_x": self.camera_offset_x.value(),
                "camera_offset_y": self.camera_offset_y.value(),
                "camera_binning_h": self.camera_binning_h.value(),
                "camera_binning_v": self.camera_binning_v.value(),
                "camera_exposure_us": self.camera_exposure.value(),
                "camera_gain": self.camera_gain.value(),
                "camera_pixel_format": self.camera_pixel_format.currentText(),
                "camera_readout": self.camera_readout.currentText(),
                "camera_framerate_enabled": False,
                "camera_framerate_hz": 30.0,
                "camera_throughput_enabled": False,
                "camera_throughput_mbps": 125.0,
                "camera_waterfall": False,
            },
        )

    def _connect_fluigent(self) -> None:
        self._run(
            "connect_fluidics",
            {"simulated": self.simulated.isChecked(), "start_polling": True},
        )

    def _run_protocol(self) -> None:
        self._run(
            "run_protocol",
            {"pipeline_name": self.pipeline_name.currentText(), "tick_s": self.tick_s.value()},
        )

    def _emergency_stop(self) -> None:
        for action in ("stop_protocol", "stop_recording", "stop_polling", "stop_camera_live"):
            self._run(action, raise_errors=False)
        self._set_status("Stopped", "danger")

    def _complete_current_stage(self) -> None:
        try:
            self.workflow_state = self.workflow.complete_current(self.workflow_state, confirmed=True)
        except Exception as exc:
            QMessageBox.warning(self, "Workflow", str(exc))
        self._sync_toc()

    def _run(self, action: str, settings: dict[str, Any] | None = None, *, raise_errors: bool = True):
        payload = dict(settings or {})
        try:
            result = self.api.run_action(action, payload)
        except Exception as exc:
            self._set_status(f"{action} failed", "danger")
            self._append_log(f"{action}: {type(exc).__name__}: {exc}")
            if raise_errors:
                QMessageBox.warning(self, "admet control", f"{action} failed:\n{exc}")
            return None
        self.last_metadata = result.result_set.metadata
        self._set_status(f"{action} ok", "success")
        if action != "camera_status":
            self._append_log(f"{action}: ok")
        self._apply_metadata(self.last_metadata)
        return result

    def _poll(self) -> None:
        self._draw_latest_frame()
        now = time.monotonic()
        if now - self._last_status_poll >= 0.5:
            self._last_status_poll = now
            self._run("camera_status", raise_errors=False)

    def _apply_metadata(self, metadata: dict[str, Any]) -> None:
        if "cameras" in metadata:
            self._populate_cameras(metadata)
        connected = bool(metadata.get("camera_connected", False))
        live = bool(metadata.get("camera_live", False))
        camera_text = "Live" if live else ("Connected" if connected else "Disconnected")
        fps = metadata.get("camera_fps", 0.0)
        if fps:
            camera_text = f"{camera_text}  {float(fps):.1f} fps"
        self.camera_state.setText(camera_text)
        self.camera_live.setChecked(live)
        self.protocol_progress.setValue(1000 if metadata.get("pipeline_state") == "running" else 0)
        self._sync_toc()

    def _populate_cameras(self, metadata: dict[str, Any]) -> None:
        current = self.camera_combo.currentData()
        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        for index, camera in enumerate(metadata.get("cameras") or ()):
            self.camera_combo.addItem(str(camera), index)
        if self.camera_combo.count() == 0:
            self.camera_combo.addItem("No cameras", 0)
        if current is not None:
            index = self.camera_combo.findData(current)
            if index >= 0:
                self.camera_combo.setCurrentIndex(index)
        self.camera_combo.blockSignals(False)

    def _draw_latest_frame(self) -> None:
        frame_getter = getattr(self.api.engine, "latest_camera_frame", None)
        if not callable(frame_getter):
            return
        frame = frame_getter()
        if frame is None:
            return
        frame_id = id(frame)
        if frame_id == self._last_frame_id:
            return
        pixmap = _pixmap_from_frame(frame)
        if pixmap.isNull():
            return
        self._last_frame_id = frame_id
        self._qt_frame = frame
        self.preview.setPixmap(
            pixmap.scaled(
                self.preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        )

    def _sync_toc(self) -> None:
        statuses = self.workflow_state.statuses
        for stage in self.workflow.stages:
            row = self.toc_rows.get(stage.id)
            if row is None:
                continue
            label, state, progress = row
            status = statuses.get(stage.id)
            text = str(status.value if status else "pending").title()
            state.setText(text)
            progress.setValue(1000 if text in {"Complete", "Skipped"} else 0)
            label.setStyleSheet(text_qss("primary" if text == "Active" else "default"))

    def _set_status(self, text: str, kind: str = "primary") -> None:
        self.status.setText(text)
        self.status.setStyleSheet(text_qss(kind))

    def _append_log(self, text: str) -> None:
        if hasattr(self, "log"):
            self.log.appendPlainText(text)

    def _pipeline_options(self) -> tuple[str, ...]:
        for param in self.api.settings.params:
            if param.name == "pipeline_name":
                return tuple(str(option.value) for option in param.options)
        return ("Priming",)

    def _choice_widget(self, name: str):
        for param in self.api.settings.params:
            if param.name == name and param.kind is ParamKind.CHOICE:
                widget = ui.combo_box([option.value for option in param.options], width=105)
                value = self.settings.get(name)
                index = widget.findText(str(value))
                if index >= 0:
                    widget.setCurrentIndex(index)
                return widget
        return ui.combo_box(width=105)


def _pixmap_from_frame(frame: np.ndarray) -> QPixmap:
    display = frame
    if display.dtype == np.uint16:
        display = (display >> 8).astype(np.uint8)
    if display.dtype != np.uint8:
        display = display.astype(np.uint8, copy=False)
    if not display.flags["C_CONTIGUOUS"]:
        display = np.ascontiguousarray(display)

    if display.ndim == 2:
        height, width = display.shape
        image = QImage(
            display.data,
            width,
            height,
            display.strides[0],
            QImage.Format.Format_Grayscale8,
        )
    elif display.ndim == 3 and display.shape[2] >= 3:
        height, width, _channels = display.shape
        image = QImage(
            display.data,
            width,
            height,
            display.strides[0],
            QImage.Format.Format_BGR888,
        )
    else:
        return QPixmap()
    return QPixmap.fromImage(image)
