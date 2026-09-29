"""JSON definitions and immutable plan review in the standalone desktop."""

import json
import math

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPlainTextEdit,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from admet.ui import theme as ui
from admet.ui.tables import GridTable, fit_table_height
from admet.core.engine import Param, ParamKind
from admet.engines.acquisition.fluidics.config import STABILITY_DURATION_S, STABILITY_TOLERANCE_UL_MIN
from admet.workflows.control import builtin_document
from admet.workflows.json_protocol import loads, parameter_declarations, parameter_text, template_documents


def value_text(value, unit=""):
    if value is None:
        return "—"
    return f"{value:g}{unit}" if isinstance(value, (float, int)) else str(value)


def step_rows(plan):
    rows = []
    for step in plan["steps"]:
        params = step["trigger_params"]
        trigger = step["trigger_type"]
        if trigger == "volume":
            condition = f"volume ch {params['sensor_index']}: {params['target_volume_ul']:g} µL"
        elif trigger == "time":
            condition = f"time: {params['duration_s']:g} s"
        elif trigger == "stability":
            tolerance = params.get("tolerance_ul_min", STABILITY_TOLERANCE_UL_MIN)
            window = params.get("window_s", STABILITY_DURATION_S)
            condition = (f"stable ch {params['sensor_index']}: ±{tolerance:g} µL/min "
                         f"for {window:g} s")
        else:
            condition = trigger + ": " + json.dumps(params, sort_keys=True)
        condition += " · ETA " + value_text(step["expected_duration_s"], " s")
        controls = [
            (index, mode, value_text(target, unit))
            for key, mode, unit in (
                ("flow_setpoints_ul_min", "flow", " µL/min"),
                ("pressure_setpoints_mbar", "pressure", " mbar"),
            )
            for index, target in step[key].items()
        ]
        gate_only = not controls and trigger == "time" and params["duration_s"] == 0 and step["confirmation"]
        if gate_only:
            condition = "Operator: " + step["name"]
        controls = controls or [("—", "confirm" if gate_only else "off", "—")]
        rows.append([
            str(step["number"]), "pending",
            "\n".join(str(index) for index, _, _ in controls),
            "\n".join(mode for _, mode, _ in controls),
            "\n".join(target for _, _, target in controls),
            condition, step["on_complete"], "Before" if step["confirmation"] else "—",
        ])
    return rows


def step_details(step):
    return (
        f"Step {step['number']} — {step['name']}\n"
        + (f"Confirm before applying targets: {step['confirmation']}\n" if step["confirmation"] else "")
        + f"Trigger: {step['trigger_type']} {json.dumps(step['trigger_params'], sort_keys=True)}\n"
        + f"ETA: {value_text(step['expected_duration_s'], ' s')} · "
        + f"Timeout: {value_text(step['timeout_s'], ' s')} (operator waiting excluded) · "
        + f"End: {step['on_complete']}"
    )


class PlanTable(GridTable):
    def __init__(self):
        super().__init__(0, 8)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.verticalHeader().hide()
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.horizontalHeader().setMinimumSectionSize(24)
        self.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self._fitting = False

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.clearSelection()
            self.setCurrentCell(-1, -1)
            self.clearFocus()
            event.accept()
            return
        super().keyPressEvent(event)

    def fit_contents(self):
        if self._fitting:
            return
        self._fitting = True
        try:
            width = self.viewport().width()
            compact = {0: 48, 1: 108, 2: 78, 3: 70, 4: 130, 6: 64, 7: 78}
            scale = min(1.0, width * 0.6 / sum(compact.values()))
            for column, preferred in compact.items():
                self.setColumnWidth(column, max(24, int(preferred * scale)))
            remaining = width - sum(self.columnWidth(c) for c in compact)
            self.setColumnWidth(5, max(24, remaining))
            self.resizeRowsToContents()
            fit_table_height(self)
        finally:
            self._fitting = False

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.fit_contents()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.fit_contents()


