"""JSON definitions and immutable plan review in the standalone desktop."""

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QPlainTextEdit,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from admet.ui import theme as ui
from admet.ui.tables import GridTable, fit_table_height
from admet.core.engine import Param, ParamKind
from admet.engines.acquisition.fluidics.config import STABILITY_DURATION_S, STABILITY_TOLERANCE_UL_MIN
from admet.workflows.control import builtin_document
from admet.workflows.json_protocol import load, loads, template_documents


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
            str(step["number"]),
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
        super().__init__(0, 7)
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

    def fit_contents(self):
        if self._fitting:
            return
        self._fitting = True
        try:
            width = self.viewport().width()
            compact = {0: 48, 1: 88, 2: 80, 3: 130, 5: 64, 6: 88}
            scale = min(1.0, width * 0.6 / sum(compact.values()))
            for column, preferred in compact.items():
                self.setColumnWidth(column, max(24, int(preferred * scale)))
            remaining = width - sum(self.columnWidth(c) for c in compact)
            self.setColumnWidth(4, max(24, remaining))
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


class ProtocolEditor(QWidget):
    """Only the new definition/preview content; transport and logs stay in the window."""

    def __init__(self, window, stage):
        super().__init__()
        self.window = window
        self.stage = stage
        self.backend = window.api
        self.plan = None
        self.dirty = True
        self.saved_name = None
        self.parameter_editors = {}
        self.parameter_table = None
        self._table_digest = None
        self._edit_generation = 0
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.editor = QPlainTextEdit()
        self.editor.setMaximumHeight(180)
        self.editor.setPlaceholderText("Open a saved JSON protocol or paste a definition here.")
        self.builtin = stage.settings_options.get("builtin", "")
        if not self.builtin:
            self.templates = template_documents()
            bar = QHBoxLayout()
            self.library = QComboBox()
            self.library.setMinimumWidth(170)
            bar.addWidget(self.library, 1)
            for label, callback in (("Open", self.open_saved),
                                    ("Import JSON", self.import_json), ("Save JSON", self.save),
                                    ("Edit JSON", self.toggle_editor)):
                button = ui.button(label)
                button.clicked.connect(callback)
                bar.addWidget(button)
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
        self.table.setHorizontalHeaderLabels(["STEP", "UNIT ID", "TYPE", "TARGET", "TRIGGER / ETA", "END", "CONFIRM"])
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
        self.table.currentCellChanged.connect(self.show_step_details)
        self.table.cellClicked.connect(self.show_step_details)
        self.editor.textChanged.connect(self.raw_edited)
        self.update_library(window.protocol_library)

    @property
    def executable(self):
        return bool(self.plan and not self.dirty and self.plan["state"] == "planned")

    def edited(self):
        self._edit_generation += 1
        self._table_digest = None
        self.dirty = True
        self.table.hide()
        self.details_box.hide()
        if self.plan:
            self.summary.setText("Definition changed. Build a new plan before execution.")
        self.window._protocol_changed()

    def raw_edited(self):
        self.edited()
        self.refresh_parameters()

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
        declarations = document.get("parameters", {})
        if not declarations:
            return
        from admet.ui.control import NumericParamEdit

        def make_editor(param):
            widget = NumericParamEdit(param, document["parameter_values"][param.name])
            widget.textEdited.connect(self.edited)
            widget.rejected.connect(self.error)
            widget.committed.connect(lambda value, name=param.name: self.set_parameter(name, value))
            self.parameter_editors[param.name] = widget
            return widget

        params = [Param(name, declaration[0], ParamKind.FLOAT, default=declaration[1], minimum=0)
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

    def toggle_editor(self):
        self.editor.setVisible(not self.editor.isVisible())

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
        if not all([widget.commit() for widget in self.parameter_editors.values()]):
            raise ValueError("Correct invalid parameter values before building or saving")
        return loads(self.editor.toPlainText())

    def update_library(self, entries):
        if self.builtin:
            return
        names = [entry["name"] for entry in entries if not entry.get("error")]
        items = [(name, name) for name in names] + [
            ("@" + name, "Template · " + name.replace("_", " ")) for name in sorted(self.templates)
        ]
        if [key for key, _ in items] == [self.library.itemData(i) for i in range(1, self.library.count())]:
            if self.library.count():
                return
        selected = self.library.currentData()
        self.library.clear()
        self.library.addItem("Select protocol or template…", None)
        for key, label in items:
            self.library.addItem(label, key)
        self.library.setCurrentIndex(max(0, self.library.findData(selected)))

    def open_saved(self):
        name = self.library.currentData()
        if name and name.startswith("@"):
            self.set_document(self.templates[name[1:]])
            self.editor.setVisible(not bool(self.parameter_editors))
            return
        if name:
            self.submit(
                lambda: self.backend.call("list_protocols", {"name": name}),
                lambda result: self.set_document(result["protocol"]),
            )

    def set_document(self, document):
        self.editor.setPlainText(json.dumps(document, indent=2, ensure_ascii=False))

    def import_json(self):
        root = str(self.backend.service.project.path / "protocols") if self.backend.service.project else ""
        path, _ = QFileDialog.getOpenFileName(self, "Open protocol", root, "JSON (*.json)")
        if path:
            try:
                self.set_document(load(path))
                self.editor.hide()
            except Exception as exc:
                self.error(exc)

    def save(self):
        try:
            document = self.document()
        except Exception as exc:
            self.error(exc)
            return

        def saved(_result):
            self.saved_name = document["name"]
            try:
                self.window._save_protocol_order()
            except Exception as exc:
                self.error(exc)
                return
            self.window._append_log("Saved " + document["name"])
            self.window._refresh_protocol_library()

        def replace_saved():
            try:
                if self.document() != document:
                    raise RuntimeError("Definition changed; Save JSON again to review the replacement")
            except Exception as exc:
                self.error(exc)
                return
            self.submit(lambda: self.backend.call("save_protocol", {"protocol": document, "replace": True}), saved)

        def failed(exc):
            if isinstance(exc, FileExistsError):
                self.window._confirm(
                    f"Replace saved definition {document['name']}?",
                    replace_saved,
                )
            else:
                self.error(exc)

        self.window.tasks.submit(
            lambda: self.backend.call("save_protocol", {"protocol": document}), saved, failed,
        )

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
        self.show_plan()
        self.editor.hide()
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

        def started(result):
            if result.get("run_id"):
                self.plan["run_id"] = result["run_id"]
            self.window._pipeline_stage_id = self.stage.id
            self.window._clear_pipeline_confirmation()
            self.result(result)

        self.submit(lambda: self.backend.call("control_protocol", {
            "action": "execute", "plan_id": plan_id, "timeout_s": 0.5,
        }), started)

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
