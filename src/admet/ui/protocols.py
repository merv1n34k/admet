"""JSON definitions and immutable plan review in the standalone desktop."""

import json
import math
import statistics

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView,
    QLineEdit, QPlainTextEdit,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from admet.ui import theme as ui
from admet.ui.tables import GridTable, SummaryLabel, fit_table_height
from admet.core.engine import Param, ParamKind
from admet.engines.acquisition.fluidics.config import STABILITY_DURATION_S, STABILITY_TOLERANCE_UL_MIN
from admet.workflows.control import builtin_document
from admet.workflows.json_protocol import loads, parameter_declarations, parameter_text, template_documents


def value_text(value, unit=""):
    if value is None:
        return "—"
    return f"{value:g}{unit}" if isinstance(value, (float, int)) else str(value)


def plan_summary(plan, label):
    flows = {}
    volumes = {"0": 0.0, "1": 0.0, "2": 0.0}
    elapsed = 0.0
    for step in plan["steps"]:
        if step["confirmation"]:
            for channel, flow in flows.items():
                if flow is None or flow != 0:
                    volumes[channel] = None
        flows.update(step["flow_setpoints_ul_min"])
        flows.update({channel: None for channel in step["pressure_setpoints_mbar"]})
        duration = step["expected_duration_s"] if step["trigger_type"] != "stability" else None
        elapsed = elapsed + duration if elapsed is not None and duration is not None else None
        for channel, flow in flows.items():
            volume = volumes.setdefault(channel, 0.0)
            if duration == 0 or flow == 0:
                continue
            volumes[channel] = (volume + max(0, flow) * duration / 60
                                if volume is not None and flow is not None and duration is not None else None)
        changed = set(step["flow_setpoints_ul_min"]) | set(step["pressure_setpoints_mbar"])
        if step["on_complete"] == "zero":
            flows.update({channel: 0.0 for channel in step["flow_setpoints_ul_min"]})
        elif step["on_complete"] == "revert":
            flows.update({channel: None for channel in changed})
    eta = "—" if elapsed is None else (f"{elapsed / 60:.3g} min" if elapsed >= 60 else f"{elapsed:.3g} s")
    labels = {"0": "Oil", "1": "Cells", "2": "Beads"}
    targets = " · ".join(f"{labels.get(channel, 'Ch ' + channel)}: {value_text(value, ' µL')}"
                         for channel, value in volumes.items())
    count = plan["step_count"]
    return f"{label}: {count} {'step' if count == 1 else 'steps'} · ETA {eta} · {targets}"


# Two-sided 95 % Student t quantiles by degrees of freedom; beyond the table the
# normal value is close enough for the batch counts used here.
_T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
         8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145}


def flow_integral(points, t0, t1):
    """uL between t0 and t1 under kept (t s, uL/min) readings, cut at the exact edges."""
    volume = 0.0
    for (ta, qa), (tb, qb) in zip(points, points[1:]):
        lo, hi = max(ta, t0), min(tb, t1)
        if hi <= lo or tb <= ta:
            continue
        slope = (qb - qa) / (tb - ta)
        volume += (qa + slope * (lo - ta) + qa + slope * (hi - ta)) / 2 * (hi - lo) / 60
    return volume


