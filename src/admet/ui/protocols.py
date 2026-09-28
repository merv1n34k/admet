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


class PlanTable(GridTable):
    def __init__(self):
        super().__init__(0, 7)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.verticalHeader().hide()
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.horizontalHeader().setMinimumSectionSize(24)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self._fitting = False

    def fit_contents(self):
        if self._fitting:
            return
        self._fitting = True
        try:
            width = self.viewport().width()
            compact = {0: 48, 1: 64, 2: 76, 3: 108, 5: 72}
            scale = min(1.0, width * 0.6 / sum(compact.values()))
            for column, preferred in compact.items():
                self.setColumnWidth(column, max(24, int(preferred * scale)))
            remaining = width - sum(self.columnWidth(c) for c in compact)
            self.setColumnWidth(4, max(24, remaining * 45 // 100))
            self.setColumnWidth(6, max(24, remaining - self.columnWidth(4)))
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
        self._table_digest = None
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
            self.editor.show()
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
            self.table.show()
            self.table.fit_contents()

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
