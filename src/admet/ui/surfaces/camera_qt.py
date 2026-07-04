from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from admet.ui.presenter import SurfaceVM
from admet.ui.surfaces.control_widgets_qt import PreviewDisplay


class CameraSurface(QWidget):
    frame_ready = Signal(object)

    def __init__(self, runtime: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self._unsubscribe = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.preview = PreviewDisplay()
        layout.addWidget(self.preview)
        self.frame_ready.connect(self.preview.set_frame)
        self.preview.frame_painted.connect(self._acknowledge_frame)
        self.destroyed.connect(lambda _obj=None: self._disconnect())
        self._connect()

    def _connect(self) -> None:
        engine = getattr(getattr(self.runtime, "api", None), "engine", None)
        if engine is None:
            return
        latest = getattr(engine, "latest_camera_frame", None)
        if callable(latest):
            self.preview.set_frame(latest())
        subscribe = getattr(engine, "subscribe_camera_frames", None)
        if callable(subscribe):
            self._unsubscribe = subscribe(lambda frame: self.frame_ready.emit(frame))

    def _disconnect(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _acknowledge_frame(self) -> None:
        engine = getattr(getattr(self.runtime, "api", None), "engine", None)
        acknowledge = getattr(engine, "acknowledge_camera_frame", None)
        if callable(acknowledge):
            acknowledge()


def render_camera_qt(layout: QVBoxLayout, _surface: SurfaceVM, runtime: Any) -> None:
    layout.addWidget(CameraSurface(runtime))