def step_flow_summary(points, t0, t1, setpoint, *, hold_s=0.5, batches=10):
    """One channel's result for one step.

    Volume covers the whole step. Mean flow covers only its settled part -- once
    the flow has held within max(0.5, 5 %) of the setpoint for HOLD_S -- so the
    ramp up does not drag the average down. Readings ~0.1 s apart are not
    independent, so the 95 % interval comes from batch means: the settled
    readings are split into up to BATCHES consecutive batches and the interval
    is taken from the spread of the batch averages.
    """
    inside = [(t, q) for t, q in points if t0 <= t <= t1]
    band = max(0.5, 0.05 * abs(setpoint))
    settled_at = run_start = None
    for t, q in inside:
        if abs(q - setpoint) <= band:
            run_start = t if run_start is None else run_start
            if t - run_start >= hold_s:
                settled_at = run_start
                break
        else:
            run_start = None
    # Counted from the end of the hold, not its start: the readings that first
    # entered the band are still the tail of the approach and bias the mean.
    values = [q for t, q in inside if settled_at is not None and t >= settled_at + hold_s]
    mean = ci = None
    if len(values) >= 6:
        count = min(batches, len(values) // 3)
        size = len(values) // count
        means = [statistics.fmean(values[i * size:(i + 1) * size]) for i in range(count)]
        mean = statistics.fmean(values)
        ci = _T975.get(count - 1, 2.0) * statistics.stdev(means) / math.sqrt(count)
    return {"volume_ul": flow_integral(points, t0, t1) if len(points) > 1 else None,
            "mean_ul_min": mean, "ci95_ul_min": ci, "settled": settled_at is not None,
            "samples": len(values)}


def summary_text(results):
    """Cell text for the run table, one line per flow-controlled channel."""
    flow, volume = [], []
    for sensor, result in results:
        if result["mean_ul_min"] is None:
            flow.append(f"{sensor}: not settled")
        else:
            flow.append(f"{sensor}: {result['mean_ul_min']:.2f} ± {result['ci95_ul_min']:.2f}")
        volume.append(f"{sensor}: —" if result["volume_ul"] is None else f"{sensor}: {result['volume_ul']:.2f} µL")
    return "\n".join(flow) or "—", "\n".join(volume) or "—"


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
            condition, step["on_complete"], "Before" if step["confirmation"] else "—", "—", "—",
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
        super().__init__(0, 10)
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
        self.table = GridTable(0, 3)
        self.table.setHorizontalHeaderLabels(["Measurement", "Value", "Step / repeat"])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().sectionResized.connect(self.fit)
        root.addWidget(self.table)
        self.hide()

    def preview(self, fields):
        self.run_id = None
        self.render(fields, {})
        self.setVisible(bool(fields))

    def attach(self, run_id):
        try:
            data = self.editor.backend.measurements(run_id)
        except FileNotFoundError:
            return
        except Exception as exc:
            self.editor.error(exc)
            return
        self.run_id = run_id
        self.render(data["fields"], {**data["values"], **self.pending.get(run_id, {})})
        self.show()

    def render(self, fields, values):
        self.cells = {}
        self.table.setRowCount(0)
        self.table.setRowCount(len(fields))
        for row, (key, field) in enumerate(fields.items()):
            label = field["label"] + (f" ({field['unit']})" if field.get("unit") else "")
            self.table.setItem(row, 0, QTableWidgetItem(label))
            self.table.setItem(row, 2, QTableWidgetItem(str(field.get("step", "—"))))
            for column in (0, 2):
                self.table.item(row, column).setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            cell = QLineEdit("" if values.get(key) is None else str(values[key]))
            cell.setPlaceholderText("Not measured")
            cell.setEnabled(self.run_id is not None)
            cell.editingFinished.connect(lambda k=key, c=cell, r=self.run_id: self.changed(k, c, r))
            self.table.setCellWidget(row, 1, cell)
            self.cells[key] = cell
        self.fit()

    def fit(self, *_args):
        self.table.resizeRowsToContents()
        fit_table_height(self.table)

    def changed(self, key, cell, run_id):
        if not run_id:
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
        self.pending.setdefault(run_id, {})[key] = value
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
        self._step_started: dict[int, float] = {}
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
        self.table = PlanTable()
        self.table.setObjectName("RawConfigTable")
        self.table.setHorizontalHeaderLabels([
            "STEP", "STATUS", "UNIT ID", "TYPE", "TARGET", "TRIGGER / ETA", "END", "CONFIRM",
            "FLOW ± 95% CI", "VOLUME",
        ])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        root.addWidget(self.table)
        self.table.hide()
        self.details_box = QWidget()
        detail_layout = QVBoxLayout(self.details_box)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.details = SummaryLabel()
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
            ("@" + name, name.replace("_", " ")) for name in sorted(self.templates)
        ]
        self.library.blockSignals(True)
        self.library.clear()
        self.library.addItem("Select protocol…", None)
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
        self.measurements.preview(plan["normalized_settings"]["protocol"].get("measurements", {}))
        self.show_plan()
        self.editor.setVisible(not bool(self.builtin))
        self.window._append_log("Preview ready — nothing recorded or actuated")
        self.window._protocol_changed()

    def show_plan(self):
        plan = self.plan
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
        self._step_started.clear()
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
        if outcome == "running" and not event.confirmation_message:
            self._step_started.setdefault(row, event.monotonic)
        elif outcome != "running":
            self.show_step_result(row, event.monotonic)
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

    def show_step_result(self, row, ended):
        """Fill a finished step's flow and volume from the readings the plot holds."""
        started = self._step_started.get(row)
        flows = self.plan["steps"][row].get("flow_setpoints_ul_min") or {}
        if started is None or not flows:
            return
        to_plot = self.window.plot_time
        results = [
            (sensor, step_flow_summary(self.window.kept_flow_points(int(sensor)),
                                       to_plot(started), to_plot(ended), float(setpoint)))
            for sensor, setpoint in flows.items()
        ]
        flow_text, volume_text = summary_text(results)
        self.table.item(row, 8).setText(flow_text)
        self.table.item(row, 9).setText(volume_text)
        self.table.resizeRowToContents(row)
        fit_table_height(self.table)

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
            if not self.dirty:  # an edit hid the plan; showing it again would show steps that are out of date
                self.show_plan()
            self.lock_definition(self.plan["state"] == "executing")
