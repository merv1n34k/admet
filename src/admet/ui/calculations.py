"""Saved-run calculations, with no instrument actions."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QScrollArea, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from admet.ui import theme as ui
from admet.ui.tables import GridTable, fit_table_height
from admet.ui.tasks import Tasks
from admet.workflows.calculations import (
    CALCULATIONS, calculate_run, recorded_runs, result_view, saved_results,
    available_calculations, calculation_readiness,
)

GOOD = {"usable", "consistent", "complete"}


def _label(text, name=None, *, wrap=True):
    label = QLabel(str(text))
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if name:
        label.setObjectName(name)
    return label


class ResultView(QScrollArea):
    """A saved result as headed sections: a few facts, its tables, then its notes."""

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumHeight(260)
        self.clear()

    def clear(self):
        self.show_message("")

    def show_message(self, text):
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.addWidget(_label(text, "MutedText"))
        layout.addStretch(1)
        self.setWidget(body)

    def display(self, view):
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setSpacing(ui.spacing("default"))
        if view["outdated"]:
            layout.addWidget(_label("Outdated: the saved inputs have changed; calculate again.", "ProtocolConfirmLabel"))
        for section in view["sections"]:
            heading = QHBoxLayout()
            heading.addWidget(_label(section["title"], "PanelTitle", wrap=False))
            if section["status"]:
                heading.addWidget(_label(section["status"], "VerdictPass" if section["status"] in GOOD else "VerdictFail",
                                         wrap=False))
            heading.addStretch(1)
            layout.addLayout(heading)
            if section["facts"]:
                facts = QGridLayout()
                facts.setColumnStretch(1, 1)
                for row, (name, value) in enumerate(section["facts"]):
                    facts.addWidget(_label(name, "FieldLabel", wrap=False), row, 0)
                    facts.addWidget(_label(value), row, 1)
                layout.addLayout(facts)
            for table in section["tables"]:
                layout.addWidget(_label(table["title"], "FieldLabel"))
                layout.addWidget(self._table(table))
            for issue in section["notes"]:
                layout.addWidget(_label(issue, "ProtocolConfirmLabel"))
            for warning in section["warnings"]:
                layout.addWidget(_label(f"Warning: {warning}", "MutedText"))
        if view["note"]:
            layout.addWidget(_label(view["note"], "MutedText"))
        layout.addWidget(_label(f"Saved: {view['saved']}", "MutedText"))
        layout.addStretch(1)
        self.setWidget(body)

    @staticmethod
    def _table(spec):
        table = GridTable(len(spec["rows"]), len(spec["headers"]))
        table.setObjectName("RawConfigTable")
        table.setHorizontalHeaderLabels(spec["headers"])
        table.verticalHeader().hide()
        # Numbers take what they need; the last column, usually a note, takes the rest.
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setEditTriggers(GridTable.EditTrigger.NoEditTriggers)
        for row, values in enumerate(spec["rows"]):
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if row == spec["highlight"]:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                table.setItem(row, column, item)
        fit_table_height(table)
        return table


class CalculationsPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.project = None
        self.tasks = Tasks(self)
        self._generation = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.calculation = QComboBox()
        for key, entry in CALCULATIONS.items():
            self.calculation.addItem(entry["label"], key)
        row.addWidget(QLabel("Calculation"))
        row.addWidget(self.calculation, 1)
        self.calculate_button = ui.button("Calculate")
        self.calculate_button.clicked.connect(self.calculate)
        row.addWidget(self.calculate_button)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Recorded run"))
        self.runs = QComboBox()
        self.runs.setMinimumContentsLength(24)
        self.runs.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        row.addWidget(self.runs, 1)
        self.refresh_button = ui.button("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.description = QLabel()
        self.description.setWordWrap(True)
        layout.addWidget(self.description)
        self.references = {}
        self.reference_layout = QVBoxLayout()
        layout.addLayout(self.reference_layout)
        layout.addWidget(QLabel("Saved results for this run"))
        self.history = QComboBox()
        layout.addWidget(self.history)
        self.status = QLabel("Open a project to load its recordings.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.output = ResultView()
        layout.addWidget(self.output, 1)
        self.calculation.currentIndexChanged.connect(self.describe)
        self.runs.currentIndexChanged.connect(self.load_history)
        self.history.currentIndexChanged.connect(self.show_result)
        self.describe()
        self.calculate_button.setEnabled(False)

    def set_project(self, project):
        if self.project == project:
            return
        self._generation += 1
        self.project = project
        self.runs.clear()
        self.history.clear()
        self.output.clear()
        self.refresh()

    def describe(self, *_args):
        if self.calculation.currentData() is None:
            return
        entry = CALCULATIONS[self.calculation.currentData()]
        self.description.setText(entry["description"] + " Reads saved files only; never controls hardware.")
        while self.reference_layout.count():
            item = self.reference_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.references = {}
        for role, kind in entry.get("references", {}).items():
            box = QComboBox()
            box.addItem(f"{role}: no reference (use recorded inputs)", None)
            for run in recorded_runs(self.project):
                for result in saved_results(run["directory"]):
                    if (result["calculation_id"] == kind and not result["outdated"]
                            and result["result"].get("status") in {"usable", "consistent", "complete"}):
                        box.addItem(f"{role}: {run['name']} · {result['created_at']}", result["path"])
            box.currentIndexChanged.connect(self.ready)
            self.reference_layout.addWidget(box)
            self.references[role] = box
        self.ready()

    def reference_values(self):
        return {role: box.currentData() for role, box in self.references.items() if box.currentData()}

    def ready(self, *_args):
        reason = calculation_readiness(self.runs.currentData(), self.calculation.currentData(), self.reference_values())
        self.calculate_button.setEnabled(not self.tasks.busy and not reason)
        self.status.setText(reason or "Inputs ready. Calculate when ready.")

    def _busy(self, busy):
        self.calculate_button.setEnabled(not busy and not calculation_readiness(
            self.runs.currentData(), self.calculation.currentData(), self.reference_values()))
        self.refresh_button.setEnabled(not busy)

    def _submit(self, work, success):
        if self.tasks.busy:
            self.status.setText("A calculation or refresh is still running.")
            return
        generation = self._generation
        self._busy(True)

        def finished(value):
            self._busy(False)
            if generation == self._generation:
                success(value)
            else:
                self.refresh()

        def failed(exc):
            self._busy(False)
            if generation == self._generation:
                self.status.setText(str(exc))
            else:
                self.refresh()

        self.tasks.submit(work, finished, failed)

    def refresh(self):
        project = self.project
        selected = self.runs.currentData()

        def loaded(entries):
            self.runs.blockSignals(True)
            self.runs.clear()
            for entry in entries:
                self.runs.addItem(f"{entry['at']} · {entry['name']} · {entry['state']}", entry["directory"])
            index = self.runs.findData(selected)
            if index >= 0:
                self.runs.setCurrentIndex(index)
            self.runs.blockSignals(False)
            self._busy(False)
            self.status.setText("Choose a calculation and a run." if entries else "No finished protocol recordings in this project.")
            self.load_history()

        self._submit(lambda: recorded_runs(project), loaded)

    def load_history(self, *_args):
        directory = self.runs.currentData()
        selected = self.calculation.currentData()
        self.calculation.blockSignals(True)
        self.calculation.clear()
        for key in available_calculations(directory) if directory else []:
            self.calculation.addItem(CALCULATIONS[key]["label"], key)
        index = self.calculation.findData(selected)
        self.calculation.setCurrentIndex(max(index, 0))
        self.calculation.blockSignals(False)
        self.history.blockSignals(True)
        self.history.clear()
        self.output.clear()
        for payload in saved_results(directory):
            key = payload.get("calculation_id", "unknown")
            label = CALCULATIONS.get(key, {}).get("label", key)
            self.history.addItem(f"{payload.get('created_at', '')} · {label}" +
                                 (" · outdated" if payload["outdated"] else ""), payload)
        self.history.blockSignals(False)
        self.calculate_button.setEnabled(bool(directory) and not self.tasks.busy)
        self.show_result()
        self.describe()

    def show_result(self, *_args):
        payload = self.history.currentData()
        if payload:
            self.output.display(result_view(payload))
        else:
            self.output.show_message("No calculation saved for this run yet.")

    def calculate(self):
        directory = self.runs.currentData()
        key = self.calculation.currentData()
        if not directory:
            return
        self.status.setText("Calculating from saved recording…")

        def calculated(payload):
            if directory != self.runs.currentData():
                self.status.setText("Result saved for the previously selected run.")
                return
            self.load_history()
            self.output.display(result_view(payload))
            self.status.setText("Result saved.")

        references = self.reference_values()
        self._submit(lambda: calculate_run(directory, key, references=references), calculated)
