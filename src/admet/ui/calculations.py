"""Saved-run calculations, with no instrument actions."""

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout, QWidget

from admet.ui import theme as ui
from admet.ui.tasks import Tasks
from admet.workflows.calculations import (
    CALCULATIONS, calculate_run, recorded_runs, result_text, saved_results,
    available_calculations, calculation_readiness,
)


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
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(260)
        layout.addWidget(self.output)
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
        self.output.setPlainText(result_text(payload) if payload else "No calculation saved for this run yet.")

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
            self.output.setPlainText(result_text(payload))
            self.status.setText("Result saved; the recording was not changed.")

        references = self.reference_values()
        self._submit(lambda: calculate_run(directory, key, references=references), calculated)
