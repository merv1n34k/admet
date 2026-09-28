import threading

from PySide6.QtCore import QObject, Signal


class Tasks(QObject):
    finished = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.busy = False
        self.finished.connect(self._deliver)

    def submit(self, work, success, failure):
        if self.busy:
            failure(RuntimeError("Another command is in progress"))
            return False
        self.busy = True

        def run():
            try:
                result = (success, work())
            except Exception as exc:
                result = (failure, exc)
            self.finished.emit(result)

        threading.Thread(target=run, daemon=True, name="admet-qt-command").start()
        return True

    def _deliver(self, result):
        self.busy = False
        callback, value = result
        callback(value)
