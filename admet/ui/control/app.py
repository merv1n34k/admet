from __future__ import annotations

import sys

from admet.core.api import AdmetAPI


def run_control_app(api: AdmetAPI, argv: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication

    from admet.ui.control.theme import stylesheet
    from admet.ui.control.window import ControlWindow

    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(argv if argv is not None else sys.argv[:1])
    app.setStyleSheet(stylesheet())

    window = ControlWindow(api)
    window.show()
    if owns_app:
        return app.exec()
    return 0
