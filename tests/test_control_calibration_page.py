"""The Calibration page's tables, driven offscreen; the Fluigent controller is simulated."""

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton, QTableWidget  # noqa: E402

from admet.ui import theme as ui  # noqa: E402
from admet.ui.backend import DesktopBackend  # noqa: E402
from admet.ui.control import ControlWindow  # noqa: E402


class CalibrationPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(ui.stylesheet())

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.api = DesktopBackend(simulated=True)
        self.addCleanup(self.api.shutdown)
        self.api.create_project(Path(tmp.name) / "p.admetp")
        self.api.add_profile(name="M1 water", liquid="water", unit="M", calibration="H2O", scale=0.975)
        self.window = ControlWindow(self.api)
        self.addCleanup(self.window.close)
        self.window.show()
        self.window._select_stage(next(i for i, s in enumerate(self.window.workflow.stages) if s.id == "corrections"))
        self.pump()

    def pump(self, seconds=0.3):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def profiles(self):
        return next(t for t in self.window.findChildren(QTableWidget) if t.isVisible() and t.columnCount() == 8)

    def test_clicking_a_cell_keeps_the_focus_there_and_selects_its_row(self):
        table = self.profiles()
        project_row = table.rowCount() - 1
        scale = table.cellWidget(project_row, 4)
        QTest.mouseClick(scale, Qt.MouseButton.LeftButton)
        self.pump()

        self.assertIs(self.app.focusWidget(), scale)
        self.assertEqual([index.row() for index in table.selectionModel().selectedRows()], [project_row])
        scale.selectAll()
        QTest.keyClicks(self.app.focusWidget(), "0.98")
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Return)
        self.pump()
        self.assertEqual(self.api.profiles[0]["scale"], 0.98)

    def test_remove_acts_on_the_selected_project_row_only(self):
        table = self.profiles()
        remove = next(b for b in self.window.findChildren(QPushButton) if b.text() == "Remove" and b.isVisible())
        self.assertFalse(remove.isEnabled())
        QTest.mouseClick(table.cellWidget(0, 4), Qt.MouseButton.LeftButton)    # a default
        self.pump()
        self.assertFalse(remove.isEnabled())
        QTest.mouseClick(table.cellWidget(table.rowCount() - 1, 4), Qt.MouseButton.LeftButton)
        self.pump()
        self.assertTrue(remove.isEnabled())
        remove.click()
        self.pump()
        self.assertEqual(self.api.profiles, [])


if __name__ == "__main__":
    unittest.main()
