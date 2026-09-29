from __future__ import annotations

import math
import re
import signal
import sys
import time
import uuid
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty
from typing import Any, Callable

import numpy as np
from PySide6.QtCore import QEvent, QLocale, QObject, QRect, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QGraphicsView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QFileDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QComboBox,
    QScrollArea,
    QDoubleSpinBox,
    QSizePolicy,
    QLineEdit,
    QInputDialog,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from admet.ui.backend import DesktopBackend as AdmetAPI
from admet.ui.tasks import Tasks
from admet.core.discovery import discover_projects, project_ref_label, projects_root
from admet.core.run import RunJob, RunResult
from admet.core.project import ProjectStore
from admet.core.engine import Param, ParamKind
from admet.core.session import session_path
from admet.workflows import Stage, StageControl, StageStatus
from admet.engines.acquisition.fluidics.config import (
    FLUIDIC_CHANNEL_LABELS,
    FLUIDIC_CHANNEL_UNITS,
    FLUIDIC_CHANNELS,
)
from admet.engines.acquisition.settings import CORRECTION_PARAM_NAMES, LIQUID_PROFILE_PARAM_NAMES
from admet.engines.acquisition.fluidics.liquids import profile_by_id
from admet.ui.preflight import PreflightPanel
from admet.ui.tables import GridTable, fit_table_height
from admet.workflows.check_history import (
    CHECK_INTERVAL_DAYS,
    CheckRecord,
    discover_checks,
    latest_check,
    load_check,
)
from admet.workflows.preflight import (
    CHECK_COMPLETE,
    CHECK_DISPENSE,
    CHECK_FLOW,
    CHECK_PARTIAL,
    CHECK_STARTED,
)
from admet.ui import theme as ui
from admet.ui.data import (
    VIDEO_TABLE_COLUMNS,
    prefer_video_row,
    recording_video_key,
    video_row,
)
from admet.ui.theme import STATUS_COLORS, Theme
from admet.ui.window import (
    connected_devices,
    log_state,
    panel_specs,
    structure_changed,
    structure_signature,
)
from admet.ui.workflow_view import (
    action_button_state,
    active_when,
    has_feature,
    guard_enabled,
    instruction_text,
    PIPELINE_FINISHED,
    pipeline_is_running,
    stage_controls,
    toc_row_states,
)
from admet.workflows import create_control_workflow


def run_control_app(api: AdmetAPI, argv: list[str] | None = None) -> int:
    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(argv if argv is not None else sys.argv[:1])
    app.setStyleSheet(ui.stylesheet())

    window = ControlWindow(api)
    window.show()
    if owns_app:
        interrupted = False
        previous_sigint = signal.getsignal(signal.SIGINT)

        def handle_sigint(_signum, _frame) -> None:
            nonlocal interrupted
            interrupted = True
            QTimer.singleShot(0, window.close)

        signal_timer = QTimer()
        signal_timer.timeout.connect(lambda: None)
        signal_timer.start(100)
        signal.signal(signal.SIGINT, handle_sigint)
        try:
            return_code = app.exec()
        except KeyboardInterrupt:
            interrupted = True
            window.close()
            return_code = 0
        finally:
            signal_timer.stop()
            signal.signal(signal.SIGINT, previous_sigint)
            if not window._shutdown_complete:
                api.shutdown()
        return 0 if interrupted else return_code
    return 0


CAMERA_AUTO_APPLY_PARAMS = {
    "camera_width",
    "camera_height",
    "camera_offset_x",
    "camera_offset_y",
    "camera_binning_h",
    "camera_binning_v",
    "camera_exposure_us",
    "camera_gain",
    "camera_pixel_format",
    "camera_readout",
    "camera_framerate_enabled",
    "camera_framerate_hz",
    "camera_throughput_enabled",
    "camera_throughput_mbps",
    "camera_waterfall",
}

PREVIEW_MIN_HEIGHT = 280
PREVIEW_MAX_HEIGHT = 520

LEFT_RAIL_WIDTH = 246

# How many past checks a stage shows before the list starts scrolling.
CHECK_HISTORY_ROWS = 6

# Which stages produce a stored check, and what kind of snapshot each one writes.
CHECK_KINDS = {
    "characterise": CHECK_FLOW,
    "gravimetry": CHECK_DISPENSE,
}


class NumericParamEdit(QLineEdit):
    committed = Signal(object)
    rejected = Signal(str)

    def __init__(self, param: Param, value: Any):
        super().__init__(_display_value(value))
        self.param = param
        self.pending = False
        self.hint = "Type a number; Enter or leaving the field applies it."
        if param.minimum is not None:
            self.hint += f" Minimum: {param.minimum:g}."
        if param.maximum is not None:
            self.hint += f" Maximum: {param.maximum:g}."
        if param.name == "desktop_pressure_limit_mbar":
            self.setPlaceholderText("Off")
            self.hint += " Leave blank to disable the software pressure trip. Hardware ranges still apply."
        self.setToolTip(self.hint)
        self.textEdited.connect(self._edited)
        self.editingFinished.connect(self.commit)

    def _edited(self, _text):
        self.pending = True

    def commit(self) -> bool:
        if not self.pending:
            return True
        try:
            text = self.text().strip()
            optional_trip = self.param.name == "desktop_pressure_limit_mbar"
            value = None if optional_trip and not text else float(text)
            if value is not None and not math.isfinite(value):
                raise ValueError("Enter a finite number.")
            value = self.param.validate(value)
        except (ValueError, TypeError) as exc:
            message = f"{self.param.label}: {exc}. {self.hint} Previous value remains unchanged."
            self.setStyleSheet(f"border: 1px solid {Theme.DANGER};")
            self.setToolTip(message)
            self.rejected.emit(message)
            return False
        self.pending = False
        self.setStyleSheet("")
        self.setToolTip(self.hint)
        self.setText(_display_value(value))
        self.committed.emit(value)
        return True


class _WheelGuard(QObject):
    """Makes the wheel scroll the page and nothing else.

    Spin boxes and combo boxes treat the wheel as a value change and pyqtgraph
    treats it as a zoom, so scrolling over one silently edits a setting or
    rescales a plot instead of moving the page. Installed on the application,
    this redirects those wheel events to the page scroll viewport.
    """

    HIJACKERS = (QAbstractSpinBox, QComboBox, QAbstractItemView, QGraphicsView)

    def __init__(self, scroll: QScrollArea) -> None:
        super().__init__(scroll)
        self._scroll = scroll

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() is not QEvent.Type.Wheel:
            return False
        viewport = self._scroll.viewport()
        if obj is viewport or not self._hijacks_wheel(obj):
            return False
        QApplication.sendEvent(viewport, event)
        return True

    def _hijacks_wheel(self, obj: QObject) -> bool:
        widget = obj if isinstance(obj, QWidget) else None
        # Stop at the scroll area itself: everything above it is page chrome, and
        # the area's own scrollbars must keep their wheel handling.
        while widget is not None and widget is not self._scroll:
            if isinstance(widget, self.HIJACKERS):
                return True
            widget = widget.parentWidget()
        return False


def _event_step_index(event: Any) -> int:
    """Step number off a pipeline event, where step 0 is a real step.

    `getattr(...) or -1` reads a legitimate first step as "no step", because 0 is
    falsy -- which silently dropped the opening step of a protocol.
    """
    raw = getattr(event, "current_step", None)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _transport_object_name(label: str, *, enabled: bool, suggested: bool) -> str:
    """Style for a transport button.

    A stage never advances on its own. The one button that carries the workflow
    forward -- Proceed within a protocol, Continue between stages -- is highlighted
    once it is available, and always in the same colour so the cue reads the same
    everywhere.
    """
    if enabled and (suggested or label == "Proceed"):
        return "TransportButtonWarning"
    return "TransportButton"


def _panel_box(title: str, object_name: str = "Panel") -> tuple[QFrame, QVBoxLayout]:
    panel = QFrame()
    panel.setObjectName(object_name)
    root = QVBoxLayout(panel)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(6)
    label = QLabel(title)
    label.setObjectName("PanelTitle")
    root.addWidget(label)
    body = QVBoxLayout()
    body.setContentsMargins(0, 0, 0, 0)
    body.setSpacing(8)
    root.addLayout(body)
    return panel, body


class ControlStagePage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mounted_signature: tuple[Any, ...] | None = None
        self.transport_button_refs: list[QPushButton] = []
        self.protocol_status_label: QLabel | None = None
        self.protocol_progress_bar: QProgressBar | None = None
        self.protocol_confirm_label: QLabel | None = None
        self.param_editors: dict[str, QWidget] = {}
        self.action_table: QTableWidget | None = None
        self.channel_panel: ChannelControlPanel | None = None
        self.video_table: QTableWidget | None = None
        self.log_label: QLabel | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        panels = {spec.key: _panel_box(spec.title, spec.object_name) for spec in panel_specs(channel_manager=True)}
        self.action_box_panel, self.action_box_layout = panels["action_box"]
        self.action_panel, self.action_layout = panels["action_panel"]
        self.main_panel, self.main_layout = panels["main"]
        self.channel_manager_panel, self.channel_manager_layout = panels["channel_manager"]
        self.results_panel, self.results_layout = panels["results"]
        self.log_panel, self.log_layout = panels["log"]

        # Planning sections carry their own panel frames, so they are hosted bare.
        self.sections_host = QWidget()
        self.sections_layout = QVBoxLayout(self.sections_host)
        self.sections_layout.setContentsMargins(0, 0, 0, 0)
        self.sections_layout.setSpacing(12)
        self.sections_host.hide()

        # Every panel keeps its natural height. Spare space is not handed to any of
        # them -- the stack is sized to the page on show, so there is none to hand out.
        for key, (panel, _body) in panels.items():
            layout.addWidget(panel)
            if key == "channel_manager":
                layout.addWidget(self.sections_host)


