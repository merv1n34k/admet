from __future__ import annotations

import signal
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from admet.core.api import AdmetAPI
from admet.core.engine import Param, ParamKind
from admet.core.run import RunJob
from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNELS
from admet.ui import theme as ui
from admet.ui.control_runtime import ControlSessionMixin
from admet.ui.presenter import FieldVM, ScreenModel, SurfaceVM, build_screen
from admet.ui.project import create_project, save_project, suggested_project_path
from admet.ui.theme import Theme
from admet.ui.window import WindowController
from admet.workflows import Stage, StageStatus, create_control_workflow


def run_control_app(api: AdmetAPI, argv: list[str] | None = None) -> int:
    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(argv if argv is not None else sys.argv[:1])
    app.setStyleSheet(ui.stylesheet())

    window = ControlWindow(api)
    window.show()
    if not owns_app:
        return 0

    interrupted = False
    previous_sigint = signal.getsignal(signal.SIGINT)

    def handle_sigint(_signum, _frame) -> None:
        nonlocal interrupted
        interrupted = True
        QTimer.singleShot(0, window.close)

    signal.signal(signal.SIGINT, handle_sigint)
    try:
        app.exec()
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
    return 130 if interrupted else 0


class ControlWindow(ControlSessionMixin, QMainWindow):
    def __init__(self, api: AdmetAPI) -> None:
        super().__init__()
        self.api = api
        self.workflow = create_control_workflow()
        self.workflow_state = self.workflow.initial_state()
        self.values = self.api.settings.defaults()
        self.last_result = None
        self.last_metadata: dict[str, Any] = {}
        self.project_path: Path | None = None
        self.project_badge: QLabel | None = None
        self.status_kind = "primary"
        self.status_text = "Ready"
        self._window_controller = WindowController()
        self._mounted_signature: tuple[Any, ...] | None = None
        self._latest_pipeline_event: Any | None = None
        self._runs_completion_confirmed = False
        self._completion_pending = False
        self._control_recording_dir: Path | None = None
        self._log_lines: list[str] = []
        self.setWindowTitle("admet control")
        self.resize(1180, 820)
        self._render_current_stage()

    def value_for(self, field: Param) -> Any:
        return self.values.get(field.name, field.default)

    def button_enabled(self, command: str) -> bool:
        return True

    def button_active(self, command: str) -> bool:
        return False

    def instructions_for(self, stage: Stage) -> tuple[str, ...]:
        if stage.instructions:
            return stage.instructions
        if stage.description:
            return (stage.description,)
        return ()

    def _render_current_stage(self) -> None:
        screen = build_screen(self.workflow, self.workflow_state, self)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        self._render_topbar(layout)
        body = QHBoxLayout()
        body.setSpacing(10)
        layout.addLayout(body, 1)
        self._render_steps(body, screen)
        self._render_content(body, screen)
        self.setCentralWidget(root)

    def _render_topbar(self, layout: QVBoxLayout) -> None:
        row = QHBoxLayout()
        self.project_badge = QLabel(self._project_text())
        self.project_badge.setObjectName("MutedText")
        row.addWidget(self.project_badge)
        row.addStretch()
        for label, handler in (
            ("New Project", self._new_project),
            ("Save Project", self._save_project),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            row.addWidget(button)
        layout.addLayout(row)

    def _render_steps(self, body: QHBoxLayout, screen: ScreenModel) -> None:
        rail = QWidget()
        rail.setFixedWidth(250)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(0, 0, 0, 0)
        rail_layout.setSpacing(4)
        for index, step in enumerate(screen.steps):
            button = QPushButton(step.label)
            button.setCheckable(True)
            button.setChecked(step.current)
            button.clicked.connect(lambda _checked=False, i=index: self._activate(i))
            rail_layout.addWidget(button)
        rail_layout.addStretch()
        body.addWidget(rail)

    def _render_content(self, body: QHBoxLayout, screen: ScreenModel) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(8)
        layout.addWidget(_title(screen.title))
        for instruction in screen.instructions:
            label = QLabel(instruction)
            label.setWordWrap(True)
            label.setObjectName("MutedText")
            layout.addWidget(label)
        self._render_buttons(layout, screen)
        self._render_fields(layout, screen.fields)
        for surface in screen.surfaces:
            self._render_surface(layout, surface)
        layout.addWidget(_title("Action Log"))
        for line in self._log_lines[-8:]:
            layout.addWidget(QLabel(line))
        layout.addStretch()
        scroll.setWidget(content)
        body.addWidget(scroll, 1)

    def _render_buttons(self, layout: QVBoxLayout, screen: ScreenModel) -> None:
        row = QHBoxLayout()
        for button in screen.buttons:
            widget = QPushButton(button.label)
            widget.setEnabled(button.enabled)
            widget.clicked.connect(lambda _checked=False, command=button.command: self._handle_command(command))
            row.addWidget(widget)
        row.addStretch()
        layout.addLayout(row)

    def _render_fields(self, layout: QVBoxLayout, fields: tuple[FieldVM, ...]) -> None:
        if not fields:
            return
        for field in fields:
            row = QHBoxLayout()
            row.addWidget(QLabel(field.label))
            row.addWidget(self._field_widget(field), 1)
            layout.addLayout(row)

    def _render_surface(self, layout: QVBoxLayout, surface: SurfaceVM) -> None:
        layout.addWidget(_title(surface.title or surface.kind))
        self._render_fields(layout, surface.fields)
        if surface.kind == "camera":
            preview = QLabel("Camera preview surface")
            preview.setMinimumHeight(260)
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setStyleSheet(f"background:{Theme.BG_RAISED}; border:1px solid {Theme.BORDER_COOL};")
            layout.addWidget(preview)

    def _field_widget(self, field: FieldVM) -> QWidget:
        value = field.value
        if field.kind is ParamKind.BOOLEAN:
            widget = QCheckBox()
            widget.setChecked(bool(value))
            widget.stateChanged.connect(lambda _state, name=field.name, w=widget: self._set_value(name, w.isChecked()))
            return widget
        if field.kind is ParamKind.CHOICE:
            widget = QComboBox()
            for option in field.options:
                widget.addItem(option.label, option.value)
            index = widget.findData(value)
            if index >= 0:
                widget.setCurrentIndex(index)
            widget.currentIndexChanged.connect(lambda _index, name=field.name, w=widget: self._set_value(name, w.currentData()))
            return widget
        if field.kind is ParamKind.INTEGER:
            widget = QSpinBox()
            widget.setRange(int(field.minimum or -2_147_483_648), int(field.maximum or 2_147_483_647))
            widget.setValue(int(value if value is not None else 0))
            widget.valueChanged.connect(lambda next_value, name=field.name: self._set_value(name, next_value))
            return widget
        if field.kind is ParamKind.FLOAT:
            widget = QDoubleSpinBox()
            widget.setRange(float(field.minimum or -1_000_000_000.0), float(field.maximum or 1_000_000_000.0))
            widget.setDecimals(4)
            widget.setValue(float(value if value is not None else 0.0))
            widget.valueChanged.connect(lambda next_value, name=field.name: self._set_value(name, next_value))
            return widget
        widget = QLineEdit("" if value is None else str(value))
        widget.textChanged.connect(lambda text, name=field.name: self._set_value(name, text))
        return widget

    def _handle_command(self, command: str) -> None:
        if command == "back":
            self.workflow_state = self.workflow.rewind(self.workflow_state)
        elif command == "skip":
            self.workflow_state = self.workflow.skip_current(self.workflow_state)
        elif command in {"complete", "advance"}:
            self._complete_current_stage()
        else:
            self._run(command)
        self._render_current_stage()

    def _run(self, action: str) -> Any:
        job = RunJob(
            id=f"control-{action}",
            engine=self.api.engine.id,
            action=action,
            settings=self._action_payload(action),
        )
        result = self.api.run(job)
        self.last_result = result
        self.last_metadata = dict(result.metadata)
        recording = result.metadata.get("recording")
        if isinstance(recording, dict):
            self._store_recording_artifact(recording)
        self._append_log(f"{action}: ok")
        return result

    def _set_value(self, name: str, value: Any) -> None:
        self.values[name] = self._param_by_name(name).validate(value)

    def _param_by_name(self, name: str) -> Param:
        for param in self.api.settings.params:
            if param.name == name:
                return param
        for stage in self.workflow.stages:
            for param in stage.settings.params:
                if param.name == name:
                    return param
            for surface in stage.surfaces:
                for param in surface.settings.params:
                    if param.name == name:
                        return param
        raise KeyError(name)

    def _action_payload(self, action: str) -> dict[str, Any]:
        action_params: tuple[str, ...] = ()
        for spec in self.api.engine.actions:
            if spec.id == action:
                action_params = spec.params
                break
        return {name: self.values.get(name) for name in action_params}

    def _activate(self, index: int) -> None:
        statuses = dict(self.workflow_state.statuses)
        current = self.workflow.current_stage(self.workflow_state)
        if statuses.get(current.id) is StageStatus.ACTIVE:
            statuses[current.id] = StageStatus.PENDING
        next_stage = self.workflow.stages[index]
        if statuses.get(next_stage.id) is StageStatus.PENDING:
            statuses[next_stage.id] = StageStatus.ACTIVE
        self.workflow_state = replace(self.workflow_state, index=index, statuses=statuses)
        self._render_current_stage()

    def _complete_current_stage(self) -> None:
        self.workflow_state = self.workflow.complete_current(self.workflow_state, confirmed=True)

    def _new_project(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "New admet project",
            str(suggested_project_path()),
            "admet projects (*.admetp)",
        )
        if not path:
            return
        try:
            project = create_project(path)
        except Exception as exc:
            self._set_status("Project create failed", "danger")
            self._notify(f"Project create failed: {exc}", "danger", timeout_ms=0)
            return
        self.project_path = project.path
        self.api.session = project.session
        self.api.workdir = str(self.project_path)
        self._control_recording_dir = None
        self._sync_project_badge()
        self._set_status("Project created", "success")
        self._notify("Project created", "success")
        self._append_log(f"project: created {self.project_path}")
        self._render_current_stage()

    def _save_project(self) -> None:
        if self.project_path is not None and self.api.session is not None:
            project = save_project(self.project_path, self.api.session)
            self.project_path = project.path
            self.api.session = project.session
            self.api.workdir = str(project.path)
            self._append_log(f"project: saved {project.path}")

    def _project_text(self) -> str:
        if self.api.session is None:
            return "Project: none"
        return f"Project: {self.api.session.project_id}.admetp"

    def _sync_project_badge(self) -> None:
        if self.project_badge is not None:
            self.project_badge.setText(self._project_text())

    def _set_status(self, message: str, kind: str) -> None:
        self.status_text = message
        self.status_kind = kind

    def _notify(self, message: str, kind: str, *, timeout_ms: int = 2500) -> None:
        self._set_status(message, kind)

    def _append_log(self, message: str) -> None:
        self._log_lines.append(message)

    def _collapsed_params(self, stage: Stage, full_params: list[Param]) -> list[Param]:
        if stage.id in {"corrections"}:
            return self._correction_primary_params(full_params)
        if len(full_params) > 6:
            return full_params[:6]
        return full_params

    def _ordered_params(self, stage: Stage, params: list[Param]) -> list[Param]:
        if stage.id != "corrections":
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

def _title(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"font-weight:650; color:{Theme.TEXT_WHITE};")
    return label
