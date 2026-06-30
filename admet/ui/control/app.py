from __future__ import annotations

import signal
import sys

from admet.core.api import AdmetAPI


def run_control_app(api: AdmetAPI, argv: list[str] | None = None) -> int:
    from PySide6.QtCore import QTimer
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
        interrupted = False
        previous_sigint = signal.getsignal(signal.SIGINT)

        def handle_sigint(_signum, _frame) -> None:
            nonlocal interrupted
            interrupted = True
            QTimer.singleShot(0, window.close)
            QTimer.singleShot(0, app.quit)

        signal_timer = QTimer()
        signal_timer.timeout.connect(lambda: None)
        signal_timer.start(100)
        signal.signal(signal.SIGINT, handle_sigint)
        try:
            return_code = app.exec()
        except KeyboardInterrupt:
            interrupted = True
            window.close()
            return_code = 0
        finally:
            signal_timer.stop()
            signal.signal(signal.SIGINT, previous_sigint)
        return 0 if interrupted else return_code
    return 0