class ControlWindow(QMainWindow):
    camera_frame_ready = Signal(object)

    def __init__(self, api: AdmetAPI) -> None:
        super().__init__()
        self.api = api
        self.tasks = Tasks(self)
        self.emergency_tasks = Tasks(self)
        self._shutdown_complete = False
        self.workflow = create_control_workflow()
        self.workflow_state = self.workflow.initial_state()
        self.values = api.settings.defaults()
        for stage in self.workflow.stages:
            self.values.update(stage.settings.defaults())
        self.values["simulated"] = api.simulated
        self.protocol_editors = {}
        self._last_protocol_log = None
        self.last_result: RunResult | None = None
        self.last_metadata: dict[str, Any] = {}
        # Result of the one-off Fluigent hardware probe. Kept out of last_metadata,
        # which every action and every status poll replaces wholesale.
        self._fluigent_probe: dict[str, Any] = {}
        self.runtime_state: dict[str, bool] = {
            "project": False,
            "camera": False,
            "camera_live": False,
            "fluidics": False,
        }
        self.status_kind = "primary"
        self.log_entries: list[str] = ["Control UI ready."]
        self.toc_rows: list[dict[str, Any]] = []
        self.project_path = Path(api.workdir) if api.workdir else None
        self._control_recording_dir: Path | None = None
        self._last_frame_id = 0
        self._last_status_poll = 0.0
        self._last_poll_error = ""
        self._camera_ack_pending = False
        self._camera_frame_unsubscribe: Callable[[], None] | None = None
        self._qt_frame: np.ndarray | None = None
        self._latest_snapshot: Any | None = None
        self._syncing_table = False
        self._action_show_all_params = False
        self.instruction_card: NotificationCard | None = None
        self.notification: NotificationCard | None = None
        self._confirmation_dialog: QMessageBox | None = None
        self._instruction_text = ""
        self._notification_text = ""
        self._notification_kind = "primary"
        self._runs_completion_confirmed = False
        # Corrections have to reach the hardware before the stage can be left, and
        # editing any correction value makes the applied set stale again.
        self._corrections_applied = False
        self._preflight: PreflightPanel | None = None
        self._calculations = None
        self._last_sweep_step = -1
        self._sweep_reading: tuple[list[float], list[float]] | None = None
        # A finished check writes its snapshot once; the completion state is polled.
        self._stored_check_stage = ""
        # Scanning every project is cheap but not free, so the history is read once
        # and dropped whenever something could have changed it.
        self._check_records: tuple[CheckRecord, ...] | None = None
        # The record a running check writes to, so its start and its end are one.
        self._check_run_id = ""
        self.check_history_table: QTableWidget | None = None
        self._check_history_rows: list[CheckRecord] = []

        self.setWindowTitle("ADMET Qt" + (" — TEST SIMULATION" if api.simulated else ""))
        self.resize(1440, 920)
        self.setMinimumSize(1040, 720)

        self.status = QLabel("")
        self.project_badge: QPushButton | None = None
        self.discovery_root = projects_root()
        self.project_refs = discover_projects(self.discovery_root)
        self.preview: PreviewDisplay | None = None
        self.action_table: QTableWidget | None = None
        self.camera_selector: QComboBox | None = None
        self.workflow_toc_panel: QFrame | None = None
        self.notification_host: QWidget | None = None
        self.notification_layout: QVBoxLayout | None = None
        self.action_box_panel: QFrame | None = None
        self.action_box_layout: QVBoxLayout | None = None
        self.action_panel: QFrame | None = None
        self.action_layout: QVBoxLayout | None = None
        self.main_panel: QFrame | None = None
        self.main_layout: QVBoxLayout | None = None
        self.channel_manager_panel: QFrame | None = None
        self.channel_manager_layout: QVBoxLayout | None = None
        self.results_panel: QFrame | None = None
        self.results_layout: QVBoxLayout | None = None
        self.log_panel: QFrame | None = None
        self.log_layout: QVBoxLayout | None = None
        self.page_scroll: QScrollArea | None = None
        self.stage_pages: dict[str, ControlStagePage] = {}
        self.current_stage_page: ControlStagePage | None = None
        self.channel_panel: ChannelControlPanel | None = None
        self.monitor_table: FluidicsMonitorTable | None = None
        self.video_table: QTableWidget | None = None
        self.csv_status: QLabel | None = None
        self._latest_pipeline_event: Any | None = None
        self._pipeline_stage_id = ""
        self._pipeline_pending_confirmation = ""
        self._tube_switch_notice_step = -1
        self._mounted_signature: tuple[Any, ...] | None = None
        self._transport_button_refs: list[QPushButton] = []
        self._protocol_status_label: QLabel | None = None
        self._protocol_progress_bar: QProgressBar | None = None
        self._protocol_confirm_label: QLabel | None = None
        self._param_editors: dict[str, QWidget] = {}
        self._numeric_drafts: dict[str, str] = {}
        self.log_label: QLabel | None = None
        self.plot_panel: PlotPanel | None = None
        self.camera_frame_ready.connect(self._show_camera_frame)
        self._subscribe_camera_frames()

        self._camera_apply_timer = QTimer(self)
        self._camera_apply_timer.setSingleShot(True)
        self._camera_apply_timer.timeout.connect(self._apply_camera_values)
        self._correction_apply_timer = QTimer(self)
        self._correction_apply_timer.setSingleShot(True)
        self._correction_apply_timer.timeout.connect(self._apply_correction_values)

        self._measurement_save_timer = QTimer(self)
        self._measurement_save_timer.setSingleShot(True)
        self._measurement_save_timer.timeout.connect(self._autosave_measurements)
        self._build_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(100)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 18, 20, 20)
        root.setSpacing(ui.spacing("group"))

        root.addWidget(self._build_top_bar())

        workspace = QWidget()
        workspace_layout = QHBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(14)

        left_rail = QWidget()
        left_rail.setFixedWidth(LEFT_RAIL_WIDTH)
        left_rail.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        left_layout = QVBoxLayout(left_rail)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        self.workflow_toc_panel = self._build_toc_panel()
        left_layout.addWidget(self.workflow_toc_panel, 0, Qt.AlignmentFlag.AlignTop)
        self.notification_host = QWidget()
        self.notification_host.setObjectName("NotificationHost")
        self.notification_host.setFixedWidth(LEFT_RAIL_WIDTH)
        self.notification_host.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        self.notification_layout = QVBoxLayout(self.notification_host)
        self.notification_layout.setContentsMargins(0, 0, 0, 0)
        self.notification_layout.setSpacing(8)
        left_layout.addWidget(self.notification_host, 0, Qt.AlignmentFlag.AlignTop)
        left_layout.addStretch()
        workspace_layout.addWidget(left_rail, 0, Qt.AlignmentFlag.AlignTop)

        page_scroll = QScrollArea()
        page_scroll.setObjectName("PageScroll")
        page_scroll.setWidgetResizable(True)
        page_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Application-wide so it also covers editors mounted later, per stage.
        self._wheel_guard = _WheelGuard(page_scroll)
        QApplication.instance().installEventFilter(self._wheel_guard)

        # One stage's page is in the scroll area at a time. A stacked widget would
        # report the height of its tallest page instead, which is what forced the
        # height to be pinned by hand -- and a pinned height cannot follow content
        # that grows after it was measured.
        self.page_scroll = page_scroll
        workspace_layout.addWidget(page_scroll, 1)
        root.addWidget(workspace, 1)

        self._render_current_stage()

    def _build_top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopPanel")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 11, 16, 11)
        layout.setSpacing(10)

        title = QLabel("admet control")
        title.setObjectName("AppTitle")
        subtitle = QLabel("Camera and fluidics workflow")
        subtitle.setObjectName("MutedText")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch()

        # Same idea as the analyze project picker: the badge is the picker.
        self.project_badge = QPushButton(self._project_text())
        self.project_badge.setObjectName("ProjectBadge")
        self.project_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.project_badge.setToolTip("Select a project")
        self.project_badge.clicked.connect(self._show_project_menu)
        layout.addWidget(self.project_badge)
        for label, callback in (
            ("New Project", self._new_project),
            ("Save Project", self._save_project),
        ):
            button = ui.button(label)
            button.clicked.connect(callback)
            layout.addWidget(button)
        return bar

    def _build_toc_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("WorkflowToc")
        panel.setFixedWidth(246)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(3)

        title = QLabel("Workflow")
        title.setObjectName("TocTitle")
        layout.addWidget(title)

        for index, stage in enumerate(self.workflow.stages):
            if stage.id == "calculations":
                add = ui.button("+ Protocol step")
                add.clicked.connect(lambda: self._add_protocol_stage())
                layout.addWidget(add)
            section = QFrame()
            section.setObjectName("TocSection")
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(0, 3, 0, 3)
            section_layout.setSpacing(3)

            row = QWidget()
            row.setObjectName("TocRow")
            row.setCursor(Qt.CursorShape.PointingHandCursor)
            row.mousePressEvent = self._toc_click_handler(index)  # type: ignore[method-assign]
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(7)
            dot = _status_dot("inactive", 8)
            label = QLabel(f"{index + 1}. " + re.sub(r"^\d+\.\s*", "", stage.label))
            label.setObjectName("TocStage")
            row_layout.addWidget(dot)
            row_layout.addWidget(label, 1)
            section_layout.addWidget(row)

            layout.addWidget(section)
            self.toc_rows.append({"dot": dot, "label": label, "section": section})


        layout.addStretch()
        return panel

    def _protocol_editor(self, stage):
        from admet.ui.protocols import ProtocolEditor

        if stage.id not in self.protocol_editors:
            self.protocol_editors[stage.id] = ProtocolEditor(self, stage)
        return self.protocol_editors[stage.id]

    def _protocol_changed(self):
        if self.current_stage_page is not None:
            self._sync_action_box(self.workflow.current_stage(self.workflow_state))

    def _rebuild_toc(self):
        old = self.workflow_toc_panel
        self.toc_rows = []
        self.workflow_toc_panel = self._build_toc_panel()
        old.parentWidget().layout().replaceWidget(old, self.workflow_toc_panel)
        old.deleteLater()
        self._sync_toc()

    def _add_protocol_stage(self, label=None):
        from admet.workflows.control import protocol_stage

        if label is None:
            label, accepted = QInputDialog.getText(self, "Add protocol step", "Name")
            if not accepted or not label.strip():
                return
        stage = protocol_stage("experiment_" + uuid.uuid4().hex[:8], label.strip())
        stages = list(self.workflow.stages)
        index = next(i for i, item in enumerate(stages) if item.id == "calculations")
        stages.insert(index, stage)
        self.workflow.stages = tuple(stages)
        self.workflow_state = replace(self.workflow_state, statuses={
            **self.workflow_state.statuses, stage.id: StageStatus.PENDING,
        })
        self._rebuild_toc()
        self._select_stage(index)
        return stage

    def _flush_measurements(self):
        for editor in self.protocol_editors.values():
            editor.measurements.flush()

    def _autosave_measurements(self):
        if self.tasks.busy or self.emergency_tasks.busy:
            self._measurement_save_timer.start(200)
            return
        try:
            self._flush_measurements()
        except Exception as exc:
            self._notify(f"Measurement save failed: {exc}", "danger", timeout_ms=0)

    def _reset_project_workflow(self):
        self._measurement_save_timer.stop()
        self._detach_live_widgets()
        if self._calculations is not None:
            self._calculations.setParent(None)
            self._calculations.hide()
            self._calculations.set_project(None)
        if self._preflight is not None:
            for section in self._preflight.sections.values():
                section.setParent(self._preflight)
            self._preflight.deleteLater()
            self._preflight = None
        self.page_scroll.takeWidget()
        for page in self.stage_pages.values():
            page.deleteLater()
        for editor in self.protocol_editors.values():
            editor.deleteLater()
        self.stage_pages.clear()
        self.protocol_editors.clear()
        self.current_stage_page = None
        self.workflow = create_control_workflow()
        self.workflow_state = self.workflow.initial_state()
        self._clear_finished_pipeline_state(self.workflow.stages[0])
        self._rebuild_toc()
        self._render_current_stage()

    def _activate_stage_page(self, stage: Stage) -> ControlStagePage:
        page = self.stage_pages.get(stage.id)
        if page is None:
            page = ControlStagePage()
            self.stage_pages[stage.id] = page
        if self.page_scroll is not None and self.page_scroll.widget() is not page:
            # takeWidget hands the outgoing page back rather than deleting it, so
            # every stage keeps its editors, its plots and its scroll position.
            previous = self.page_scroll.takeWidget()
            if previous is not None:
                previous.setParent(None)
                previous.hide()
            self.page_scroll.setWidget(page)
            page.show()
        self.current_stage_page = page
        self.action_box_panel = page.action_box_panel
        self.action_box_layout = page.action_box_layout
        self.action_panel = page.action_panel
        self.action_layout = page.action_layout
        self.main_panel = page.main_panel
        self.main_layout = page.main_layout
        self.channel_manager_panel = page.channel_manager_panel
        self.channel_manager_layout = page.channel_manager_layout
        self.results_panel = page.results_panel
        self.results_layout = page.results_layout
        self.log_panel = page.log_panel
        self.log_layout = page.log_layout
        self._load_page_refs(page)
        return page

    def _load_page_refs(self, page: ControlStagePage) -> None:
        self._mounted_signature = page.mounted_signature
        self._transport_button_refs = page.transport_button_refs
        self._protocol_status_label = page.protocol_status_label
        self._protocol_progress_bar = page.protocol_progress_bar
        self._protocol_confirm_label = page.protocol_confirm_label
        self._param_editors = page.param_editors
        self.action_table = page.action_table
        self.channel_panel = page.channel_panel
        self.video_table = page.video_table
        self.log_label = page.log_label

    def _save_page_refs(self) -> None:
        page = self.current_stage_page
        if page is None:
            return
        page.mounted_signature = self._mounted_signature
        page.transport_button_refs = self._transport_button_refs
        page.protocol_status_label = self._protocol_status_label
        page.protocol_progress_bar = self._protocol_progress_bar
        page.protocol_confirm_label = self._protocol_confirm_label
        page.param_editors = self._param_editors
        page.action_table = self.action_table
        page.channel_panel = self.channel_panel
        page.video_table = self.video_table
        page.log_label = self.log_label

    def _toc_click_handler(self, index: int):
        def handler(event) -> None:
            self._select_stage(index)

        return handler

    def _project_text(self) -> str:
        if self.api.session is None:
            return "Project: none"
        return f"Project: {self.api.session.project_id}.admetp"

    def _project_ready(self) -> bool:
        return self.api.session is not None

    def _sync_project_badge(self) -> None:
        if self.project_badge is not None:
            self.project_badge.setText(self._project_text())

    def _new_project(self) -> None:
        if self.tasks.busy:
            self._notify("Wait for the current command before changing project.", "warning")
            return
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "New admet project",
            str(session_path(Path.cwd() / f"admet_{time.strftime('%Y%m%d_%H%M%S')}")),
            "admet projects (*.admetp)",
        )
        if not path:
            return
        target = session_path(Path(path))
        try:
            self._flush_measurements()
            self.project_path = self.api.create_project(target).path
        except Exception as exc:
            self._set_status("Project create failed", "danger")
            self._notify(f"Project create failed: {exc}", "danger", timeout_ms=0)
            return
        self._control_recording_dir = None
        self._sync_project_badge()
        self._set_status("Project created", "success")
        self._notify("Project created", "success")
        self._append_log(f"project: created {self.project_path}")
        self._reset_project_workflow()
        self._render_current_stage()

    def _ensure_preflight(self) -> PreflightPanel:
        if self._preflight is None:
            self._preflight = PreflightPanel(
                channel_labels=FLUIDIC_CHANNEL_LABELS,
                channel_units={
                    label: FLUIDIC_CHANNEL_UNITS[prefix]
                    for prefix, label, *_rest in FLUIDIC_CHANNELS
                },
                liquids=self._channel_liquids,
                parent=self,
            )
            saved = self.api.session.metadata.get("qt_checkup") if self.api.session else None
            if saved:
                self._preflight.load_snapshot(saved)
        return self._preflight

    def _channel_liquids(self) -> dict[str, tuple[float, float]]:
        """(density, viscosity) per channel label, from each channel's selected liquid."""
        liquids: dict[str, tuple[float, float]] = {}
        for (prefix, label, *_rest) in FLUIDIC_CHANNELS:
            profile = profile_by_id(str(self.values.get(f"{prefix}_profile", "")))
            if profile is not None:
                liquids[label] = (profile.density, profile.viscosity)
        return liquids

    def _build_project_menu(self) -> QMenu:
        """Discovered projects, listed the way the analyze picker lists them."""
        self.project_refs = discover_projects(self.discovery_root)
        self._check_records = None
        menu = QMenu(self)
        current = str(self.project_path) if self.project_path else ""
        for ref in self.project_refs:
            action = menu.addAction(project_ref_label(ref))
            action.setCheckable(True)
            action.setChecked(str(ref.path) == current)
            action.triggered.connect(lambda _checked=False, path=ref.path: self._load_project_path(path))
        if not self.project_refs:
            empty = menu.addAction(f"No projects in {self.discovery_root}")
            empty.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Browse...").triggered.connect(self._select_project)
        return menu

    def _show_project_menu(self) -> None:
        if self.project_badge is None:
            return
        menu = self._build_project_menu()
        menu.exec(self.project_badge.mapToGlobal(self.project_badge.rect().bottomLeft()))

    def _select_project(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Select admet project",
            str(self.discovery_root),
        )
        if path:
            self._load_project_path(Path(path))

    def _load_project_path(self, path: Path) -> None:
        if self.tasks.busy:
            self._notify("Wait for the current command before changing project.", "warning")
            return
        try:
            self._flush_measurements()
            self.project_path = self.api.open_project(path).path
        except Exception as exc:
            self._set_status("Project load failed", "danger")
            self._notify(f"Project load failed: {exc}", "danger", timeout_ms=0)
            return
        self._control_recording_dir = None
        self._sync_project_badge()
        self._set_status("Project selected", "success")
        self._notify("Project selected", "success")
        self._append_log(f"project: selected {path}")
        self._reset_project_workflow()
        self._render_current_stage()

    def _save_project(self) -> None:
        if self.tasks.busy:
            self._notify("Wait for the current command before saving.", "warning")
            return
        if self.api.session is None:
            self._new_project()
            return
        self._flush_measurements()
        target = self.project_path
        if target is None:
            path, _filter = QFileDialog.getSaveFileName(
                self,
                "Save admet project",
                str(session_path(Path.cwd() / self.api.session.project_id)),
                "admet projects (*.admetp)",
            )
            if not path:
                return
            target = Path(path)
        try:
            self.api.save_project(checkup=self._preflight.workspace_state() if self._preflight else None)
        except Exception as exc:
            self._set_status("Project save failed", "danger")
            self._notify(f"Project save failed: {exc}", "danger", timeout_ms=0)
            return
        self._sync_project_badge()
        self._set_status("Project saved", "success")
        self._notify("Project saved", "success")
        self._append_log(f"project: saved {self.project_path}")

    def _ensure_recording_project(self) -> bool:
        if self.api.session is None:
            self._notify("Create or select a project before recording.", "warning", timeout_ms=0)
            return False
        if self.project_path is None:
            self._notify("Save the project before recording.", "warning", timeout_ms=0)
            self._save_project()
        if self.project_path is None:
            return False
        return True

    def _select_stage(self, index: int) -> None:
        index = max(0, min(index, len(self.workflow.stages) - 1))
        self._dismiss_notification(restore_instruction=False)
        statuses = dict(self.workflow_state.statuses)
        current = self.workflow.current_stage(self.workflow_state)
        keep_current_active = current.id == self._pipeline_stage_id and self._pipeline_active()
        if statuses.get(current.id) is StageStatus.ACTIVE and not keep_current_active:
            statuses[current.id] = StageStatus.PENDING
        target = self.workflow.stages[index]
        if statuses.get(target.id) is StageStatus.PENDING:
            statuses[target.id] = StageStatus.ACTIVE
        self.workflow_state = replace(self.workflow_state, index=index, statuses=statuses)
        self._render_current_stage()

    def _render_current_stage(self) -> None:
        if self._camera_ack_pending:
            self._acknowledge_camera_frame()
        stage = self.workflow.current_stage(self.workflow_state)
        self._refresh_runtime_state()
        if "fluidics_preflight" in stage.features:
            self._ensure_fluigent_availability()
        self._apply_stage_liquids(stage)
        previous_page = self.current_stage_page
        page = self._activate_stage_page(stage)
        stage_changed = previous_page is not None and previous_page is not page
        signature = self._structure_signature(stage)
        if structure_changed(page.mounted_signature, signature):
            self._mount_stage(stage)
            page.mounted_signature = signature
            self._mounted_signature = signature
            self._save_page_refs()
        elif stage_changed:
            self._remount_shared_stage_panels(stage)
            self._save_page_refs()
        else:
            self._mounted_signature = signature
        self._sync_stage(stage)

    def _structure_signature(self, stage: Stage) -> tuple[Any, ...]:
        state = getattr(getattr(self.api.engine, "hardware", None), "state", None)
        sensor_count = len(getattr(state, "sensor_channels", []) or ())
        pressure_count = len(getattr(state, "pressure_channels", []) or ())
        return structure_signature(
            stage.id,
            self._project_ready(),
            self.runtime_state["camera"],
            self.runtime_state["fluidics"],
            int(self.last_metadata.get("camera_count") or 0),
            sensor_count,
            pressure_count,
            self._action_show_all_params,
            bool(self._pending_pipeline_confirmation()),
            len(self._video_rows()),
        )

    def _mount_stage(self, stage: Stage) -> None:
        self._detach_live_widgets()
        if "calculations" in stage.features and self._calculations is not None:
            self._calculations.setParent(None)
            self._calculations.hide()
        if stage.id in self.protocol_editors:
            self.protocol_editors[stage.id].setParent(None)
        self._clear_layout(self.action_box_layout)
        self._clear_layout(self.main_layout)
        self._clear_layout(self.channel_manager_layout)
        self._clear_layout(self.results_layout)
        self._clear_layout(self.action_layout)
        self._clear_layout(self.log_layout)
        self.channel_panel = None
        self.csv_status = None
        self.action_table = None
        self.video_table = None
        self._transport_button_refs = []
        self._protocol_status_label = None
        self._protocol_progress_bar = None
        self._protocol_confirm_label = None
        self._param_editors = {}
        self.log_label = None

        self._render_action_box(stage)
        self._render_main(stage)
        self._render_channel_manager(stage)
        self._render_sections(stage)
        self._render_results(stage)
        self._render_action(stage)
        self._render_log()

    def _remount_shared_stage_panels(self, stage: Stage) -> None:
        self._detach_live_widgets()
        self._clear_layout(self.main_layout)
        self._clear_layout(self.channel_manager_layout)
        self._clear_layout(self.results_layout)
        self.camera_selector = None
        self.channel_panel = None
        self.csv_status = None
        self.video_table = None
        self._render_main(stage)
        self._render_channel_manager(stage)
        self._render_sections(stage)
        self._render_results(stage)
        if "calculations" in stage.features and self._calculations is not None:
            if not self._calculations.tasks.busy:
                self._calculations.refresh()

    def _render_sections(self, stage: Stage) -> None:
        """Mount this stage's planning sections, moving them off whatever held them.

        The sections are one calculation shared between stages, so they are moved
        rather than copied: only one stage shows a given section at a time, and it
        is always the same widget carrying the same numbers.
        """
        page = self.current_stage_page
        if page is None:
            return
        self._clear_layout(page.sections_layout, delete=False)
        self.check_history_table: QTableWidget | None = None
        self._check_history_rows: list[CheckRecord] = []
        keys = tuple(stage.settings_options.get("sections") or ())
        history = self._build_check_history(stage)
        if not keys and history is None:
            page.sections_host.hide()
            return
        for section in self._ensure_preflight().sections_for(keys):
            page.sections_layout.addWidget(section)
            section.show()
        if history is not None:
            page.sections_layout.addWidget(history)
        page.sections_host.show()

    def _build_check_history(self, stage: Stage) -> QWidget | None:
        """Every check of this kind already on record, oldest question first:
        what did this rig read last time?
        """
        kind = self._check_kind(stage)
        if not kind:
            return None
        panel, body = _panel_box(f"Previous {kind or 'system'} checks")
        hint = QLabel(
            "Every run of this check that has been recorded, on this rig, in any "
            "project. A run that was stopped part way is listed with what it did "
            "measure. Click a run to put its readings and the setup it was measured "
            "on back into this stage."
        )
        hint.setObjectName("StageSummary")
        hint.setWordWrap(True)
        body.addWidget(hint)

        table = GridTable(0, 3)
        table.setHorizontalHeaderLabels(("When", "Project", "Result"))
        table.verticalHeader().hide()
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setCursor(Qt.CursorShape.PointingHandCursor)
        table.cellClicked.connect(self._load_check_row)
        body.addWidget(table)
        self.check_history_table = table
        self._sync_check_history(stage)
        return panel

    def _load_check_row(self, row: int, _column: int = 0) -> None:
        if 0 <= row < len(self._check_history_rows):
            self._load_check_record(self._check_history_rows[row])

    def _sync_check_history(self, stage: Stage | None = None) -> None:
        table = self.check_history_table
        if table is None:
            return
        stage = stage or self.workflow.current_stage(self.workflow_state)
        kind = self._check_kind(stage)
        records = [record for record in self._check_history() if not kind or record.kind == kind]
        self._check_history_rows = records
        table.setRowCount(len(records) or 1)
        if not records:
            empty = QTableWidgetItem("No run of this check has been recorded yet.")
            empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(0, 0, empty)
            table.setSpan(0, 0, 1, table.columnCount())
        else:
            table.clearSpans()
            for row, record in enumerate(records):
                when = record.recorded_at[:16].replace("T", " ") or "unrecorded"
                for column, text in enumerate((when, record.project_id, record.summary)):
                    item = QTableWidgetItem(text)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    table.setItem(row, column, item)
        table.resizeRowsToContents()
        # Every run stays listed. The panel is tall enough to read at a glance and
        # scrolls past that, so a rig checked weekly for a year neither loses its
        # history nor pushes the rest of the stage off the page.
        fit_table_height(table, max_rows=CHECK_HISTORY_ROWS)
        table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if len(records) > CHECK_HISTORY_ROWS
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

    def _sync_stage(self, stage: Stage) -> None:
        self._sync_action_box(stage)
        self._sync_param_editors()
        self._sync_results()
        self._sync_log()
        self._sync_check_history(stage)
        self._sync_toc()
        self._show_stage_instruction(stage)

    def _render_action_box(self, stage: Stage) -> None:
        action_box = QFrame()
        action_box.setObjectName("ProcessBar")
        action_layout = QVBoxLayout(action_box)
        # Inset by the frame's border width, otherwise the children paint over the
        # 1px border and it disappears behind them.
        action_layout.setContentsMargins(1, 1, 1, 1)
        action_layout.setSpacing(0)

        if stage.pipeline:
            action_layout.addWidget(self._pipeline_status_widget(stage))

        command_row = QWidget()
        command_row.setObjectName("CommandRow")
        command_layout = QHBoxLayout(command_row)
        command_layout.setContentsMargins(0, 0, 0, 0)
        command_layout.setSpacing(0)
        # Without a status widget above, the button row is the whole box and its
        # outer buttons have to round their top corners too.
        command_layout.addWidget(self._transport_buttons(stage, cap_top=not stage.pipeline), 1)
        action_layout.addWidget(command_row)
        self.action_box_layout.addWidget(action_box)

    def _action_button_specs(self, stage: Stage) -> list[tuple[str, Any, bool, bool, bool, bool]]:
        controls: list[tuple[str, Any, bool, bool, bool, bool]] = []
        if "json_protocol" in stage.features:
            editor = self._protocol_editor(stage)
            active = self._pipeline_active()
            owns = stage.id == self._pipeline_stage_id
            paused = self._pipeline_paused()
            for label, callback, enabled in (
                ("Plan", editor.build_plan, not active),
                ("Execute", editor.execute, editor.executable and not active),
                ("Confirm", lambda: editor.control("confirm"), active and owns and bool(self._pending_pipeline_confirmation()) and not paused),
                ("Skip", lambda: editor.control("skip"), active and owns),
                ("Resume" if paused else "Pause", lambda: editor.control("resume" if self._pipeline_paused() else "pause"), active and owns),
                ("Abort", lambda: editor.control("abort"), active and owns),
                ("Continue", self._complete_current_stage, not active),
                ("E-STOP", self._emergency_stop, True),
            ):
                controls.append((label, lambda _checked=False, cb=callback: cb(), enabled, False, False, label == "Confirm" and enabled))
            return controls
        for control in stage_controls(stage):
            spec = self._command_spec(stage, control)
            if spec is None:
                continue
            suggested = bool(control.completes) and guard_enabled(
                control.suggest_when or control.guard, self._guard_value
            )
            controls.append((spec[0], spec[1], spec[2], spec[3], spec[4], suggested))
        controls.append(("E-STOP", lambda _checked=False: self._emergency_stop(), True, False, False, False))
        return controls

    def _render_main(self, stage: Stage) -> None:
        offline = bool({"preflight", "calculations"}.intersection(stage.features))
        self.main_panel.setVisible(not offline)
        if offline:
            return
        display = QWidget()
        display.setObjectName("MainDisplay")
        layout = QHBoxLayout(display)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        plot_was_new = self.plot_panel is None
        if self.plot_panel is None:
            self.plot_panel = PlotPanel()
            self.plot_panel.setMinimumWidth(360)
            self.plot_panel.setMaximumHeight(PREVIEW_MAX_HEIGHT)
            self.plot_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.plot_panel, 1)

        preview_column = QWidget()
        preview_layout = QVBoxLayout(preview_column)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(8)
        if has_feature(stage, "camera") and self._project_ready():
            preview_layout.addWidget(self._camera_selector_row())
        if self.preview is None:
            self.preview = PreviewDisplay()
            self.preview.frame_painted.connect(self._acknowledge_camera_frame)
            self.preview.setObjectName("CameraPreview")
            self.preview.setMinimumSize(420, PREVIEW_MIN_HEIGHT)
            self.preview.setMaximumHeight(PREVIEW_MAX_HEIGHT)
            self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        preview_layout.addWidget(self.preview)
        layout.addWidget(preview_column, 1)

        if self._latest_snapshot is not None and plot_was_new:
            self.plot_panel.update_from_snapshot(self._latest_snapshot)
        if self._qt_frame is not None:
            self.preview.set_frame(self._qt_frame)
        self._sync_preview_overlay()

        self.main_layout.addWidget(display)

    def _transport_buttons(self, stage: Stage, *, cap_top: bool = False) -> QWidget:
        group = QFrame()
        group.setObjectName("TransportButtons")
        layout = QHBoxLayout(group)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        controls = self._action_button_specs(stage)
        self._transport_button_refs = []

        for index, (label, callback, enabled, checked, toggle, suggested) in enumerate(controls):
            if index:
                separator = QFrame()
                separator.setObjectName("TransportSeparator")
                separator.setFixedWidth(1)
                layout.addWidget(separator)
            button = QPushButton(label)
            button.setObjectName(_transport_object_name(label, enabled=enabled, suggested=suggested))
            # Follow the surrounding frame's radius so a highlighted end button does
            # not square off the rounded corner.
            button.setProperty("roundLeft", index == 0)
            button.setProperty("roundRight", index == len(controls) - 1)
            button.setProperty("roundTop", cap_top)
            button.setCheckable(toggle)
            button.setChecked(checked)
            button.clicked.connect(callback)
            button.setEnabled(enabled)
            layout.addWidget(button, 1)
            self._transport_button_refs.append(button)
        return group

    def _pipeline_status_widget(self, stage: Stage) -> QWidget:
        panel = QFrame()
        panel.setObjectName("ProtocolStatus")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        status = QLabel(self._pipeline_status_text(stage))
        status.setObjectName("ProtocolStatusLabel")
        status.setWordWrap(True)
        layout.addWidget(status)
        self._protocol_status_label = status

        progress = QProgressBar()
        progress.setObjectName("ProtocolProgress")
        progress.setRange(0, 1000)
        progress.setValue(int(self._stage_progress(stage) * 10))
        progress.setTextVisible(True)
        progress.setFormat(self._pipeline_progress_text(stage))
        progress.setStyleSheet(
            "QProgressBar#ProtocolProgress::chunk "
            f"{{ background: {self._pipeline_progress_color()}; }}"
        )
        layout.addWidget(progress)
        self._protocol_progress_bar = progress

        confirmation = self._pending_pipeline_confirmation()
        if confirmation:
            confirm = QLabel(confirmation)
            confirm.setObjectName("ProtocolConfirmLabel")
            confirm.setWordWrap(True)
            layout.addWidget(confirm)
            self._protocol_confirm_label = confirm
        return panel

    def _sync_action_box(self, stage: Stage) -> None:
        editor = self.protocol_editors.get(stage.id)
        if editor is not None and editor.builtin:
            for widget in self._param_editors.values():
                widget.setEnabled(not editor._executing)
        if self._protocol_status_label is not None:
            self._protocol_status_label.setText(self._pipeline_status_text(stage))
        if self._protocol_progress_bar is not None:
            self._protocol_progress_bar.setValue(int(self._stage_progress(stage) * 10))
            self._protocol_progress_bar.setFormat(self._pipeline_progress_text(stage))
            self._protocol_progress_bar.setStyleSheet(
                "QProgressBar#ProtocolProgress::chunk "
                f"{{ background: {self._pipeline_progress_color()}; }}"
            )
        if self._protocol_confirm_label is not None:
            self._protocol_confirm_label.setText(self._pending_pipeline_confirmation())

        specs = self._action_button_specs(stage)
        if len(specs) != len(self._transport_button_refs):
            return
        for button, (label, _callback, enabled, checked, toggle, suggested) in zip(
            self._transport_button_refs,
            specs,
            strict=True,
        ):
            if button.text() != label:
                button.setText(label)
            if button.isCheckable() != toggle:
                button.setCheckable(toggle)
            if toggle and button.isChecked() != checked:
                button.setChecked(checked)
            elif not toggle and button.isChecked():
                button.setChecked(False)
            button.setEnabled(enabled)
            object_name = _transport_object_name(label, enabled=enabled, suggested=suggested)
            if button.objectName() != object_name:
                button.setObjectName(object_name)
                button.style().unpolish(button)
                button.style().polish(button)

    def _command_spec(
        self,
        stage: Stage,
        control: StageControl,
    ) -> tuple[str, Any, bool, bool, bool] | None:
        action = control.action
        if action in {"stop_camera_live", "stop_recording", "stop_protocol", "resume_protocol"}:
            return None
        if control.kind == "toggle" and control.off_action is not None:
            active = active_when(control.active_when, self._guard_value)
            state = action_button_state(
                control,
                self._guard_value,
                enabled=self._action_enabled(action),
                active=active,
                toggle=True,
                label=_short_control_label(control.label),
            )
            return (
                state.label,
                lambda _checked=False, c=control: self._run(
                    c.off_action if active_when(c.active_when, self._guard_value) else c.action
                ),
                state.enabled,
                state.active,
                state.toggle,
            )
        if action == "run_protocol":
            state = action_button_state(
                control,
                self._guard_value,
                enabled=self._action_enabled("run_protocol"),
                active=self._pipeline_active(),
                toggle=True,
                label=_short_control_label(control.label),
            )
            return (
                state.label,
                lambda _checked=False, s=stage: self._toggle_pipeline(s),
                state.enabled,
                state.active,
                state.toggle,
            )
        if action == "pause_protocol":
            paused = self._pipeline_paused()
            state = action_button_state(
                control,
                self._guard_value,
                enabled=self._action_enabled("pause_protocol"),
                active=paused,
                toggle=True,
                label="Resume" if paused else "Pause",
            )
            return (
                state.label,
                lambda _checked=False: self._toggle_pause(),
                state.enabled,
                state.active,
                state.toggle,
            )
        if action == "confirm_protocol":
            state = action_button_state(
                control,
                self._guard_value,
                enabled=self._action_enabled(action),
                active=False,
                label=_short_control_label(control.label),
            )
            return (
                state.label,
                lambda _checked=False, s=stage: self._confirm_pipeline_step(s),
                state.enabled,
                state.active,
                state.toggle,
            )
        if action == "load_last_check":
            state = action_button_state(
                control,
                self._guard_value,
                active=False,
                label=_short_control_label(control.label),
            )
            return (
                state.label,
                lambda _checked=False, s=stage: self._load_last_check(s),
                state.enabled,
                state.active,
                state.toggle,
            )
        if action == "skip_protocol":
            state = action_button_state(
                control,
                self._guard_value,
                enabled=self._action_enabled(action),
                active=False,
                label=_short_control_label(control.label),
            )
            return (
                state.label,
                lambda _checked=False, s=stage: self._skip_pipeline_step(s),
                state.enabled,
                state.active,
                state.toggle,
            )
        state = action_button_state(
            control,
            self._guard_value,
            enabled=self._action_enabled(action) if action is not None else True,
            active=False,
            label=_short_control_label(control.label),
        )
        return (
            state.label,
            lambda _checked=False, c=control: self._handle_control(stage, c),
            state.enabled,
            state.active,
            state.toggle,
        )

    def _render_channel_manager(self, stage: Stage) -> None:
        if self.channel_manager_panel is not None:
            self.channel_manager_panel.setVisible(has_feature(stage, "fluidics"))
        if not has_feature(stage, "fluidics"):
            return
        channels = self._channel_states()
        if not channels:
            label = QLabel("Connect Fluigent to manage channels.")
            label.setObjectName("StageSummary")
            self.channel_manager_layout.addWidget(label)
            return
        self.channel_panel = ChannelControlPanel(
            channels,
            on_flow=self._set_channel_flow,
            on_pressure=self._set_channel_pressure,
            on_stop=self._stop_channel,
        )
        self.channel_manager_layout.addWidget(self.channel_panel)
        self.channel_panel.update_modes(channels, pipeline_paused=self._pipeline_paused())
        if self._latest_snapshot is not None:
            self.channel_panel.update_from_snapshot(self._latest_snapshot)

    def _camera_selector_row(self) -> QWidget:
        row = QWidget()
        row.setObjectName("CameraSelectorRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        label = QLabel("Camera")
        label.setObjectName("FieldLabel")
        layout.addWidget(label)

        selector = QComboBox()
        selector.setObjectName("CameraSelector")
        cameras = self.last_metadata.get("cameras") or ()
        if cameras:
            for index, camera in enumerate(cameras):
                selector.addItem(str(camera), index)
        else:
            selector.addItem("No cameras detected", 0)
            selector.setEnabled(False)
        current_index = int(self.values.get("camera_index") or 0)
        if selector.count() > 0:
            selector.setCurrentIndex(max(0, min(current_index, selector.count() - 1)))
        selector.currentIndexChanged.connect(self._camera_index_changed)
        layout.addWidget(selector, 1)
        self.camera_selector = selector
        return row

    def _camera_index_changed(self, index: int) -> None:
        if self.camera_selector is None:
            return
        data = self.camera_selector.itemData(index)
        self.values["camera_index"] = int(data if data is not None else index)

    def _main_settings(self, stage: Stage) -> list[Param]:
        visible_names = {param.name for param in self._stage_params(stage)}
        names: list[str] = []
        names.extend(
            name
            for name in stage.settings_options.get("main", ())
            if name in visible_names and name not in names
        )
        return [self._param_by_name(name) for name in names]

    def _collapsed_params(self, stage: Stage, full_params: list[Param]) -> list[Param]:
        if "primary" in stage.settings_options:
            return self._declared_params(stage, "primary", full_params)
        params = self._main_settings(stage) or full_params
        if self._param_row_count(params) > 3:
            return params[:6]
        return params

    def _ordered_params(self, stage: Stage, params: list[Param]) -> list[Param]:
        if "primary" not in stage.settings_options and "secondary" not in stage.settings_options:
            return params
        primary = self._declared_params(stage, "primary", params)
        secondary = self._declared_params(stage, "secondary", params)
        ordered_names = {param.name for param in (*primary, *secondary)}
        return [*primary, *secondary, *(param for param in params if param.name not in ordered_names)]

    @staticmethod
    def _declared_params(stage: Stage, key: str, params: list[Param]) -> list[Param]:
        """Params the stage lists under settings_options[key], in declared order."""
        by_name = {param.name: param for param in params}
        return [by_name[name] for name in stage.settings_options.get(key, ()) if name in by_name]

    @staticmethod
    def _param_row_count(params: list[Param]) -> int:
        return max(1, (len(params) + 1) // 2)

    def _param_table(self, params: list[Param], editor_factory=None) -> QTableWidget:
        rows = self._param_row_count(params)
        table = GridTable(rows, 4)
        table.setObjectName("RawConfigTable")
        table.setHorizontalHeaderLabels(("Parameter", "Value", "Parameter", "Value"))
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        table.setAlternatingRowColors(False)

        self._syncing_table = True
        for index, param in enumerate(params):
            row = index // 2
            column = 0 if index % 2 == 0 else 2
            key = QTableWidgetItem(param.label)
            key.setFlags(key.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, column, key)
            table.setCellWidget(row, column + 1, (editor_factory or self._param_editor)(param))
        self._syncing_table = False

        table.resizeRowsToContents()
        fit_table_height(table)
        return table

    def _render_results(self, stage: Stage) -> None:
        offline = bool({"preflight", "calculations"}.intersection(stage.features))
        self.results_panel.setVisible(not offline)
        if offline:
            return
        if has_feature(stage, "fluidics"):
            if self.monitor_table is None:
                self.monitor_table = FluidicsMonitorTable()
            state = getattr(getattr(self.api.engine, "hardware", None), "state", None)
            if state is not None:
                channel_count = min(len(getattr(state, "sensor_channels", [])), len(FLUIDIC_CHANNEL_LABELS))
                if self.monitor_table.table.rowCount() != channel_count:
                    self.monitor_table.setup_channels(channel_count)
            self._update_csv_status()
            if self._latest_snapshot is not None:
                self.monitor_table.update_from_snapshot(self._latest_snapshot)
            self.results_layout.addWidget(self.monitor_table)

        video_rows = self._video_rows()
        if video_rows:
            self.video_table = _video_table(video_rows)
            self.results_layout.addWidget(self.video_table)

    def _sync_results(self) -> None:
        self._update_csv_status()
        if self._latest_snapshot is not None:
            if self.monitor_table is not None:
                self.monitor_table.update_from_snapshot(self._latest_snapshot)
            if self.channel_panel is not None:
                self.channel_panel.update_modes(self._channel_states(), pipeline_paused=self._pipeline_paused())
                self.channel_panel.update_from_snapshot(self._latest_snapshot)
        if self.video_table is not None:
            self._sync_video_table(self._video_rows())

    def _sync_video_table(self, rows: list[dict[str, str]]) -> None:
        if self.video_table is None or self.video_table.rowCount() != len(rows):
            return
        for row_index, row in enumerate(rows):
            for column_index, (key, _label) in enumerate(VIDEO_TABLE_COLUMNS):
                item = self.video_table.item(row_index, column_index)
                if item is not None and item.text() != row.get(key, ""):
                    item.setText(row.get(key, ""))
        self.video_table.resizeRowsToContents()
        fit_table_height(self.video_table)

    def _render_action(self, stage: Stage) -> None:
        self.action_panel.setVisible("preflight" not in stage.features)
        if "preflight" in stage.features:
            return
        if "calculations" in stage.features:
            from admet.ui.calculations import CalculationsPanel

            self.action_panel.layout().itemAt(0).widget().setText("Calculations")
            if self._calculations is None:
                self._calculations = CalculationsPanel()
            unchanged_project = self._calculations.project == self.api.workdir
            self._calculations.set_project(self.api.workdir)
            if unchanged_project and not self._calculations.tasks.busy:
                self._calculations.refresh()
            self.action_layout.addWidget(self._calculations)
            self._calculations.show()
            return
        self.action_panel.layout().itemAt(0).widget().setText(
            "Protocol" if "json_protocol" in stage.features else "Action Panel"
        )
        if "json_protocol" in stage.features and not stage.settings_options.get("builtin"):
            editor = self._protocol_editor(stage)
            self.action_layout.addWidget(editor)
            editor.show()
            return
        full_params = self._ordered_params(stage, self._stage_params(stage))
        collapsed_params = self._collapsed_params(stage, full_params)
        can_expand = self._param_row_count(full_params) > 3 and len(full_params) > len(collapsed_params)
        params = full_params if self._action_show_all_params and can_expand else collapsed_params

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)
        title = QLabel("Parameters")
        title.setObjectName("FieldLabel")
        header_layout.addWidget(title)
        header_layout.addStretch()
        if can_expand:
            expand = ui.button("collapse" if self._action_show_all_params else "expand", size="large")
            expand.setMinimumWidth(96)
            expand.clicked.connect(self._toggle_action_params)
            header_layout.addWidget(expand)
        self.action_layout.addWidget(header)

        if not params:
            label = QLabel("No editable parameters for this section.")
            label.setObjectName("StageSummary")
            self.action_layout.addWidget(label)
            return

        self.action_table = self._param_table(params)
        self.action_layout.addWidget(self.action_table)
        self._render_liquid_profile_summary(params)
        if "json_protocol" in stage.features:
            editor = self._protocol_editor(stage)
            self.action_layout.addWidget(editor)
            editor.show()

    def _render_liquid_profile_summary(self, params: list[Param]) -> None:
        """Spell out what the selected liquids apply, so a profile is not a black box."""
        selected = [param for param in params if param.name in LIQUID_PROFILE_PARAM_NAMES]
        if not selected:
            return
        lines = []
        for param in selected:
            profile = profile_by_id(str(self.values.get(param.name, "")))
            if profile is not None:
                channel = param.label.removesuffix(" Liquid")
                lines.append(f"{channel} - {profile.name}: {profile.summary()}")
        if not lines:
            return
        label = QLabel("\n".join(lines))
        label.setObjectName("StageSummary")
        label.setWordWrap(True)
        self.action_layout.addWidget(label)

    def _toggle_action_params(self) -> None:
        self._action_show_all_params = not self._action_show_all_params
        self._render_current_stage()

    def _render_log(self) -> None:
        state = log_state(self.log_entries)
        log = QLabel("\n".join(state.lines) or state.empty_text)
        log.setObjectName("LogText")
        log.setWordWrap(True)
        self.log_layout.addWidget(log)
        self.log_label = log

    def _sync_log(self) -> None:
        if self.log_label is not None:
            state = log_state(self.log_entries)
            self.log_label.setText("\n".join(state.lines) or state.empty_text)

    def _handle_control(self, stage: Stage, control: StageControl) -> None:
        if control.action is not None:
            result = self._run(control.action)
            if control.completes:
                if result is not None:
                    self._request_stage_completion(stage)
            return
        if control.skippable:
            self._request_stage_skip(stage)
            return
        if control.completes:
            self._request_stage_completion(stage)

    def _request_stage_completion(self, stage: Stage) -> None:
        if stage.pipeline and stage.skippable and not self._guard_value("pipeline_complete"):
            # Continue is the only way out of a pipeline stage, so it has to stand
            # for both "ran it" and "moved past it". Which one happened is recorded
            # rather than inferred: a stage passed without its protocol is skipped.
            self._request_stage_skip(stage)
            return
        if stage.confirmation_required:
            self._confirm(
                f"Confirm completion of {stage.label}?",
                lambda: self._complete_current_stage(confirmed=True),
            )
            return
        self._complete_current_stage()

    def _request_stage_skip(self, stage: Stage) -> None:
        if stage.confirmation_required:
            self._confirm(
                f"Skip {stage.label}?",
                self._skip_current_stage,
            )
            return
        self._skip_current_stage()

    def _complete_current_stage(self, *, confirmed: bool = False) -> None:
        stage = self.workflow.current_stage(self.workflow_state)
        if stage.pipeline:
            self._clear_finished_pipeline_state(stage)
        try:
            self.workflow_state = self.workflow.complete_current(
                self.workflow_state,
                confirmed=confirmed,
            )
            completion_message = stage.settings_options.get("completion_message")
            if completion_message:
                self._notify(str(completion_message), "warning")
            else:
                self._notify("Stage complete", "success")
            self._append_log("stage: complete")
        except Exception as exc:
            self._set_status("Stage failed", "danger")
            self._notify(str(exc), "danger")
        self._render_current_stage()

    def _skip_current_stage(self) -> None:
        try:
            self.workflow_state = self.workflow.skip_current(self.workflow_state)
            self._set_status("Stage skipped", "warning")
            self._notify("Stage skipped", "warning")
            self._append_log("stage: skipped")
        except Exception as exc:
            self._set_status("Skip failed", "danger")
            self._notify(str(exc), "danger")
        self._render_current_stage()

    def _run(
        self,
        action: str,
        settings: dict[str, Any] | None = None,
        *,
        raise_errors: bool = True,
        refresh: bool = True,
        notify_success: bool = True,
        _confirmed: bool = False,
    ):
        if action == "reset_safety" and not _confirmed:
            self._confirm(
                "Reset safety? Confirm the trip cause has been resolved and the physical rig is safe.",
                lambda: self._run(action, settings, refresh=refresh,
                                  notify_success=notify_success, _confirmed=True),
            )
            return None
        payload = self._prepare_action_payload(action, settings)
        if payload is None:
            return None
        job = self._build_run_job(action, payload)

        def finished(result):
            if action == "apply_corrections":
                self._corrections_applied = True
            self._handle_action_result(
                action, result, refresh=refresh, notify_success=notify_success,
            )

        self.tasks.submit(
            lambda: self.api.run(job), finished,
            lambda exc: self._handle_action_error(
                action, exc, refresh=refresh, raise_errors=False,
            ),
        )

    def _prepare_action_payload(
        self,
        action: str,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if self._action_requires_project(action) and not self._project_ready():
            self._set_status("Project required", "warning")
            self._notify("Create or select a project before camera setup.", "warning", timeout_ms=0)
            return None
        if action == "start_recording" and not self._ensure_recording_project():
            return None
        if self._action_requires_camera_live(action) and not self._camera_scene_ready():
            self._set_status("Camera live preview required", "warning")
            self._notify("Connect camera and start live preview first.", "warning", timeout_ms=0)
            return None
        if self._action_requires_fluigent(action) and not self._fluigent_ready():
            self._set_status("Fluigent connection required", "warning")
            self._notify("Connect Fluigent first.", "warning", timeout_ms=0)
            return None
        payload = self._action_payload(action)
        if settings:
            payload.update(settings)
        if action in {"apply_camera_settings", "apply_corrections"}:
            if self._numeric_drafts.keys() & payload.keys():
                self._notify("Finish or correct numeric entries before applying settings.", "warning")
                return None
        return payload

    def _build_run_job(self, action: str, payload: dict[str, Any]) -> RunJob:
        metadata = {
            "workflow": self.workflow.id,
            "stage": self.workflow.current_stage(self.workflow_state).id,
        }
        outputs: dict[str, Path] = {}
        if action == "start_recording" and self.project_path is not None and self.api.session is not None:
            label = str(payload.get("recording_label") or "recording")
            store = ProjectStore(self.project_path, self.api.session)
            target = store.control_recording_target(label)
            outputs = target.outputs
            metadata["recording_label"] = label
            metadata["recording_id"] = target.recording_id
        return RunJob(
            id=f"{action}_{int(time.time() * 1000)}",
            engine=self.api.engine.id,
            action=action,
            settings=payload,
            outputs=outputs,
            metadata=metadata,
        )

    def _handle_action_error(
        self,
        action: str,
        exc: Exception,
        *,
        refresh: bool,
        raise_errors: bool,
    ) -> None:
        self._set_status(f"{action} failed", "danger")
        self._append_log(f"{action}: {type(exc).__name__}: {exc}")
        if refresh:
            self._render_current_stage()
        if raise_errors:
            self._notify(f"{action} failed: {exc}", "danger")

    def _handle_action_result(
        self,
        action: str,
        result: RunResult,
        *,
        refresh: bool,
        notify_success: bool,
    ) -> RunResult:
        self.last_result = result
        self.last_metadata = dict(result.metadata)
        self._refresh_runtime_state()
        warning = self._action_result_warning(action, self.last_metadata)
        if warning:
            self._set_status(action, "warning")
            self._notify(warning, "warning", timeout_ms=0)
            self._append_log(f"{action}: warning: {warning}")
        else:
            self._set_status("Ready", "primary")
            if notify_success:
                self._notify(f"{action} ok", "success")
            self._append_log(f"{action}: ok")
        if refresh:
            self._render_current_stage()
        return result

    def _toggle_pipeline(self, stage: Stage) -> None:
        if self._pipeline_active():
            self._run("stop_protocol", refresh=False)
            if stage.completion_gate == "recording_confirmation" and self.last_metadata.get("recording_active"):
                self._run("stop_recording", refresh=False)
            self._render_current_stage()
            return

        if stage.completion_gate == "recording_confirmation":
            self._runs_completion_confirmed = False
        self._latest_pipeline_event = None
        if stage.id in CHECK_KINDS:
            self._stored_check_stage = ""
            self._check_run_id = ""
        if stage.settings_options.get("pipeline_name") == "Characterise":
            self._last_sweep_step = -1
            self._sweep_reading = None
            self._ensure_preflight().clear_sweep()
        self._pipeline_stage_id = stage.id
        statuses = dict(self.workflow_state.statuses)
        statuses[stage.id] = StageStatus.ACTIVE
        self.workflow_state = replace(self.workflow_state, statuses=statuses)
        started = self._run("run_protocol", self._protocol_run_settings(stage), refresh=False)
        if started is not None:
            # On disk before the first reading: a run that is stopped or that
            # never finishes still leaves the setup it was measuring.
            self._store_system_check(stage, status=CHECK_STARTED)
        self._render_current_stage()

    def _skip_pipeline_step(self, stage: Stage) -> None:
        self._run("skip_protocol", refresh=False, notify_success=False)
        self._clear_pipeline_confirmation()
        self._dismiss_notification()
        self._refresh_action_box(stage)

    def _confirm_pipeline_step(self, stage: Stage | None = None) -> None:
        if stage is None:
            stage = self.workflow.current_stage(self.workflow_state)
        confirmation = self._pending_pipeline_confirmation()
        run_complete_label = _run_complete_label(confirmation)
        if stage.completion_gate == "recording_confirmation":
            run_label = _run_start_label(confirmation)
            if run_label and not self.last_metadata.get("recording_active"):
                if self._run(
                    "start_recording",
                    self._recording_settings(run_label),
                    refresh=False,
                    notify_success=False,
                ) is None:
                    self._refresh_action_box(stage)
                    return
        if self._run("confirm_protocol", refresh=False, notify_success=False) is None:
            self._refresh_action_box(stage)
            return
        if stage.completion_gate == "recording_confirmation" and run_complete_label:
            self._runs_completion_confirmed = True
        self._clear_pipeline_confirmation()
        self._dismiss_notification()
        if stage.pipeline:
            self._refresh_action_box(stage)

    def _protocol_run_settings(self, stage: Stage) -> dict[str, Any]:
        settings = {
            "pipeline_name": stage.settings_options.get("pipeline_name", self.values.get("pipeline_name")),
            "tick_s": self.values.get("tick_s"),
        }
        for name in stage.settings_options.get("main", ()):
            settings[name] = self.values.get(name)
        return settings

    def _recording_settings(self, run_label: str) -> dict[str, Any]:
        if self.project_path is None:
            return {}
        if self._control_recording_dir is None:
            self._control_recording_dir = self.project_path / "records"
        return {
            "recording_root": str(self._control_recording_dir),
            "recording_label": run_label,
        }

    def _toggle_pause(self) -> None:
        action = "resume_protocol" if self._pipeline_paused() else "pause_protocol"
        result = self._run(action, refresh=False, notify_success=False)
        if result is None:
            self._render_current_stage()
            return
        self._render_current_stage()

    def _pipeline_active(self) -> bool:
        return self.last_metadata.get("pipeline_state") in {"running", "paused", "stopping"}

    def _pipeline_paused(self) -> bool:
        return self.last_metadata.get("pipeline_state") == "paused"

    def _pipeline_stage(self) -> Stage:
        if self._pipeline_stage_id:
            for stage in self.workflow.stages:
                if stage.id == self._pipeline_stage_id:
                    return stage
        return self.workflow.current_stage(self.workflow_state)

    def _pending_pipeline_confirmation(self) -> str:
        if self._pipeline_pending_confirmation:
            return self._pipeline_pending_confirmation
        event = self._latest_pipeline_event
        if event is None:
            return ""
        return str(getattr(event, "confirmation_message", "") or "")

    def _clear_pipeline_confirmation(self) -> None:
        self._pipeline_pending_confirmation = ""

    def _pipeline_event_state(self, event: Any | None = None) -> str:
        if event is None:
            value = self.last_metadata.get("pipeline_state") or "idle"
        else:
            value = getattr(event, "state", "idle")
        if hasattr(value, "value"):
            return str(value.value).lower()
        text = str(value)
        if "." in text:
            text = text.rsplit(".", 1)[-1]
        return text.lower()

    def _pipeline_step_label(self, stage: Stage, event: Any | None = None) -> str:
        event = self._latest_pipeline_event if event is None else event
        if event is not None:
            step = str(getattr(event, "step_name", "") or "").strip()
            if step:
                return step
        return _pipeline_start_label(stage)

    def _pipeline_status_text(self, stage: Stage) -> str:
        if "json_protocol" in stage.features and stage.id != self._pipeline_stage_id:
            editor = self._protocol_editor(stage)
            return f"{stage.label}: {editor.plan['state'] if editor.plan else 'ready to plan'}"
        event = self._latest_pipeline_event
        state = self._pipeline_event_state(event)
        step = self._pipeline_step_label(stage, event)
        confirmation = self._pending_pipeline_confirmation()
        if confirmation and state == "paused":
            return f"Paused: {step}"
        if confirmation:
            return f"Waiting: {step}"
        if state == "running":
            return f"Running: {step}"
        if state == "paused":
            return f"Paused: {step}"
        if state == "stopping":
            return f"Stopping: {step}"
        if state == "completed":
            return "Protocol complete"
        if state == "error":
            error = str(getattr(event, "error_msg", "") or "").strip() if event else ""
            return f"Protocol error: {error}" if error else "Protocol error"
        return _pipeline_start_label(stage)

    def _pipeline_progress_text(self, stage: Stage) -> str:
        if "json_protocol" in stage.features and stage.id != self._pipeline_stage_id:
            return "Not running"
        return (
            f"Step {self._pipeline_step_progress_percent():.0f}% "
            f"/ Total {self._pipeline_total_progress_percent():.0f}%"
        )

    def _pipeline_progress_color(self) -> str:
        state = self._pipeline_event_state(self._latest_pipeline_event)
        if state == "completed":
            return Theme.SUCCESS
        if state == "error":
            return Theme.DANGER
        if self._pending_pipeline_confirmation() or state in {"running", "paused", "stopping"}:
            return Theme.WARNING
        return Theme.BORDER_HOVER

    def _poll(self) -> None:
        self._poll_fluidics_plots()
        self._poll_pipeline_events()
        now = time.monotonic()
        if now - self._last_status_poll >= 0.5 and not self.tasks.busy:
            self._last_status_poll = now
            self.tasks.submit(
                lambda: (self.api.run(RunJob(
                    id="qt_status", engine="acquisition", action="camera_status",
                )), self.api.call("planned_protocols")["plans"]),
                self._status_received,
                lambda exc: self._append_log(f"status: {exc}"),
            )

    def _status_received(self, received):
        result, plans = received
        for editor in self.protocol_editors.values():
            editor.update_plan(plans)
        self.last_result = result
        was_running = self.last_metadata.get("pipeline_state")
        self.last_metadata = dict(result.metadata)
        self._refresh_runtime_state()
        self._sync_preview_overlay()
        self._protocol_changed()
        if self.last_metadata.get("pipeline_state") != was_running:
            self._refresh_action_box(self.workflow.current_stage(self.workflow_state))

    def _stop_recording_on_finished_pipeline(self) -> None:
        # Recording finalization and artifact registration belong to the service.
        return

    def _poll_pipeline_events(self) -> None:
        queue = getattr(self.api.engine, "pipeline_queue", None)
        if queue is None:
            return
        latest = None
        while not queue.empty():
            try:
                latest = queue.get_nowait()
                editor = self.protocol_editors.get(self._pipeline_stage_id)
                if editor is not None:
                    editor.pipeline_event(latest)
                signature = (str(latest.state), str(latest.outcome), latest.current_step,
                             latest.confirmation_message, latest.error_msg)
                if signature != self._last_protocol_log:
                    self._last_protocol_log = signature
                    self._append_log(
                        f"Protocol {latest.state} / {latest.outcome}: "
                        f"{latest.confirmation_message or latest.error_msg or latest.step_name}"
                    )
            except Empty:
                break
        if latest is None:
            return
        self._latest_pipeline_event = latest
        self.last_metadata["pipeline_state"] = str(latest.state)
        self._pipeline_pending_confirmation = str(latest.confirmation_message or "")
        stage = self._pipeline_stage()
        if stage.pipeline:
            confirmation = str(getattr(latest, "confirmation_message", "") or "").strip()
            if confirmation:
                self._pipeline_pending_confirmation = confirmation
                if stage.completion_gate == "recording_confirmation":
                    self._sync_run_recording_for_confirmation(confirmation)
            elif self._pipeline_event_state(latest) not in {"paused"}:
                self._clear_pipeline_confirmation()
            self._maybe_notify_tube_switch(stage, latest)
            self._capture_sweep_point(stage, latest)
            if self.workflow.current_stage(self.workflow_state).id == stage.id:
                self._refresh_action_box(stage)
            else:
                self._sync_toc()
        finished = self._pipeline_event_state(latest)
        if finished in PIPELINE_FINISHED:
            # However it ended, what it measured is worth keeping: a sweep stopped
            # half way still says what those steps cost.
            self._store_system_check(
                stage,
                status=CHECK_COMPLETE if finished == "completed" else CHECK_PARTIAL,
            )
            self._refresh_action_box(stage)

    def _store_system_check(self, stage: Stage, *, status: str = CHECK_COMPLETE) -> None:
        """Keep a check as a project record, from the moment it starts.

        Written twice: as the run begins, carrying everything entered -- tube runs,
        bores, flows, limits, liquids, the settling rule -- and again as it ends,
        carrying what was measured. The first write is what survives a run that is
        stopped, crashes, or is walked away from; the second replaces it in place,
        so one run is one record. Numbers only: no video and no fluidics trace,
        because a check is about the rig rather than about a sample.
        """
        kind = CHECK_KINDS.get(stage.id, "")
        if not kind:
            return
        if status != CHECK_STARTED and self._stored_check_stage == stage.id:
            return
        if self.project_path is None or self.api.session is None:
            self._append_log("system check: no project open, nothing stored")
            return
        snapshot = self._ensure_preflight().snapshot(
            kind,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            status=status,
            settings=self._protocol_run_settings(stage),
        )
        try:
            store = ProjectStore(self.project_path, self.api.session)
            path = store.append_system_check(
                snapshot.to_dict(),
                summary=snapshot.summary(),
                check_id=self._check_run_id,
            )
        except Exception as exc:
            self._append_log(f"system check: not stored ({type(exc).__name__}: {exc})")
            return
        self._check_run_id = path.stem
        if status != CHECK_STARTED:
            self._stored_check_stage = stage.id
        self.project_path = store.path
        self.api.session = store.session
        self.api.workdir = str(store.path)
        self._check_records = None
        # The list on this very stage is one of the things the record changes.
        self._sync_check_history(stage)
        self._append_log(f"system check stored as {path.name}: {snapshot.summary()}")

    def _capture_sweep_point(self, stage: Stage, event: Any) -> None:
        """Record a settled sweep step into the pre-flight table.

        Taken as the step advances, so the readings are the ones the step ended on --
        settled if the flow steadied, saturated if it never did, which is itself the
        measurement. Nothing is written to the project: the sweep only fills the table.
        """
        if stage.settings_options.get("pipeline_name") != "Characterise":
            return
        step_index = _event_step_index(event)
        if step_index < 0:
            return
        if step_index != self._last_sweep_step:
            # The step changed, so whatever was last seen belongs to the one that
            # ended -- reading at the moment of the change would catch flows already
            # moving towards the next setpoint.
            if self._last_sweep_step >= 0 and self._sweep_reading is not None:
                pressures, flows = self._sweep_reading
                self._ensure_preflight().record_sweep_point(
                    self._last_sweep_step, pressures, flows
                )
                self._append_log(f"sweep: recorded step {self._last_sweep_step + 1}")
            self._last_sweep_step = step_index
            self._sweep_reading = None
        if self._latest_snapshot is not None:
            self._sweep_reading = (
                list(self._latest_snapshot.pressures),
                list(self._latest_snapshot.flows),
            )

    def _sync_run_recording_for_confirmation(self, confirmation: str) -> None:
        if not _run_complete_label(confirmation):
            return
        if not self.last_metadata.get("recording_active"):
            return
        self._run("stop_recording", raise_errors=False, refresh=False, notify_success=False)

    def _maybe_notify_tube_switch(self, stage: Stage, event: Any | None) -> None:
        if stage.id != "runs" or event is None:
            return
        if self._pipeline_event_state(event) != "running":
            return
        step_name = str(getattr(event, "step_name", "") or "")
        if not step_name.startswith("Run set"):
            return
        step_index = _event_step_index(event)
        if step_index == self._tube_switch_notice_step:
            return
        progress = max(0.0, min(1.0, float(getattr(event, "progress", 0.0) or 0.0)))
        run_volume = float(self.values.get("run_volume_ul") or 0.0)
        oil_flow_ul_min = 250.0
        remaining_s = ((1.0 - progress) * run_volume / oil_flow_ul_min * 60.0) if run_volume > 0 else 0.0
        if 0.0 < remaining_s <= 5.0:
            self._tube_switch_notice_step = step_index
            self._notify("Switch collection tube to waste tube.", "warning", timeout_ms=0)

    def _clear_finished_pipeline_state(self, stage: Stage) -> None:
        """Drop the finished protocol's leftovers as the operator leaves the stage."""
        self._latest_pipeline_event = None
        self._pipeline_stage_id = ""
        self._tube_switch_notice_step = -1
        self._clear_pipeline_confirmation()
        self._dismiss_notification()
        if stage.completion_gate == "recording_confirmation":
            self._runs_completion_confirmed = False

    def _refresh_action_box(self, stage: Stage) -> None:
        self._refresh_runtime_state()
        signature = self._structure_signature(stage)
        if signature != self._mounted_signature:
            self._render_current_stage()
            return
        self._sync_action_box(stage)
        self._sync_toc()

    def _poll_fluidics_plots(self) -> None:
        queue = getattr(self.api.engine, "data_queue", None)
        if queue is None:
            return
        latest = None
        while not queue.empty():
            try:
                latest = queue.get_nowait()
            except Empty:
                break
        if latest is not None:
            self._latest_snapshot = latest
            if self.plot_panel is not None:
                self.plot_panel.update_from_snapshot(latest)
            if self.channel_panel is not None:
                self.channel_panel.update_modes(self._channel_states(), pipeline_paused=self._pipeline_paused())
                self.channel_panel.update_from_snapshot(latest)
            if self.monitor_table is not None:
                self.monitor_table.update_from_snapshot(latest)
        self._update_csv_status()

    def _update_csv_status(self) -> None:
        if self.monitor_table is None:
            return
        logger = getattr(self.api.engine, "csv_logger", None)
        filepath = getattr(logger, "filepath", None)
        row_count = int(getattr(logger, "row_count", 0) or 0)
        self.monitor_table.update_csv_status(filepath, row_count)

    def _subscribe_camera_frames(self) -> None:
        subscribe = getattr(self.api.engine, "subscribe_camera_frames", None)
        if callable(subscribe):
            self._camera_frame_unsubscribe = subscribe(self.camera_frame_ready.emit)

    def _show_camera_frame(self, frame: np.ndarray) -> None:
        self._qt_frame = frame
        self._last_frame_id = id(frame)
        self._camera_ack_pending = True
        if self.preview is None:
            self._acknowledge_camera_frame()
            return
        self.preview.set_frame(frame)
        self._sync_preview_overlay()

    def _acknowledge_camera_frame(self) -> None:
        if not self._camera_ack_pending:
            return
        self._camera_ack_pending = False
        acknowledge = getattr(self.api.engine, "acknowledge_camera_frame", None)
        if callable(acknowledge):
            acknowledge()

    def _sync_preview_overlay(self) -> None:
        if self.preview is None:
            return
        elapsed = self.last_metadata.get("camera_record_elapsed")
        fps = self.last_metadata.get("camera_fps")
        frames = self.last_metadata.get("camera_recorded_frames")
        state = "recording" if self.last_metadata.get("camera_recording") else "live"
        self.preview.set_overlay(
            (
                state,
                f"time {_measurement(elapsed)} s",
                f"fps {_measurement(fps)}",
                f"frames {'—' if frames is None else frames}",
            )
        )

    def _emergency_stop(self) -> None:
        self._set_status("Emergency stop requested", "danger")
        self.emergency_tasks.submit(
            self.api.emergency_stop,
            lambda result: self._notify(
                f"Emergency stop: {result}", "danger", timeout_ms=0,
            ),
            lambda exc: self._notify(
                f"Stop failed: {exc}. Use the physical emergency stop!", "danger", timeout_ms=0,
            ),
        )

    def _sync_toc(self) -> None:
        rows = toc_row_states(self.workflow, self.workflow_state, status_for=lambda index, _stage: self._status_key(index))
        for state, row in zip(rows, self.toc_rows, strict=True):
            _apply_dot_status(row["dot"], state.status)
            row["label"].setStyleSheet(_toc_label_qss(state.status))

    def _status_key(self, index: int) -> str:
        stage = self.workflow.stages[index]
        status = self.workflow_state.statuses.get(stage.id, StageStatus.PENDING)
        if index == self.workflow_state.index and self.status_kind == "danger":
            return "error"
        if stage.id == self._pipeline_stage_id and self._pipeline_active():
            return "processing"
        if self._check_infeasible(stage.id):
            # A setup that cannot reach its flows is worth seeing from the contents,
            # not only from inside the stage that measured it.
            return "error"
        if status is StageStatus.COMPLETE:
            return "done"
        if status is StageStatus.ACTIVE:
            return "processing"
        return "inactive"

    def _pipeline_step_progress_percent(self) -> float:
        event = self._latest_pipeline_event
        if event is None:
            return 0.0
        state = self._pipeline_event_state(event)
        if state == "completed":
            return 100.0
        if state == "error":
            return 0.0
        return min(99.0, max(0.0, float(getattr(event, "progress", 0.0) or 0.0) * 100.0))

    def _pipeline_total_progress_percent(self) -> float:
        event = self._latest_pipeline_event
        if event is None:
            return 35.0 if self.last_metadata.get("recording_active") else 0.0
        total = max(1, int(getattr(event, "total_steps", 1) or 1))
        current = max(0, int(getattr(event, "current_step", 0) or 0))
        step_progress = float(getattr(event, "progress", 0.0) or 0.0)
        state = self._pipeline_event_state(event)
        if state == "completed":
            return 100.0
        if state == "error":
            return 0.0
        return min(99.0, max(0.0, (current + step_progress) / total * 100.0))

    def _pipeline_progress_percent(self) -> float:
        return self._pipeline_total_progress_percent()

    def _stage_progress(self, stage):
        if "json_protocol" in stage.features and stage.id != self._pipeline_stage_id:
            return 0.0
        return self._pipeline_total_progress_percent()

    def _guard_value(self, name: str) -> bool:
        self._refresh_runtime_state()
        if name == "project_ready":
            return self.runtime_state["project"]
        if name == "camera_connected":
            return self.runtime_state["camera"]
        if name == "camera_live":
            return self.runtime_state["camera_live"]
        if name == "fluidics_connected":
            return self.runtime_state["fluidics"]
        if name == "pipeline_running":
            return pipeline_is_running(
                str(self.last_metadata.get("pipeline_state") or ""),
                self._pipeline_event_state(self._latest_pipeline_event),
            )
        if name == "pipeline_waiting":
            return bool(self._pending_pipeline_confirmation()) and self.last_metadata.get("pipeline_state") == "running"
        if name == "pipeline_complete":
            stage = self.workflow.current_stage(self.workflow_state)
            if not stage.pipeline:
                return False
            if self.workflow_state.statuses.get(stage.id) is not StageStatus.ACTIVE:
                return False
            # The protocol finishing is enough. The recording-confirmation gate only
            # existed to hold back the automatic advance; the operator's click is the
            # confirmation now.
            return self._pipeline_event_state(self._latest_pipeline_event) == "completed"
        if name == "corrections_applied":
            return self._corrections_applied
        if name == "devices_released":
            self._refresh_runtime_state()
            return not (self.runtime_state["camera"] or self.runtime_state["fluidics"])
        if name == "check_infeasible":
            return self._check_infeasible(
                self.workflow.current_stage(self.workflow_state).id
            )
        if name == "check_recorded":
            return self._last_check() is not None
        if name == "check_satisfied":
            # Leaving a check stage means either running it now or having an
            # earlier one to stand on. A rig with no check of this kind at all
            # has to run one.
            return self._guard_value("pipeline_complete") or self._last_check() is not None
        if name == "check_due":
            record = self._last_check()
            return record is None or record.is_due(datetime.now(timezone.utc))
        return True

    def _check_infeasible(self, stage_id: str = "") -> bool:
        """True when the measured sweep says the target flows cannot be reached."""
        if self._preflight is None:
            return False
        if stage_id and stage_id != "characterise":
            return False
        return self._preflight.verdict_feasible is False

    def _check_kind(self, stage: Stage | None = None) -> str:
        stage = stage if stage is not None else self.workflow.current_stage(self.workflow_state)
        return CHECK_KINDS.get(stage.id, "")

    def _check_history(self) -> tuple[CheckRecord, ...]:
        if self._check_records is None:
            self._check_records = discover_checks(self.discovery_root)
        return self._check_records

    def _last_check(self, stage: Stage | None = None) -> CheckRecord | None:
        kind = self._check_kind(stage)
        if not kind:
            return None
        return latest_check(self._check_history(), kind)

    def _load_last_check(self, stage: Stage) -> None:
        """Bring the last stored check back instead of running another."""
        record = self._last_check(stage)
        if record is None:
            self._notify("No stored check to load", "warning")
            return
        self._load_check_record(record)

    def _load_check_record(self, record: CheckRecord) -> None:
        """Put a stored run back into the stage that measured it.

        Only worth doing when nothing about the setup has moved since -- the
        readings are re-shown exactly as they were taken, and the stage is left
        for the operator to accept or re-run.
        """
        if self._guard_value("pipeline_running"):
            self._notify("A protocol is running -- its readings would be replaced", "warning")
            return
        data = load_check(record.path)
        if not data:
            self._notify(f"Could not read {record.path.name}", "danger")
            return
        restored = self._ensure_preflight().load_snapshot(data)
        if not restored:
            self._notify("Stored check held nothing to restore", "warning")
            return
        now = datetime.now(timezone.utc)
        age = record.age_days(now)
        age_text = f"{age:.0f} days old" if age is not None else "undated"
        if record.is_due(now):
            # Loading an old check shows its numbers but does not make them current.
            self._notify(
                f"Loaded check from {record.project_id}, {age_text} -- past the "
                f"{CHECK_INTERVAL_DAYS} day interval, so it is still owed a fresh run",
                "danger",
            )
        else:
            self._notify(f"Loaded check from {record.project_id}, {age_text}", "warning")
        self._append_log(f"check loaded: {record.path.name} ({record.summary})")
        self._render_current_stage()

    def _refresh_runtime_state(self) -> None:
        camera = getattr(self.api.engine, "camera", None)
        hardware = getattr(self.api.engine, "hardware", None)
        self.runtime_state.update(
            {
                "project": self._project_ready(),
                "camera": self._bool_attr(camera, "connected", "camera_connected"),
                "camera_live": self._bool_attr(self.api.engine, "camera_live", "camera_live"),
                "fluidics": self._bool_attr(hardware, "connected", "connected"),
            }
        )

    def _bool_attr(self, owner: Any, attr: str, fallback_metadata: str) -> bool:
        if owner is not None and hasattr(owner, attr):
            try:
                return bool(getattr(owner, attr))
            except Exception:
                pass
        return bool(self.last_metadata.get(fallback_metadata))

    def _camera_scene_ready(self) -> bool:
        self._refresh_runtime_state()
        return self.runtime_state["camera_live"]

    def _action_requires_camera_live(self, action: str) -> bool:
        return False

    def _action_requires_project(self, action: str) -> bool:
        return action in {
            "refresh_cameras",
            "connect_camera",
            "disconnect_camera",
            "start_camera_live",
            "stop_camera_live",
            "apply_camera_settings",
            "start_recording",
        }

    def _action_requires_fluigent(self, action: str) -> bool:
        return action in {
            "apply_corrections",
            "set_channel_flow",
            "set_channel_pressure",
            "stop_channel",
            "set_channel_response",
            "start_recording",
            "run_protocol",
            "pause_protocol",
            "resume_protocol",
            "confirm_protocol",
            "skip_protocol",
            "wash",
        }

    def _fluigent_ready(self) -> bool:
        self._refresh_runtime_state()
        return self.runtime_state["fluidics"]

    def _action_result_warning(self, action: str, metadata: dict[str, Any]) -> str:
        if action == "connect_camera" and metadata.get("camera_connect_ok") is False:
            return str(metadata.get("camera_connect_message") or "Camera is not available.")
        if action in {"connect_fluidics", "verify_fluigent"} and metadata.get("fluigent_connect_ok") is False:
            return str(metadata.get("fluigent_connect_message") or "Fluigent is not available.")
        return ""

    def _channel_states(self) -> list[Any]:
        manager = getattr(self.api.engine, "channel_manager", None)
        if manager is None:
            return []
        return list(getattr(manager, "channels", []))

    def _set_channel_flow(self, channel_index: int, flow_ul_min: float) -> None:
        self._run(
            "set_channel_flow",
            {"channel_index": channel_index, "channel_flow_ul_min": flow_ul_min},
            refresh=False,
        )

    def _set_channel_pressure(self, channel_index: int, pressure_mbar: float) -> None:
        self._run(
            "set_channel_pressure",
            {"channel_index": channel_index, "channel_pressure_mbar": pressure_mbar},
            refresh=False,
        )

    def _stop_channel(self, channel_index: int) -> None:
        self._run("stop_channel", {"channel_index": channel_index}, refresh=False)

    def _schedule_camera_apply(self) -> None:
        self._refresh_runtime_state()
        if self._syncing_table or not self.runtime_state["camera"]:
            return
        self._camera_apply_timer.start(180)

    def _apply_camera_values(self) -> None:
        self._run("apply_camera_settings", raise_errors=False, refresh=False)

    def _schedule_correction_apply(self) -> None:
        if self._syncing_table or not self._fluigent_ready():
            return
        self._correction_apply_timer.start(180)

    def _apply_stage_liquids(self, stage: Stage) -> None:
        """Switch channels to the liquids a stage declares (runs use oil, wash IPA)."""
        declared = stage.settings_options.get("liquids") or {}
        changed = False
        for prefix, profile_id in declared.items():
            param = f"{prefix}_profile"
            if self.values.get(param) == profile_id:
                continue
            profile = profile_by_id(str(profile_id))
            if profile is None:
                self._append_log(f"liquid: {stage.id} declares unknown profile {profile_id!r}")
                continue
            self.values[param] = profile_id
            self.values.update(profile.corrections(prefix))
            self._append_log(f"liquid: {prefix} -> {profile.name} (for {stage.id})")
            changed = True
        if changed and self._fluigent_ready():
            self._schedule_correction_apply()

    def _apply_liquid_profile(self, name: str, profile_id: Any) -> None:
        """Write a liquid profile's correction terms onto its channel."""
        profile = profile_by_id(str(profile_id))
        if profile is None:
            self._notify(f"Unknown liquid profile: {profile_id}", "danger")
            return
        prefix = name.removesuffix("_profile")
        self.values.update(profile.corrections(prefix))
        self._schedule_correction_apply()
        self._append_log(f"liquid: {prefix} -> {profile.name} ({profile.summary()})")
        QTimer.singleShot(0, self._render_current_stage)

    def _apply_correction_values(self) -> None:
        self._run("apply_corrections", raise_errors=False, refresh=False)
        self._refresh_action_box(self.workflow.current_stage(self.workflow_state))

    def _action_enabled(self, action: str | None) -> bool:
        if action is None:
            return True
        if self._action_requires_camera_live(action) and not self._camera_scene_ready():
            return False
        if action == "connect_fluidics":
            if self._fluigent_ready():
                return False
            if self.values.get("simulated"):
                return True
            return int(self._fluigent_probe.get("fluigent_instrument_count") or 0) > 0
        if action == "disconnect_fluidics":
            return self._fluigent_ready()
        if self._action_requires_fluigent(action) and not self._fluigent_ready():
            return False
        return True

    def _ensure_fluigent_availability(self) -> None:
        if self.values.get("simulated"):
            return
        if self._fluigent_probe:
            return
        if self.tasks.busy:
            return

        def received(result):
            self._fluigent_probe = dict(result.metadata)
            self.last_metadata.update(result.metadata)
            self._render_current_stage()

        self.tasks.submit(
            lambda: self.api.run(RunJob("qt_probe", "acquisition", "verify_backend")),
            received,
            lambda exc: self._notify(str(exc), "danger"),
        )

    def _stage_params(self, stage: Stage) -> list[Param]:
        params: dict[str, Param] = {}
        hidden = {
            "camera_index",
            "start_polling",
            "pipeline_name",
            "tick_s",
            "recording_root",
            "recording_label",
        }
        for param in stage.settings.params:
            if param.name in hidden:
                continue
            params[param.name] = param
        for surface in stage.surfaces:
            for param in surface.settings.params:
                if param.name in hidden:
                    continue
                params[param.name] = param
        return list(params.values())

    def _param_by_name(self, name: str) -> Param:
        for stage in self.workflow.stages:
            for param in stage.settings.params:
                if param.name == name:
                    return param
        for param in self.api.settings.params:
            if param.name == name:
                return param
        raise KeyError(name)

    def _param_editor(self, param: Param) -> QWidget:
        value = self.values.get(param.name)
        if param.kind is ParamKind.BOOLEAN:
            editor = QComboBox()
            editor.addItem("False", False)
            editor.addItem("True", True)
            editor.setCurrentIndex(1 if bool(value) else 0)
            if param.name == "simulated":
                editor.setEnabled(not self._fluigent_ready() and not self.api.simulated)
                editor.setToolTip("Choose before connecting. Disconnect before changing the backend.")
            editor.currentIndexChanged.connect(
                lambda _index, widget=editor, name=param.name: self._set_value(name, widget.currentData())
            )
            self._param_editors[param.name] = editor
            return editor
        if param.kind is ParamKind.CHOICE:
            editor = QComboBox()
            for option in param.options:
                editor.addItem(option.label, option.value)
            index = editor.findData(value)
            if index >= 0:
                editor.setCurrentIndex(index)
            editor.currentIndexChanged.connect(
                lambda _index, widget=editor, name=param.name: self._set_value(name, widget.currentData())
            )
            self._param_editors[param.name] = editor
            return editor
        if param.kind in {ParamKind.INTEGER, ParamKind.FLOAT}:
            editor = NumericParamEdit(param, value)
            if param.name in self._numeric_drafts:
                editor.setText(self._numeric_drafts[param.name])
                editor.pending = True
            editor.committed.connect(lambda value, name=param.name: self._numeric_committed(name, value))
            editor.rejected.connect(lambda message: self._notify(message, "danger", timeout_ms=0))
            editor.textEdited.connect(lambda text, name=param.name: self._numeric_draft_changed(name, text))
            self._param_editors[param.name] = editor
            return editor
        editor = QLineEdit(_display_value(value))
        editor.textChanged.connect(lambda value, name=param.name: self._set_value(name, value))
        self._param_editors[param.name] = editor
        return editor

    def _sync_param_editors(self) -> None:
        for name, editor in self._param_editors.items():
            if _widget_has_focus(editor):
                continue
            self._sync_param_editor(name, editor)

    def _sync_param_editor(self, name: str, editor: QWidget) -> None:
        if isinstance(editor, NumericParamEdit) and editor.pending:
            return
        value = self.values.get(name)
        was_blocked = editor.blockSignals(True)
        try:
            if isinstance(editor, QComboBox):
                index = editor.findData(value)
                if index >= 0 and editor.currentIndex() != index:
                    editor.setCurrentIndex(index)
            elif isinstance(editor, QSpinBox):
                next_value = int(value if value is not None else 0)
                if editor.value() != next_value:
                    editor.setValue(next_value)
            elif isinstance(editor, QDoubleSpinBox):
                next_value = float(value if value is not None else 0.0)
                if editor.value() != next_value:
                    editor.setValue(next_value)
            elif isinstance(editor, QLineEdit):
                text = _display_value(value)
                if editor.text() != text:
                    editor.setText(text)
        finally:
            editor.blockSignals(was_blocked)

    def _numeric_committed(self, name: str, value: Any) -> None:
        self._numeric_drafts.pop(name, None)
        self._set_value(name, value)

    def _numeric_draft_changed(self, name: str, text: str) -> None:
        self._numeric_drafts[name] = text
        for editor in self.protocol_editors.values():
            if editor.builtin and name in editor.stage.settings.defaults():
                editor.edited()

    def _commit_numeric_settings(self, stage: Stage) -> bool:
        valid = True
        for editor in self._param_editors.values():
            if isinstance(editor, NumericParamEdit) and not editor.commit():
                valid = False
        if self._numeric_drafts.keys() & stage.settings.defaults().keys():
            self._notify("Finish or correct numeric entries before planning; expand parameters if hidden.", "warning")
            valid = False
        return valid

    def _set_value(self, name: str, value: Any) -> None:
        if name == "simulated" and (self._fluigent_ready() or self.api.simulated):
            self._notify("Disconnect Fluigent before changing simulation mode.", "warning")
            return
        param = self._param_by_name(name)
        try:
            self.values[name] = param.validate(value)
        except Exception as exc:
            self._set_status(f"{param.label} rejected", "danger")
            self._notify(str(exc), "danger")
            return
        for editor in self.protocol_editors.values():
            if editor.builtin and name in editor.stage.settings.defaults():
                editor.edited()
        if name in CAMERA_AUTO_APPLY_PARAMS:
            self._schedule_camera_apply()
        if name in LIQUID_PROFILE_PARAM_NAMES:
            self._apply_liquid_profile(name, value)
        if name in CORRECTION_PARAM_NAMES:
            self._schedule_correction_apply()
        if name == "simulated":
            if not value:
                # Leaving simulation: drop the cached probe so real hardware is
                # re-checked on the next render.
                self._fluigent_probe.clear()
                self.last_metadata.pop("fluigent_instrument_count", None)
                self.last_metadata.pop("fluigent_instruments", None)
                self.last_metadata.pop("fluigent_device_message", None)
            QTimer.singleShot(0, self._render_current_stage)

    def _action_payload(self, action: str) -> dict[str, Any]:
        action = self.api.aliases.get(action, action)
        action_params: tuple[str, ...] = ()
        for spec in self.api.engine.actions:
            if spec.id == action:
                action_params = spec.params
                break
        return {name: self.values.get(name) for name in action_params}

    def _store_recording_artifact(self, recording: Any) -> None:
        if not isinstance(recording, dict) or self.project_path is None or self.api.session is None:
            return
        try:
            store = ProjectStore(self.project_path, self.api.session)
            store.append_control_recording(recording)
            self.project_path = store.path
            self.api.session = store.session
            self.api.workdir = str(store.path)
        except Exception as exc:
            self._append_log(f"project: acquisition metadata save failed: {exc}")

    def _video_rows(self) -> list[dict[str, str]]:
        rows: dict[str, dict[str, str]] = {}
        session = self.api.session
        if session is not None:
            for file in session.files:
                if file.role != "control_video" and file.media_type != "video/avi":
                    continue
                data = dict(file.metadata)
                data.setdefault("video_path", file.path)
                row = video_row(data)
                rows[recording_video_key(data, row)] = row

        for recording in self._recording_metadata_sources():
            row = video_row(recording)
            key = recording_video_key(recording, row)
            if prefer_video_row(rows.get(key), row):
                rows[key] = row
        return list(rows.values())

    def _recording_metadata_sources(self) -> list[dict[str, Any]]:
        recordings: list[dict[str, Any]] = []
        if self.last_result is not None:
            result_recording = self.last_result.metadata.get("recording")
            if isinstance(result_recording, dict):
                recordings.append(result_recording)

        metadata_sources = getattr(self.api.engine, "recording_metadata_sources", None)
        if callable(metadata_sources):
            recordings.extend(
                item for item in metadata_sources() if isinstance(item, dict)
            )
        return recordings

    def _show_stage_instruction(self, stage: Stage | None = None) -> None:
        if stage is None:
            stage = self.workflow.current_stage(self.workflow_state)
        text = instruction_text(stage, self._guard_value)
        self._show_instruction_card(self._with_last_check(stage, text))

    def _with_last_check(self, stage: Stage, text: str) -> str:
        """Append what this rig's last check of this kind was, if any.

        The date is given as the date, not as an age: a standing note that says
        "12 days ago" is wrong tomorrow, while the day it was run stays true.
        """
        kind = self._check_kind(stage)
        if not kind:
            return text
        record = self._last_check(stage)
        if record is None:
            # Phrased as the same fact in its empty state: "no record" on its own
            # reads as the stage not carrying this information at all.
            return f"{text} Last {kind} check: never run on this rig."
        day = record.recorded_at[:10] or "an unrecorded date"
        return f"{text} Last {kind} check: {day}, project {record.project_id}."

    def _set_status(self, text: str, kind: str = "primary") -> None:
        self.status_kind = kind

    def _notify(self, text: str, kind: str = "primary", *, timeout_ms: int = 3500) -> None:
        self._show_notification_card(text, kind=kind, timeout_ms=timeout_ms)

    def _confirm(self, text: str, on_confirm: Callable[[], None]) -> None:
        if self._confirmation_dialog is not None:
            self._confirmation_dialog.raise_()
            self._confirmation_dialog.activateWindow()
            return
        dialog = QMessageBox(QMessageBox.Icon.Warning, "Confirm — ADMET", text, parent=self)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        dialog.button(QMessageBox.StandardButton.Ok).setText("Confirm")
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.setEscapeButton(QMessageBox.StandardButton.Cancel)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._confirmation_dialog = dialog

        def finished(result):
            if self._confirmation_dialog is not dialog:
                return
            self._confirmation_dialog = None
            dialog.deleteLater()
            if result == QMessageBox.StandardButton.Ok:
                on_confirm()

        dialog.finished.connect(finished)
        dialog.open()
        dialog.raise_()
        dialog.activateWindow()
        dialog.button(QMessageBox.StandardButton.Cancel).setFocus()

    def _dismiss_notification(self, *, restore_instruction: bool = True) -> None:
        card = self.notification
        if card is None:
            if restore_instruction:
                self._show_stage_instruction()
            return
        self.notification = None
        self._notification_text = ""
        self._notification_kind = "primary"
        if self.notification_layout is not None:
            self.notification_layout.removeWidget(card)
        card.blockSignals(True)
        card.close()
        card.deleteLater()
        if restore_instruction:
            self._show_stage_instruction()

    def _show_instruction_card(self, text: str) -> None:
        if self.instruction_card is not None and self._instruction_text == text:
            return
        if self.instruction_card is not None:
            # Same card, new words. Rebuilding it tore the widget down and put an
            # identical one back on every stage change, which is seen as a blink.
            self._instruction_text = text
            self.instruction_card.set_text(text)
            self._position_notification()
            return
        parent = self.notification_host or self.centralWidget()
        if parent is None:
            return
        self._instruction_text = text
        self.instruction_card = NotificationCard(parent, text, "primary")
        if self.notification_layout is not None:
            self.notification_layout.insertWidget(0, self.instruction_card)
        self._position_notification()
        self.instruction_card.show()

    def _show_notification_card(
        self,
        text: str,
        *,
        kind: str,
        timeout_ms: int = 3500,
    ) -> None:
        if (
            self.notification is not None
            and self._notification_text == text
            and self._notification_kind == kind
        ):
            return
        if self.notification is not None:
            self._dismiss_notification(restore_instruction=False)
        parent = self.notification_host or self.centralWidget()
        if parent is None:
            return
        self.notification = NotificationCard(parent, text, kind)
        self._notification_text = text
        self._notification_kind = kind
        self.notification.closed.connect(self._clear_notification)
        if self.notification_layout is not None:
            self.notification_layout.addWidget(self.notification)
        self._position_notification()
        self.notification.show()
        self.notification.raise_()
        if timeout_ms > 0:
            card = self.notification
            QTimer.singleShot(
                timeout_ms,
                lambda: self._dismiss_notification() if card is self.notification else None,
            )

    def _clear_notification(self) -> None:
        card = self.notification
        self.notification = None
        self._notification_text = ""
        self._notification_kind = "primary"
        if card is not None and self.notification_layout is not None:
            self.notification_layout.removeWidget(card)
            card.deleteLater()

    def _position_notification(self) -> None:
        if self.instruction_card is None and self.notification is None:
            return
        if self.notification_host is not None:
            available_height = max(120, self.height() - self.notification_host.y() - 18)
            if self.instruction_card is not None:
                self.instruction_card.fit_to_parent(
                    LEFT_RAIL_WIDTH,
                    available_height,
                )
            if self.notification is not None:
                self.notification.fit_to_parent(LEFT_RAIL_WIDTH, available_height)
            return
        parent = self.centralWidget()
        if parent is None:
            return
        card = self.notification or self.instruction_card
        if card is None:
            return
        margin = 18
        card.fit_to_parent(
            max(220, parent.width() - margin * 2),
            max(120, parent.height() - margin * 2),
        )
        size = card.size()
        x = max(margin, parent.width() - size.width() - margin)
        y = max(margin, parent.height() - size.height() - margin)
        card.move(x, y)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_notification()

    def _connected_devices(self) -> tuple[str, ...]:
        self._refresh_runtime_state()
        return connected_devices(
            camera=self.runtime_state["camera"],
            camera_live=self.runtime_state["camera_live"],
            fluidics=self.runtime_state["fluidics"],
            recording=bool(self.last_metadata.get("recording_active")),
            protocol=(self._pipeline_stage_id or "pipeline") if self._pipeline_active() else "",
        )

    def _confirm_close(self, devices: tuple[str, ...]) -> None:
        self._confirm(
            "Close ADMET? Zero outputs, stop recording and disconnect:\n" + "\n".join(devices),
            self._begin_shutdown,
        )

    def closeEvent(self, event) -> None:
        if self._shutdown_complete:
            super().closeEvent(event)
            return
        event.ignore()
        if self.emergency_tasks.busy:
            return
        devices = self._connected_devices()
        if devices:
            self._confirm_close(devices)
            return
        self._begin_shutdown()

    def _begin_shutdown(self):
        if self.emergency_tasks.busy:
            return
        self._measurement_save_timer.stop()
        if self._camera_frame_unsubscribe is not None:
            self._camera_frame_unsubscribe()
            self._camera_frame_unsubscribe = None
        if self._camera_ack_pending:
            self._acknowledge_camera_frame()
        self.timer.stop()
        self._camera_apply_timer.stop()
        self._correction_apply_timer.stop()
        self.centralWidget().setEnabled(False)
        self.emergency_tasks.submit(self.api.shutdown, self._closed_safely, self._close_failed)

    def _closed_safely(self, _result):
        try:
            self._flush_measurements()
        except Exception as exc:
            self._close_failed(exc)
            return
        self._shutdown_complete = True
        self.close()

    def _close_failed(self, exc):
        self.centralWidget().setEnabled(True)
        self.timer.start(100)
        self._notify(f"Shutdown failed: {exc}. Use physical emergency stop.", "danger", timeout_ms=0)

    def _append_log(self, text: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        entry = f"{timestamp} {text}"
        self.log_entries.append(entry)
        try:
            print(f"[admet control] {entry}", flush=True)
        except OSError:
            pass

    def _detach_live_widgets(self) -> None:
        for widget in (self.plot_panel, self.preview, self.monitor_table):
            if widget is not None and widget.parent() is not None:
                widget.setParent(None)

    def _clear_layout(self, layout, *, delete: bool = True) -> None:
        """Empty a layout. Widgets that outlive the mount are only unparented."""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget is not None:
                if delete:
                    widget.deleteLater()
                else:
                    widget.setParent(None)
            elif child_layout is not None:
                self._clear_layout(child_layout, delete=delete)


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

    def update_modes(self, channels: list[Any], *, pipeline_paused: bool = False) -> None:
        for index, row in enumerate(self._rows):
            if index >= len(channels):
                row.set_status("missing")
                row.set_editable(False)
                continue
            channel = channels[index]
            mode = getattr(channel, "mode", "off")
            owner = getattr(channel, "owner", "user")
            row.set_status(f"{mode} / {owner}")
            # Same rule the engine enforces: a channel the protocol drives is only
            # writable while the protocol is paused.
            row.set_editable(owner == "user" or pipeline_paused)

    def update_from_snapshot(self, snapshot: Any) -> None:
        for index, row in enumerate(self._rows):
            row.update_values(
                _safe_list_value(snapshot.pressures, index),
                _safe_list_value(snapshot.flows, index),
                _safe_list_value(snapshot.volumes_ul, index),
                _safe_list_value(snapshot.stability, index),
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

        self.live = QLabel("— mbar | — µL/min | — µL | unknown")
        self.live.setObjectName("MutedText")
        layout.addWidget(self.live)

        flow_row = QHBoxLayout()
        flow_row.setContentsMargins(0, 0, 0, 0)
        flow_row.setSpacing(6)
        flow_label = QLabel("Flow")
        flow_label.setObjectName("FieldLabel")
        flow_row.addWidget(flow_label)
        self.flow = _small_double_box(0.0, 5000.0, " uL/min")
        self.flow.setLocale(QLocale.c())
        self.flow.setToolTip("Type a flow target and press Enter to apply. Stop zeros this channel.")
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
        self.pressure.setLocale(QLocale.c())
        self.pressure.setToolTip("Type a pressure target and press Enter to apply. Stop zeros this channel.")
        self.pressure.lineEdit().returnPressed.connect(lambda: on_pressure(self._index, self.pressure.value()))
        pressure_row.addWidget(self.pressure, 1)
        layout.addLayout(pressure_row)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(lambda: on_stop(self._index))
        layout.addWidget(self.stop_button)
        self._editable = True

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def set_editable(self, editable: bool) -> None:
        """Show whether this channel can be driven by hand right now."""
        if editable == self._editable:
            return
        self._editable = editable
        self.flow.setEnabled(editable)
        self.pressure.setEnabled(editable)
        self.stop_button.setEnabled(editable)
        self.setProperty("locked", not editable)
        self.style().unpolish(self)
        self.style().polish(self)

    def update_values(self, pressure: float, flow: float, volume: float, stable: bool) -> None:
        state = "unknown" if stable is None else ("stable" if stable else "unstable")
        self.live.setText(
            f"{_measurement(pressure)} mbar | {_measurement(flow)} µL/min | "
            f"{_measurement(volume)} µL | {state}"
        )


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

        self.table = GridTable(0, 9)
        self.table.setObjectName("RawConfigTable")
        self.table.setHorizontalHeaderLabels(
            ("Channel", "Pressure", "Flow", "Mean", "Std", "Min", "Max", "Volume", "Stable")
        )
        self.table.verticalHeader().hide()
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
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
            stable = _safe_list_value(snapshot.stability, row)
            self._set_item(row, 1, _measurement(pressure) + " mbar")
            self._set_item(row, 2, _measurement(flow) + " µL/min")
            for column, key in enumerate(("mean", "std", "min", "max"), 3):
                self._set_item(row, column, _measurement(getattr(stats, key, None)))
            self._set_item(row, 7, _measurement(volume) + " µL")
            self._set_item(row, 8, "—" if stable is None else ("yes" if stable else "no"))
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


def _small_double_box(minimum: float, maximum: float, suffix: str) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setDecimals(3)
    box.setSingleStep(1.0)
    box.setSuffix(suffix)
    box.setMaximumWidth(150)
    return box


def _widget_has_focus(widget: QWidget) -> bool:
    focus = QApplication.focusWidget()
    return focus is widget or bool(focus is not None and widget.isAncestorOf(focus))


def _measurement(value):
    return "—" if value is None else f"{value:.3f}"


def _safe_list_value(values: Any, index: int, *, default: Any = None) -> Any:
    try:
        return values[index]
    except (IndexError, TypeError):
        return default


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
        # No wheel override: _WheelGuard routes the wheel to the page scroll, so the
        # plot never zooms. Dragging still pans and pauses auto-scroll.
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


class NotificationCard(QFrame):
    closed = Signal()

    def __init__(
        self,
        parent: QWidget,
        text: str,
        kind: str,
    ) -> None:
        super().__init__(parent)
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

        self.setStyleSheet(_notification_qss(kind))

    def set_text(self, text: str) -> None:
        """Replace the words without replacing the card."""
        self.text_label.setText(text)
        self.text_label.setMinimumHeight(0)
        self.updateGeometry()

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
        self._overlay_lines: tuple[str, ...] = ()

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

    def set_overlay(self, lines: tuple[str, ...]) -> None:
        self._overlay_lines = tuple(line for line in lines if line)
        self.update()

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
        if self._overlay_lines:
            text = "\n".join(self._overlay_lines)
            overlay_rect = QRect(self.frame_rect.x() + 10, self.frame_rect.y() + 10, 168, 76)
            painter.fillRect(overlay_rect, QColor(0, 0, 0, 145))
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(
                overlay_rect.adjusted(8, 7, -8, -7),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                text,
            )
        self.frame_painted.emit()


def _video_table(rows: list[dict[str, str]]) -> QTableWidget:
    table = GridTable(len(rows), len(VIDEO_TABLE_COLUMNS))
    table.setObjectName("RawConfigTable")
    table.setHorizontalHeaderLabels(tuple(label for _key, label in VIDEO_TABLE_COLUMNS))
    table.verticalHeader().hide()
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for column in range(1, len(VIDEO_TABLE_COLUMNS)):
        table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
    for row_index, row in enumerate(rows):
        for column_index, (key, _label) in enumerate(VIDEO_TABLE_COLUMNS):
            item = QTableWidgetItem(row.get(key, ""))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row_index, column_index, item)
    table.resizeRowsToContents()
    fit_table_height(table)
    return table


def _short_control_label(label: str) -> str:
    replacements = {
        "Disconnect": "Disconnect",
        "Run Priming": "Prime",
        "Confirm Step": "Confirm",
        "Priming Done": "Done",
        "Run Protocol": "Run",
        "Stop Protocol": "Stop",
        "Runs Done": "Done",
        "Run Wash": "Wash",
        "Skip Wash": "Skip",
        "Workflow Done": "Done",
    }
    return replacements.get(label, label)


def _pipeline_start_label(stage: Stage) -> str:
    return str(stage.settings_options.get("pipeline_label") or "Start")


def _run_start_label(message: str) -> str:
    match = re.search(r"\bStart\s+(set\d{2}_rep\d{2})\b", message)
    return match.group(1) if match else ""


def _run_complete_label(message: str) -> str:
    match = re.search(r"\b(set\d{2}_rep\d{2})\s+complete\b", message)
    return match.group(1) if match else ""


def _status_dot(status: str, size: int) -> QLabel:
    dot = QLabel()
    dot.setFixedSize(size, size)
    dot.setObjectName("StatusDot")
    _apply_dot_status(dot, status)
    return dot


def _apply_dot_status(dot: QLabel, status: str) -> None:
    color = STATUS_COLORS[status]
    radius = max(1, dot.width() // 2)
    dot.setStyleSheet(
        f"background: {color}; border: 1px solid {color}; border-radius: {radius}px;"
    )


def _toc_label_qss(status: str) -> str:
    if status == "done":
        color = Theme.SUCCESS
    elif status == "inactive":
        color = Theme.TEXT_SUBTLE
    else:
        color = Theme.TEXT_WHITE
    weight = "650" if status == "processing" else "500"
    return f"color: {color}; font-weight: {weight};"


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


def _display_value(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{key}={val}" for key, val in value.items())
    if isinstance(value, list | tuple | set):
        return ", ".join(str(item) for item in value)
    if value is None:
        return ""
    return str(value)