class Measurements(QWidget):
    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        self.run_id = None
        self.pending = {}
        self.cells = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(QLabel("Measurements — recorded only; Calculate processes them later"))
        self.runs = QComboBox()
        self.runs.currentIndexChanged.connect(self.select_run)
        root.addWidget(self.runs)
        self.table = GridTable(0, 3)
        self.table.setHorizontalHeaderLabels(["Measurement", "Value", "Step / repeat"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        root.addWidget(self.table)
        self.fields = {}
        self.hide()

    def preview(self, fields):
        self.fields = fields
        self.run_id = None
        self.runs.blockSignals(True)
        self.runs.clear()
        self.runs.addItem("Next run — enabled on Execute", None)
        for run in self.editor.backend.measurement_runs():
            self.runs.addItem(f"{run['name']} · {run['at']} · {run['run_id'][-8:]}", run["run_id"])
        self.runs.blockSignals(False)
        self.render(fields, {})
        self.setVisible(bool(fields) or self.runs.count() > 1)

    def select_run(self):
        run_id = self.runs.currentData()
        if run_id:
            self.attach(run_id)
        else:
            self.run_id = None
            self.render(self.fields, {})

    def attach(self, run_id):
        try:
            data = self.editor.backend.measurements(run_id)
        except FileNotFoundError:
            return
        except Exception as exc:
            self.editor.error(exc)
            return
        self.run_id = run_id
        self.runs.blockSignals(True)
        index = self.runs.findData(run_id)
        if index < 0:
            self.runs.addItem("Run " + run_id[-8:], run_id)
            index = self.runs.count() - 1
        self.runs.setCurrentIndex(index)
        self.runs.blockSignals(False)
        self.render(data["fields"], {**data["values"], **self.pending.get(run_id, {})})
        self.show()

    def render(self, fields, values):
        self.cells = {}
        self.table.setRowCount(0)
        self.table.setRowCount(len(fields))
        for row, (key, field) in enumerate(fields.items()):
            self.table.setItem(row, 0, QTableWidgetItem(field["label"]))
            self.table.setItem(row, 2, QTableWidgetItem(str(field.get("step", "—"))))
            cell = QLineEdit("" if values.get(key) is None else str(values[key]))
            cell.setPlaceholderText("Not measured")
            cell.setEnabled(self.run_id is not None)
            cell.editingFinished.connect(lambda k=key, c=cell: self.changed(k, c))
            self.table.setCellWidget(row, 1, cell)
            self.cells[key] = cell
        self.table.resizeRowsToContents()
        fit_table_height(self.table)

    def changed(self, key, cell):
        if not self.run_id:
            return
        try:
            value = float(cell.text()) if cell.text().strip() else None
            if value is not None and not math.isfinite(value):
                raise ValueError("non-finite")
        except ValueError:
            cell.setStyleSheet("border: 1px solid #8b2b2b")
            self.editor.error(ValueError("Measurement must be a finite number or empty (not measured)"))
            return
        cell.setStyleSheet("")
        self.pending.setdefault(self.run_id, {})[key] = value
        self.editor.window._measurement_save_timer.start(200)

    def flush(self):
        for run_id, changes in list(self.pending.items()):
            self.editor.backend.measurements(run_id, changes)
            del self.pending[run_id]


class ProtocolEditor(QWidget):
    """Only the new definition/preview content; transport and logs stay in the window."""

    def __init__(self, window, stage):
        super().__init__()
        self.window = window
        self.stage = stage
        self.backend = window.api
        self.plan = None
        self.dirty = True
        self.parameter_editors = {}
        self.parameter_table = None
        self._table_digest = None
        self._edit_generation = 0
        self._executing = False
        self._last_sequence = 0
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.editor = QPlainTextEdit()
        self.editor.setMaximumHeight(180)
        self.editor.setPlaceholderText("Select a template or edit the JSON definition here.")
        self.builtin = stage.settings_options.get("builtin", "")
        if not self.builtin:
            self.templates = template_documents()
            bar = QHBoxLayout()
            self.library = QComboBox()
            self.library.setMinimumWidth(170)
            bar.addWidget(self.library, 1)
            self.library.currentIndexChanged.connect(self.open_template)
            root.addLayout(bar)
            self.parameter_box = QVBoxLayout()
            root.addLayout(self.parameter_box)
            root.addWidget(self.editor)
        else:
            self.editor.hide()
        self.summary = QLabel("Build a plan to review the exact targets before execution.")
        self.summary.setObjectName("StageSummary")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        self.table = PlanTable()
        self.table.setObjectName("RawConfigTable")
        self.table.setHorizontalHeaderLabels(["STEP", "STATUS", "UNIT ID", "TYPE", "TARGET", "TRIGGER / ETA", "END", "CONFIRM"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        root.addWidget(self.table)
        self.table.hide()
        self.details_box = QWidget()
        detail_layout = QVBoxLayout(self.details_box)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        detail_layout.addWidget(self.details)
        hide_details = ui.button("Hide details")
        hide_details.clicked.connect(self.details_box.hide)
        detail_layout.addWidget(hide_details, alignment=Qt.AlignmentFlag.AlignLeft)
        root.addWidget(self.details_box)
        self.details_box.hide()
        self.measurements = Measurements(self)
        root.addWidget(self.measurements)
        self.table.currentCellChanged.connect(self.show_step_details)
        self.table.cellClicked.connect(self.show_step_details)
        self.editor.textChanged.connect(self.raw_edited)
        self.populate_templates()
        if not self.builtin:
            self.measurements.preview({})

    @property
    def executable(self):
        return bool(self.plan and not self.dirty and self.plan["state"] == "planned")

    def edited(self):
        self._edit_generation += 1
        self._table_digest = None
        self.dirty = True
        self.editor.setVisible(not self.builtin and not self._executing)
        self.table.hide()
        self.details_box.hide()
        if self.plan:
            self.summary.setText("Definition changed. Build a new plan before execution.")
        self.window._protocol_changed()

    def raw_edited(self):
        self.edited()
        self.refresh_parameters()
        try:
            fields = loads(self.editor.toPlainText()).get("measurements", {})
        except (ValueError, TypeError):
            fields = {}
        self.measurements.preview(fields)

    def refresh_parameters(self):
        if self.builtin:
            return
        if self.parameter_table is not None:
            self.parameter_box.removeWidget(self.parameter_table)
            self.parameter_table.deleteLater()
            self.parameter_table = None
        self.parameter_editors = {}
        try:
            document = loads(self.editor.toPlainText())
        except (ValueError, TypeError):
            return
        declarations = parameter_declarations(document)
        if not declarations:
            return
        from admet.ui.control import NumericParamEdit

        def make_editor(param):
            value = document["parameter_values"][param.name]
            if param.kind == ParamKind.FLOAT:
                widget = NumericParamEdit(param, value)
                widget.textEdited.connect(self.edited)
                widget.rejected.connect(self.error)
                widget.committed.connect(lambda value, name=param.name: self.set_parameter(name, value))
            elif param.kind == ParamKind.BOOLEAN:
                widget = QCheckBox()
                widget.setChecked(value)
                widget.toggled.connect(lambda value, name=param.name: self.set_parameter(name, value))
            elif param.kind == ParamKind.CHOICE:
                widget = QComboBox()
                for index, option in enumerate(declarations[param.name]["options"]):
                    widget.addItem(parameter_text(option), option)
                    if type(option) is type(value) and option == value:
                        widget.setCurrentIndex(index)
                widget.currentIndexChanged.connect(
                    lambda _index, name=param.name, widget=widget: self.set_parameter(name, widget.currentData()))
            else:
                widget = QLineEdit(value)
                widget.setMaxLength(4096)
                widget.textEdited.connect(self.edited)
                widget.editingFinished.connect(
                    lambda name=param.name, widget=widget: self.set_parameter(name, widget.text()))
            self.parameter_editors[param.name] = widget
            widget.setEnabled(not self._executing)
            return widget

        kinds = {"number": ParamKind.FLOAT, "text": ParamKind.TEXT, "boolean": ParamKind.BOOLEAN, "choice": ParamKind.CHOICE}
        params = [Param(name, declaration["label"], kinds[declaration["type"]], default=declaration["default"],
                        minimum=declaration.get("min"), maximum=declaration.get("max"))
                  for name, declaration in declarations.items()]
        self.parameter_table = self.window._param_table(params, editor_factory=make_editor)
        self.parameter_box.addWidget(self.parameter_table)

    def set_parameter(self, name, value):
        # Keep other pending cells intact; validation of all resolved steps happens on Build/Save.
        document = json.loads(self.editor.toPlainText())
        document.setdefault("parameter_values", {})[name] = value
        self.editor.blockSignals(True)
        self.editor.setPlainText(json.dumps(document, indent=2, ensure_ascii=False))
        self.editor.blockSignals(False)
        self.edited()

    def error(self, exc):
        self.window._notify(str(exc), "danger", timeout_ms=0)
        self.window._append_log(str(exc))

    def submit(self, work, success=None):
        return self.window.tasks.submit(work, success or self.result, self.error)

    def result(self, result):
        yielded = result.get("yield", {})
        self.window._append_log(yielded.get("reason") or result.get("state") or "Protocol action complete")
        self.window._poll_pipeline_events()
        self.window._protocol_changed()

    def document(self):
        if self.builtin:
            return builtin_document(self.builtin, self.window.values)
        from admet.ui.control import NumericParamEdit

        if not all([widget.commit() for widget in self.parameter_editors.values() if isinstance(widget, NumericParamEdit)]):
            raise ValueError("Correct invalid parameter values before building or saving")
        for name, widget in self.parameter_editors.items():
            if isinstance(widget, QLineEdit) and not isinstance(widget, NumericParamEdit):
                if json.loads(self.editor.toPlainText()).get("parameter_values", {}).get(name) != widget.text():
                    self.set_parameter(name, widget.text())
        return loads(self.editor.toPlainText())

    def populate_templates(self):
        if self.builtin:
            return
        items = [
            ("@" + name, "Template · " + name.replace("_", " ")) for name in sorted(self.templates)
        ]
        self.library.blockSignals(True)
        self.library.clear()
        self.library.addItem("Select template…", None)
        for key, label in items:
            self.library.addItem(label, key)
        self.library.blockSignals(False)

    def open_template(self):
        name = self.library.currentData()
        if name and name.startswith("@"):
            self.set_document(self.templates[name[1:]])

    def set_document(self, document):
        self.editor.setPlainText(json.dumps(document, indent=2, ensure_ascii=False))

    def build_plan(self):
        if self.builtin and not self.window._commit_numeric_settings(self.stage):
            return
        try:
            document = self.document()
        except Exception as exc:
            self.error(exc)
            return
        generation = self._edit_generation

        def received(plan):
            if generation == self._edit_generation:
                self.add_plan(plan)

        self.submit(lambda: self.backend.preview(self.stage.id, document), received)

    def add_plan(self, plan):
        self.plan = plan
        self.dirty = False
        self._table_digest = None
        self.show_plan()
        self.editor.setVisible(not bool(self.builtin))
        self.window._append_log("Preview ready — nothing recorded or actuated")
        self.window._protocol_changed()

    def show_plan(self):
        plan = self.plan
        limits = plan["armed_safety_limits"]["pressure_mbar"]
        trips = f"{json.dumps(limits)} mbar" if limits else "Off"
        if not self.dirty:
            self.summary.setText(
                f"{plan['state']} · {plan['step_count']} steps · ETA {value_text(plan['expected_duration_s'], ' s')} "
                f"+ confirmation waits · pressure trips {trips}"
                + (f"\nUnmet guards: {', '.join(plan['unmet_guards'])}" if plan["unmet_guards"] else "")
            )
        self.summary.setToolTip(
            "Abort: " + "; ".join(plan["abort_conditions"]) + "\n"
            + "; ".join(plan["warnings"])
        )
        if self._table_digest != plan["digest"]:
            self._table_digest = plan["digest"]
            rows = step_rows(plan)
            self.table.setRowCount(len(rows))
            for row, values in enumerate(rows):
                for column, value in enumerate(values):
                    self.table.setItem(row, column, QTableWidgetItem(value))
                    self.table.item(row, column).setToolTip(step_details(plan["steps"][row]))
                    self.table.item(row, column).setTextAlignment(
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    )
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
            self.details_box.hide()
            self.table.show()
            self.table.fit_contents()

    def show_step_details(self, row, *_args):
        if self.plan and 0 <= row < len(self.plan["steps"]):
            self.details.setText(step_details(self.plan["steps"][row]))
            self.details_box.show()
        else:
            self.details_box.hide()

    def execute(self):
        if not self.executable:
            return
        plan_id = self.plan["plan_id"]
        self.window._poll_pipeline_events()
        self.window._pipeline_stage_id = self.stage.id
        self.lock_definition(True)

        def started(result):
            if result.get("run_id"):
                self.plan["run_id"] = result["run_id"]
                self.measurements.attach(result["run_id"])
            self.window._pipeline_stage_id = self.stage.id
            self.window._clear_pipeline_confirmation()
            self.result(result)

        def failed(exc):
            self.lock_definition(False)
            self.editor.setVisible(not bool(self.builtin))
            self.error(exc)

        self.window.tasks.submit(lambda: self.backend.call("control_protocol", {
            "action": "execute", "plan_id": plan_id, "timeout_s": 0.5,
        }), started, failed)

    def lock_definition(self, active):
        self._executing = active
        self.editor.setReadOnly(active)
        if active:
            self.editor.hide()
        if not self.builtin:
            self.library.setEnabled(not active)
        for widget in self.parameter_editors.values():
            widget.setEnabled(not active)

    def pipeline_event(self, event):
        if not self.plan or event.sequence <= self._last_sequence:
            return
        self._last_sequence = event.sequence
        state, outcome = str(event.state), str(event.outcome)
        self.lock_definition(state in {"running", "paused", "stopping"})
        row = event.current_step
        if not event.step_name or not 0 <= row < self.table.rowCount():
            return
        if outcome != "running":
            status = outcome.replace("_", " ")
        elif state == "paused":
            status = "paused"
        elif event.confirmation_message:
            status = "confirm"
        else:
            status = f"running {event.progress:.0%}"
        color = ("#fce3e3" if outcome in {"error", "timed_out", "cancelled"} else
                 "#e0f1e9" if outcome == "completed" else
                 "#edf3f7" if outcome == "skipped" else
                 "#fff0c2" if status in {"paused", "confirm"} else "#dceefa")
        if self.table.item(row, 1).text() != status:
            self.table.item(row, 1).setText(status)
            self.table.resizeRowToContents(row)
            fit_table_height(self.table)
        for column in range(self.table.columnCount()):
            item = self.table.item(row, column)
            item.setBackground(QColor(color))
            item.setForeground(QColor("#16212b"))

    def control(self, action):
        def finished(result):
            self.window._clear_pipeline_confirmation()
            self.result(result)
        self.submit(
            lambda: self.backend.call("control_protocol", {"action": action, "timeout_s": 0.5}),
            finished,
        )

    def update_plan(self, plans):
        if self.plan:
            self.plan = next((p for p in plans if p["plan_id"] == self.plan["plan_id"]), self.plan)
            self.show_plan()
            self.lock_definition(self.plan["state"] == "executing")
