import importlib.util
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "Qt required")
class DropdownContrastTests(unittest.TestCase):
    def test_popup_selection_has_explicit_contrast_on_windows_style(self):
        from PySide6.QtCore import QItemSelectionModel
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QApplication, QComboBox, QStyleFactory, QTableWidget
        from admet.ui.theme import Theme, stylesheet

        app = QApplication.instance() or QApplication([])
        old_style, old_sheet = app.style().objectName(), app.styleSheet()
        self.addCleanup(lambda: app.setStyle(QStyleFactory.create(old_style)))
        self.addCleanup(lambda: app.setStyleSheet(old_sheet))
        app.setStyleSheet(stylesheet())
        for style in (name for name in QStyleFactory.keys() if name.lower() in {"windows", "fusion"}):
            with self.subTest(style=style):
                app.setStyle(QStyleFactory.create(style))
                table = QTableWidget(1, 1)
                combo = QComboBox()
                combo.addItems(["First option", "Selected option"])
                table.setCellWidget(0, 0, combo)
                table.show()
                combo.setCurrentIndex(1)
                combo.showPopup()
                app.processEvents()
                view = combo.view()
                index = combo.model().index(1, 0)
                view.selectionModel().select(index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
                app.processEvents()
                image = view.viewport().grab().toImage()
                ratio = image.devicePixelRatio()
                rect = view.visualRect(index).intersected(view.viewport().rect())
                colors = {image.pixelColor(int(x * ratio), int(y * ratio)).name()
                          for x in range(rect.left(), rect.right())
                          for y in range(rect.top(), rect.bottom())}
                self.assertTrue({QColor(Theme.ACCENT).name(), QColor(Theme.ACCENT_HOVER).name()} & colors)
                self.assertIn("#ffffff", colors)
                combo.hidePopup()
                table.close()
