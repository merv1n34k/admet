"""JSON definitions and immutable plan review in the standalone desktop."""

import json

from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QPlainTextEdit,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from admet.ui import theme as ui
from admet.workflows.control import builtin_document
from admet.workflows.json_protocol import load, loads


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
        else:
            condition = trigger + ": " + json.dumps(params, sort_keys=True)
        condition += "\nETA " + value_text(step["expected_duration_s"], " s")
        condition += " / timeout " + value_text(step["timeout_s"], " s")
        controls = [
            (index, mode, value_text(target, unit))
            for key, mode, unit in (
                ("flow_setpoints_ul_min", "flow", " µL/min"),
                ("pressure_setpoints_mbar", "pressure", " mbar"),
            )
            for index, target in step[key].items()
        ] or [("—", "off", "—")]
        for index, mode, target in controls:
            rows.append([
                str(step["number"]), index, mode, target, condition,
                step["on_complete"], step["confirmation"] or "—",
            ])
    return rows


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
        self._table_digest = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.editor = QPlainTextEdit()
        self.editor.setMaximumHeight(180)
        self.editor.setPlaceholderText("Open a saved JSON protocol or paste a definition here.")
        self.builtin = stage.settings_options.get("builtin", "")
        if not self.builtin:
            bar = QHBoxLayout()
            self.library = QComboBox()
            self.library.setMinimumWidth(170)
            bar.addWidget(self.library, 1)
            for label, callback in (("Open saved", self.open_saved),
                                    ("Import JSON", self.import_json), ("Save JSON", self.save),
                                    ("Edit JSON", self.toggle_editor)):
                button = ui.button(label)
                button.clicked.connect(callback)
                bar.addWidget(button)
            root.addLayout(bar)
            root.addWidget(self.editor)
        else:
            self.editor.hide()
        self.summary = QLabel("Build a plan to review the exact targets before execution.")
        self.summary.setObjectName("StageSummary")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        self.table = QTableWidget(0, 7)
        self.table.setObjectName("RawConfigTable")
        self.table.setHorizontalHeaderLabels(["STEP", "UNIT ID", "TYPE", "TARGET", "TRIGGER / ETA", "END", "CONFIRM"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.setMaximumHeight(280)
        self.table.setWordWrap(True)
        root.addWidget(self.table)
        self.table.hide()
        self.editor.textChanged.connect(self.edited)
        self.update_library(window.protocol_library)

    @property
    def executable(self):
        return bool(self.plan and not self.dirty and self.plan["state"] == "planned")

    def edited(self):
        self.dirty = True
        if self.plan:
            self.summary.setText("Definition changed. Build a new plan before execution.")
        self.window._protocol_changed()

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
        return loads(self.editor.toPlainText())

    def update_library(self, entries):
        if self.builtin:
            return
        names = [entry["name"] for entry in entries if not entry.get("error")]
        if names == [self.library.itemData(i) for i in range(1, self.library.count())]:
            if self.library.count():
                return
        selected = self.library.currentData()
        self.library.clear()
        self.library.addItem("Select saved protocol…", None)
        for name in names:
            self.library.addItem(name, name)
        if selected in names:
            self.library.setCurrentIndex(names.index(selected) + 1)

    def open_saved(self):
        name = self.library.currentData()
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
        try:
            document = self.document()
        except Exception as exc:
            self.error(exc)
            return
        self.submit(
            lambda: self.backend.call("plan_protocol", {
                "operation_id": "run_json_protocol", "settings": {"protocol": document},
            }),
            self.add_plan,
        )

    def add_plan(self, plan):
        self.plan = plan
        self.dirty = False
        self.show_plan()
        self.editor.hide()
        self.window._append_log("Planned " + plan["plan_id"] + " — no actuation")
        self.window._protocol_changed()

    def show_plan(self):
        plan = self.plan
        limits = plan["armed_safety_limits"]["pressure_mbar"]
        if not self.dirty:
            self.summary.setText(
                f"{plan['state']} · {plan['step_count']} steps · ETA {value_text(plan['expected_duration_s'], ' s')} "
                f"+ confirmation waits · pressure trips {json.dumps(limits)} mbar"
                + (f"\nUnmet guards: {', '.join(plan['unmet_guards'])}" if plan["unmet_guards"] else "")
            )
        self.summary.setToolTip(
            "Abort: " + "; ".join(plan["abort_conditions"]) + "\n"
            + "; ".join(plan["warnings"]) + "\n" + plan["digest"]
        )
        if self._table_digest != plan["digest"]:
            self._table_digest = plan["digest"]
            rows = step_rows(plan)
            self.table.setRowCount(len(rows))
            for row, values in enumerate(rows):
                for column, value in enumerate(values):
                    self.table.setItem(row, column, QTableWidgetItem(value))
            self.table.resizeRowsToContents()
            self.table.show()

    def execute(self):
        if not self.executable:
            return
        plan_id = self.plan["plan_id"]

        def started(result):
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
