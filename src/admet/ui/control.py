from __future__ import annotations

import re
import signal
import sys
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from queue import Empty
from typing import Any, Callable

import numpy as np
from PySide6.QtCore import QEvent, QObject, QRect, QSize, QTimer, Qt, Signal
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
    QProgressBar,
    QPushButton,
    QComboBox,
    QScrollArea,
    QDoubleSpinBox,
    QSizePolicy,
    QLineEdit,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from admet.core.api import AdmetAPI
from admet.core.run import RunJob, RunResult
from admet.core.project import ProjectStore
from admet.core.engine import Param, ParamKind
from admet.core.session import load_session, new_session, save_session, session_path
from admet.workflows import Stage, StageControl, StageStatus
from admet.engines.acquisition.fluidics.config import (
    FLUIDIC_CHANNEL_LABELS,
    FLUIDIC_CHANNELS,
)
from admet.engines.acquisition.settings import CORRECTION_PARAM_NAMES
from admet.ui import theme as ui
from admet.ui.data import (
    VIDEO_TABLE_COLUMNS,
    prefer_video_row,
    recording_video_key,
    video_row,
)
from admet.ui.theme import STATUS_COLORS, Theme
from admet.ui.window import log_state, panel_specs, structure_changed, structure_signature
from admet.ui.workflow_view import (
    action_button_state,
    active_when,
    guard_enabled,
    has_feature,
    instruction_text,
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
            QTimer.singleShot(0, app.quit)

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

        for panel, _body in panels.values():
            layout.addWidget(panel)
        layout.addStretch()


class ControlWindow(QMainWindow):
    camera_frame_ready = Signal(object)

    def __init__(self, api: AdmetAPI) -> None:
        super().__init__()
        self.api = api
        self.workflow = create_control_workflow()
        self.workflow_state = self.workflow.initial_state()
        self.values = api.settings.defaults()
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
        self.project_path: Path | None = None
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
        self._instruction_text = ""
        self._notification_text = ""
        self._notification_kind = "primary"
        self._runs_completion_confirmed = False
        self._completion_pending = False

        self.setWindowTitle("admet control")
        self.resize(1440, 920)
        self.setMinimumSize(1040, 720)

        self.status = QLabel("")
        self.project_badge: QLabel | None = None
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
        self.stage_stack: QStackedWidget | None = None
        self.stage_pages: dict[str, ControlStagePage] = {}
        self.current_stage_page: ControlStagePage | None = None
        self.channel_panel: ChannelControlPanel | None = None
        self.monitor_table: FluidicsMonitorTable | None = None
        self.video_table: QTableWidget | None = None
        self.csv_status: QLabel | None = None
        self._latest_pipeline_event: Any | None = None
        self._pipeline_stage_id = ""
        self._pipeline_pending_confirmation = ""
        self._pipeline_confirmation_notice = ""
        self._tube_switch_notice_step = -1
        self._mounted_signature: tuple[Any, ...] | None = None
        self._transport_button_refs: list[QPushButton] = []
        self._protocol_status_label: QLabel | None = None
        self._protocol_progress_bar: QProgressBar | None = None
        self._protocol_confirm_label: QLabel | None = None
        self._param_editors: dict[str, QWidget] = {}
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

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(12)
        page_scroll.setWidget(content)

        self.stage_stack = QStackedWidget()
        self.stage_stack.setObjectName("StageStack")
        content_layout.addWidget(self.stage_stack)
        content_layout.addStretch()
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

        self.project_badge = QLabel(self._project_text())
        self.project_badge.setObjectName("ProjectBadge")
        layout.addWidget(self.project_badge)
        for label, callback in (
            ("New Project", self._new_project),
            ("Select Project", self._select_project),
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
            label = QLabel(stage.label)
            label.setObjectName("TocStage")
            row_layout.addWidget(dot)
            row_layout.addWidget(label, 1)
            section_layout.addWidget(row)

            layout.addWidget(section)
            self.toc_rows.append({"dot": dot, "label": label, "section": section})
        layout.addStretch()
        return panel

    def _activate_stage_page(self, stage: Stage) -> ControlStagePage:
        page = self.stage_pages.get(stage.id)
        if page is None:
            page = ControlStagePage()
            self.stage_pages[stage.id] = page
            if self.stage_stack is not None:
                self.stage_stack.addWidget(page)
        if self.stage_stack is not None and self.stage_stack.currentWidget() is not page:
            self.stage_stack.setCurrentWidget(page)
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
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "New admet project",
            str(session_path(Path.cwd() / f"admet_{time.strftime('%Y%m%d_%H%M%S')}")),
            "admet projects (*.admetp)",
        )
        if not path:
            return
        target = session_path(Path(path))
        project_id = target.stem
        session = new_session(project_id)
        try:
            self.project_path = save_session(target, session)
        except Exception as exc:
            self._set_status("Project create failed", "danger")
            self._notify(f"Project create failed: {exc}", "danger", timeout_ms=0)
            return
        self.api.session = session
        self.api.workdir = str(self.project_path)
        self._control_recording_dir = None
        self._sync_project_badge()
        self._set_status("Project created", "success")
        self._notify("Project created", "success")
        self._append_log(f"project: created {self.project_path}")
        self._render_current_stage()

    def _select_project(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Select admet project",
            str(Path.cwd()),
        )
        if not path:
            return
        try:
            self.api.session = load_session(path)
        except Exception as exc:
            self._set_status("Project load failed", "danger")
            self._notify(f"Project load failed: {exc}", "danger", timeout_ms=0)
            return
        self.project_path = session_path(Path(path))
        self.api.workdir = str(self.project_path)
        self._control_recording_dir = None
        self._sync_project_badge()
        self._set_status("Project selected", "success")
        self._notify("Project selected", "success")
        self._append_log(f"project: selected {path}")
        self._render_current_stage()

    def _save_project(self) -> None:
        if self.api.session is None:
            self._new_project()
            return
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
            self.project_path = save_session(target, self.api.session)
        except Exception as exc:
            self._set_status("Project save failed", "danger")
            self._notify(f"Project save failed: {exc}", "danger", timeout_ms=0)
            return
        self.api.workdir = str(self.project_path)
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
        self.api.workdir = str(self.project_path)
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
        self._render_results(stage)

    def _sync_stage(self, stage: Stage) -> None:
        self._sync_action_box(stage)
        self._sync_param_editors()
        self._sync_results()
        self._sync_log()
        self._sync_toc()
        self._show_stage_instruction(stage)

    def _render_action_box(self, stage: Stage) -> None:
        action_box = QFrame()
        action_box.setObjectName("ProcessBar")
        action_layout = QVBoxLayout(action_box)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(0)

        if stage.pipeline:
            action_layout.addWidget(self._pipeline_status_widget(stage))

        command_row = QWidget()
        command_row.setObjectName("CommandRow")
        command_layout = QHBoxLayout(command_row)
        command_layout.setContentsMargins(0, 0, 0, 0)
        command_layout.setSpacing(0)
        command_layout.addWidget(self._transport_buttons(stage), 1)
        action_layout.addWidget(command_row)
        self.action_box_layout.addWidget(action_box)

    def _action_button_specs(self, stage: Stage) -> list[tuple[str, Any, bool, bool, bool]]:
        controls: list[tuple[str, Any, bool, bool, bool]] = []
        for control in stage_controls(stage):
            if control.completes and control.action is None:
                continue
            spec = self._command_spec(stage, control)
            if spec is None:
                continue
            controls.append((spec[0], spec[1], spec[2], spec[3], spec[4]))
        controls.append(("E-STOP", lambda _checked=False: self._emergency_stop(), True, False, False))
        return controls

    def _render_main(self, stage: Stage) -> None:
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

    def _transport_buttons(self, stage: Stage) -> QWidget:
        group = QFrame()
        group.setObjectName("TransportButtons")
        layout = QHBoxLayout(group)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        controls = self._action_button_specs(stage)
        self._transport_button_refs = []

        for index, (label, callback, enabled, checked, toggle) in enumerate(controls):
            if index:
                separator = QFrame()
                separator.setObjectName("TransportSeparator")
                separator.setFixedWidth(1)
                layout.addWidget(separator)
            button = QPushButton(label)
            if label == "Proceed" and enabled:
                button.setObjectName("TransportButtonWarning")
            else:
                button.setObjectName("TransportButton")
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
        progress.setValue(int(self._pipeline_progress_percent() * 10))
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
        if self._protocol_status_label is not None:
            self._protocol_status_label.setText(self._pipeline_status_text(stage))
        if self._protocol_progress_bar is not None:
            self._protocol_progress_bar.setValue(int(self._pipeline_total_progress_percent() * 10))
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
        for button, (label, _callback, enabled, checked, toggle) in zip(
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
            object_name = "TransportButtonWarning" if label == "Proceed" and enabled else "TransportButton"
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
            paused = self.last_metadata.get("pipeline_state") == "paused"
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
            return self._correction_primary_params(full_params)
        params = self._main_settings(stage) or full_params
        if self._param_row_count(params) > 3:
            return params[:6]
        return params

    def _ordered_params(self, stage: Stage, params: list[Param]) -> list[Param]:
        if "primary" not in stage.settings_options and "secondary" not in stage.settings_options:
            return params
        primary = self._correction_primary_params(params)
        secondary = self._correction_secondary_params(params)
        ordered_names = {param.name for param in (*primary, *secondary)}
        return [*primary, *secondary, *(param for param in params if param.name not in ordered_names)]

    def _correction_primary_params(self, params: list[Param]) -> list[Param]:
        by_name = {param.name: param for param in params}
        return [
            by_name[name]
            for prefix, _label, _calibration, _scale, _offset, _quadratic in FLUIDIC_CHANNELS
            for name in (f"{prefix}_calibration", f"{prefix}_scale")
            if name in by_name
        ]

    def _correction_secondary_params(self, params: list[Param]) -> list[Param]:
        by_name = {param.name: param for param in params}
        return [
            by_name[name]
            for prefix, _label, _calibration, _scale, _offset, _quadratic in FLUIDIC_CHANNELS
            for name in (f"{prefix}_offset", f"{prefix}_quadratic")
            if name in by_name
        ]

    @staticmethod
    def _param_row_count(params: list[Param]) -> int:
        return max(1, (len(params) + 1) // 2)

    def _param_table(self, params: list[Param]) -> QTableWidget:
        rows = self._param_row_count(params)
        table = QTableWidget(rows, 4)
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
        table.setShowGrid(True)

        self._syncing_table = True
        for index, param in enumerate(params):
            row = index // 2
            column = 0 if index % 2 == 0 else 2
            key = QTableWidgetItem(param.label)
            key.setFlags(key.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, column, key)
            table.setCellWidget(row, column + 1, self._param_editor(param))
        self._syncing_table = False

        table.resizeRowsToContents()
        _fit_table_height(table)
        return table

    def _render_results(self, stage: Stage) -> None:
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
                self.channel_panel.update_modes(self._channel_states())
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
        _fit_table_height(self.video_table)

    def _render_action(self, stage: Stage) -> None:
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
        header_action = stage.settings_options.get("header_action")
        if header_action:
            action = next((item for item in stage.actions if item.action == header_action), None)
            apply_button = ui.button(action.label if action is not None else "Apply", variant="primary", size="inline")
            apply_button.clicked.connect(self._apply_all_corrections)
            apply_button.setEnabled(self._fluigent_ready())
            header_layout.addWidget(apply_button)
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

    def _auto_complete_ready_stage(self, action: str) -> bool:
        stage = self.workflow.current_stage(self.workflow_state)
        actions = set(stage.settings_options.get("auto_complete_actions", ()))
        guard = str(stage.settings_options.get("auto_complete_guard") or "")
        if action in actions and guard_enabled(guard, self._guard_value):
            self._complete_current_stage()
            return True
        return False

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
    ):
        payload = self._prepare_action_payload(action, settings)
        if payload is None:
            return None
        try:
            result = self.api.run(self._build_run_job(action, payload))
        except Exception as exc:
            self._handle_action_error(action, exc, refresh=refresh, raise_errors=raise_errors)
            return None
        return self._handle_action_result(
            action,
            result,
            refresh=refresh,
            notify_success=notify_success,
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
        if action == "stop_recording":
            self._store_recording_artifact(result.metadata.get("recording"))
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
        if self._auto_complete_ready_stage(action):
            return result
        if refresh:
            self._render_current_stage()
        return result

    def _toggle_pipeline(self, stage: Stage) -> None:
        if self._pipeline_active():
            self._run("stop_protocol", refresh=False)
            if stage.completion_gate == "recording_confirmation" and self.last_metadata.get("recording_active"):
                self._run("stop_recording", refresh=False)
            self._completion_pending = False
            self._render_current_stage()
            return

        if stage.completion_gate == "recording_confirmation":
            self._runs_completion_confirmed = False
        self._pipeline_stage_id = stage.id
        statuses = dict(self.workflow_state.statuses)
        statuses[stage.id] = StageStatus.ACTIVE
        self.workflow_state = replace(self.workflow_state, statuses=statuses)
        self._completion_pending = False
        self._run("run_protocol", self._protocol_run_settings(stage), refresh=False)
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
        action = "resume_protocol" if self.last_metadata.get("pipeline_state") == "paused" else "pause_protocol"
        result = self._run(action, refresh=False, notify_success=False)
        if result is None:
            self._render_current_stage()
            return
        stage = self.workflow.current_stage(self.workflow_state)
        if action == "resume_protocol" and stage.pipeline:
            self._show_pipeline_confirmation_notice(stage, self._latest_pipeline_event)
        self._render_current_stage()

    def _pipeline_active(self) -> bool:
        return self.last_metadata.get("pipeline_state") in {"running", "paused", "stopping"}

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
        self._pipeline_confirmation_notice = ""

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
        event = self._latest_pipeline_event
        state = self._pipeline_event_state(event)
        step = self._pipeline_step_label(stage, event)
        if self._pending_pipeline_confirmation() and state == "paused":
            return f"{step}: paused"
        if self._pending_pipeline_confirmation():
            return f"{step}: waiting"
        if state == "completed":
            return "Protocol complete"
        if state == "error":
            return f"{step}: error"
        if state == "paused":
            return f"{step}: paused"
        return (
            f"{step}: step {self._pipeline_step_progress_percent():.0f}% "
            f"/ total {self._pipeline_total_progress_percent():.0f}%"
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

    def _pipeline_notification_text(self, _stage: Stage, event: Any | None) -> str:
        message = ""
        if event is not None:
            message = str(getattr(event, "confirmation_message", "") or "").strip()
        return message or self._pending_pipeline_confirmation()

    def _show_pipeline_confirmation_notice(self, stage: Stage, event: Any | None) -> None:
        if not self._pending_pipeline_confirmation():
            return
        notice = self._pipeline_notification_text(stage, event)
        if notice == self._pipeline_confirmation_notice and self.notification is not None:
            return
        self._pipeline_confirmation_notice = notice
        self._notify(notice, "warning", timeout_ms=0)

    def _poll(self) -> None:
        self._poll_fluidics_plots()
        self._poll_pipeline_events()
        now = time.monotonic()
        if now - self._last_status_poll >= 0.5:
            self._last_status_poll = now
            try:
                result = self.api.run(
                    RunJob(
                        id=f"camera_status_{int(now * 1000)}",
                        engine=self.api.engine.id,
                        action="camera_status",
                    )
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                if message != self._last_poll_error:
                    self._last_poll_error = message
                    self._append_log(f"camera_status: {message}")
                return
            self._last_poll_error = ""
            self.last_result = result
            self.last_metadata = dict(result.metadata)
            self._refresh_runtime_state()
            self._sync_preview_overlay()
            self._stop_recording_on_finished_pipeline()

    def _stop_recording_on_finished_pipeline(self) -> None:
        if not self.last_metadata.get("recording_active"):
            return
        if self.last_metadata.get("pipeline_state") not in {"completed", "error"}:
            return
        self._run("stop_recording", raise_errors=False, refresh=False, notify_success=False)

    def _poll_pipeline_events(self) -> None:
        queue = getattr(self.api.engine, "pipeline_queue", None)
        if queue is None:
            return
        latest = None
        while not queue.empty():
            try:
                latest = queue.get_nowait()
            except Empty:
                break
        if latest is None:
            return
        self._latest_pipeline_event = latest
        stage = self._pipeline_stage()
        if stage.pipeline:
            confirmation = str(getattr(latest, "confirmation_message", "") or "").strip()
            if confirmation:
                self._pipeline_pending_confirmation = confirmation
                if stage.completion_gate == "recording_confirmation":
                    self._sync_run_recording_for_confirmation(confirmation)
                self._show_pipeline_confirmation_notice(stage, latest)
            elif self._pending_pipeline_confirmation():
                self._show_pipeline_confirmation_notice(stage, latest)
            elif self._pipeline_event_state(latest) not in {"paused"}:
                self._clear_pipeline_confirmation()
            self._maybe_notify_tube_switch(stage, latest)
            if self.workflow.current_stage(self.workflow_state).id == stage.id:
                self._refresh_action_box(stage)
            else:
                self._sync_toc()
        if self._pipeline_event_state(latest) == "completed":
            self._schedule_completed_pipeline_stage_finish(stage)

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
        step_index = int(getattr(event, "current_step", -1) or -1)
        if step_index == self._tube_switch_notice_step:
            return
        progress = max(0.0, min(1.0, float(getattr(event, "progress", 0.0) or 0.0)))
        run_volume = float(self.values.get("run_volume_ul") or 0.0)
        oil_flow_ul_min = 250.0
        remaining_s = ((1.0 - progress) * run_volume / oil_flow_ul_min * 60.0) if run_volume > 0 else 0.0
        if 0.0 < remaining_s <= 5.0:
            self._tube_switch_notice_step = step_index
            self._notify("Switch collection tube to waste tube.", "warning", timeout_ms=0)

    def _can_complete_completed_pipeline_stage(self, stage: Stage) -> bool:
        if not stage.pipeline:
            return False
        if self.workflow_state.statuses.get(stage.id) is not StageStatus.ACTIVE:
            return False
        if stage.completion_gate == "recording_confirmation" and not self._runs_completion_confirmed:
            return False
        return True

    def _schedule_completed_pipeline_stage_finish(self, stage: Stage) -> None:
        if self._completion_pending or not self._can_complete_completed_pipeline_stage(stage):
            return
        self._completion_pending = True
        self._refresh_action_box(stage)
        QTimer.singleShot(500, self._finish_completed_pipeline_stage)

    def _finish_completed_pipeline_stage(self) -> None:
        self._completion_pending = False
        self._complete_completed_pipeline_stage()

    def _complete_completed_pipeline_stage(self) -> None:
        stage = self._pipeline_stage()
        if not self._can_complete_completed_pipeline_stage(stage):
            return
        self._latest_pipeline_event = None
        self._pipeline_stage_id = ""
        self._tube_switch_notice_step = -1
        self._clear_pipeline_confirmation()
        self._dismiss_notification()
        current_index = self.workflow_state.index
        stage_index = self.workflow.stages.index(stage)
        self.workflow_state = replace(self.workflow_state, index=stage_index)
        self._complete_current_stage()
        if current_index != stage_index and current_index < len(self.workflow.stages):
            self.workflow_state = replace(self.workflow_state, index=current_index)
            self._render_current_stage()
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
                self.channel_panel.update_modes(self._channel_states())
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
        elapsed = float(self.last_metadata.get("camera_record_elapsed") or 0.0)
        fps = float(self.last_metadata.get("camera_fps") or 0.0)
        frames = int(self.last_metadata.get("camera_recorded_frames") or 0)
        state = "recording" if self.last_metadata.get("camera_recording") else "live"
        self.preview.set_overlay(
            (
                state,
                f"time {elapsed:.1f}s",
                f"fps {fps:.1f}",
                f"frames {frames}",
            )
        )

    def _emergency_stop(self) -> None:
        for action in ("stop_protocol", "stop_recording", "stop_polling", "stop_camera_live"):
            self._run(action, raise_errors=False, refresh=False)
        self._set_status("Stopped", "danger")
        self._notify("Stopped", "danger", timeout_ms=0)
        self._append_log("emergency stop issued")
        self._render_current_stage()

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
        if has_feature(stage, "camera") and self._camera_scene_ready():
            return "done"
        if "fluidics_preflight" in stage.features and self._fluigent_ready():
            return "done"
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
            return self.last_metadata.get("pipeline_state") in {"running", "paused", "stopping"}
        if name == "pipeline_waiting":
            return bool(self._pending_pipeline_confirmation()) and self.last_metadata.get("pipeline_state") == "running"
        return True

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

    def _apply_correction_values(self) -> None:
        self._run("apply_corrections", raise_errors=False, refresh=False)

    def _apply_all_corrections(self) -> None:
        result = self._run("apply_corrections", refresh=False)
        if result is None:
            self._render_current_stage()
            return
        stage = self.workflow.current_stage(self.workflow_state)
        if any(action.action == "apply_corrections" and action.completes for action in stage.actions):
            self._complete_current_stage()
            return
        self._render_current_stage()

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
        try:
            result = self.api.run(
                RunJob(
                    id=f"verify_backend_{int(time.time() * 1000)}",
                    engine=self.api.engine.id,
                    action="verify_backend",
                )
            )
        except Exception as exc:
            metadata = {
                "fluigent_detect_ok": False,
                "fluigent_instrument_count": 0,
                "fluigent_instruments": [],
                "fluigent_device_message": str(exc),
            }
        else:
            metadata = dict(result.metadata)
        self._fluigent_probe = metadata
        self.last_metadata.update(metadata)

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
        if param.kind is ParamKind.INTEGER:
            editor = QSpinBox()
            editor.setRange(
                int(param.minimum if param.minimum is not None else -2_147_483_648),
                int(param.maximum if param.maximum is not None else 2_147_483_647),
            )
            if param.step is not None:
                editor.setSingleStep(max(1, int(param.step)))
            editor.setValue(int(value if value is not None else param.default or 0))
            editor.valueChanged.connect(lambda value, name=param.name: self._set_value(name, value))
            self._param_editors[param.name] = editor
            return editor
        if param.kind is ParamKind.FLOAT:
            editor = QDoubleSpinBox()
            editor.setRange(
                float(param.minimum if param.minimum is not None else -1_000_000_000.0),
                float(param.maximum if param.maximum is not None else 1_000_000_000.0),
            )
            editor.setDecimals(4)
            if param.step is not None:
                editor.setSingleStep(float(param.step))
            editor.setValue(float(value if value is not None else param.default or 0.0))
            editor.valueChanged.connect(lambda value, name=param.name: self._set_value(name, value))
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

    def _set_value(self, name: str, value: Any) -> None:
        param = self._param_by_name(name)
        try:
            self.values[name] = param.validate(value)
        except Exception as exc:
            self._set_status(f"{param.label} rejected", "danger")
            self._notify(str(exc), "danger")
            return
        if name in CAMERA_AUTO_APPLY_PARAMS:
            self._schedule_camera_apply()
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
        self._show_instruction_card(instruction_text(stage, self._guard_value))

    def _set_status(self, text: str, kind: str = "primary") -> None:
        self.status_kind = kind

    def _notify(self, text: str, kind: str = "primary", *, timeout_ms: int = 3500) -> None:
        self._show_notification_card(text, kind=kind, timeout_ms=timeout_ms)

    def _confirm(self, text: str, on_confirm: Callable[[], None]) -> None:
        self._show_notification_card(
            text,
            kind="warning",
            on_confirm=on_confirm,
            timeout_ms=0,
        )

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
            card = self.instruction_card
            self.instruction_card = None
            if self.notification_layout is not None:
                self.notification_layout.removeWidget(card)
            card.blockSignals(True)
            card.close()
            card.deleteLater()
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
        on_confirm: Callable[[], None] | None = None,
        timeout_ms: int = 3500,
    ) -> None:
        if (
            self.notification is not None
            and self._notification_text == text
            and self._notification_kind == kind
            and on_confirm is None
        ):
            return
        if self.notification is not None:
            self._dismiss_notification(restore_instruction=False)
        parent = self.notification_host or self.centralWidget()
        if parent is None:
            return
        self.notification = NotificationCard(parent, text, kind, on_confirm=on_confirm)
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

    def closeEvent(self, event) -> None:
        if self._camera_frame_unsubscribe is not None:
            self._camera_frame_unsubscribe()
            self._camera_frame_unsubscribe = None
        if self._camera_ack_pending:
            self._acknowledge_camera_frame()
        self.timer.stop()
        for action in (
            "stop_protocol",
            "stop_recording",
            "stop_polling",
            "stop_camera_live",
            "disconnect_camera",
            "disconnect_fluidics",
        ):
            try:
                self.api.run(
                    RunJob(
                        id=f"close_{action}_{int(time.time() * 1000)}",
                        engine=self.api.engine.id,
                        action=action,
                    )
                )
            except Exception:
                continue
        super().closeEvent(event)

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

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget is not None:
                widget.deleteLater()
            elif child_layout is not None:
                self._clear_layout(child_layout)


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
        _fit_table_height(self.table)

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
        _fit_table_height(self.table)

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


def _safe_list_value(values: Any, index: int, *, default: Any = 0.0) -> Any:
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
    _fit_table_height(table)
    return table


def _fit_table_height(table: QTableWidget, *, max_rows: int | None = None) -> None:
    height = table.horizontalHeader().height() + table.frameWidth() * 2
    row_count = table.rowCount() if max_rows is None else min(table.rowCount(), max_rows)
    height += sum(table.rowHeight(row) for row in range(row_count))
    table.setFixedHeight(height)


def _short_control_label(label: str) -> str:
    replacements = {
        "Disconnect": "Disconnect",
        "Continue": "Next",
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
