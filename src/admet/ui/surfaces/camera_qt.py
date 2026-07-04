from __future__ import annotations

from typing import Any

import numpy as np
from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QVBoxLayout, QWidget

from admet.ui.presenter import SurfaceVM


PREVIEW_MIN_HEIGHT = 280
PREVIEW_MAX_HEIGHT = 520


class PreviewDisplay(QWidget):
    frame_painted = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.current_frame: np.ndarray | None = None
        self.frame_rect = QRect()
        self.message = "No camera frame"
        self._aspect = 480 / 640

    def set_frame(self, frame: np.ndarray | None) -> None:
        if frame is None:
            return
        if frame.dtype == np.uint16:
            frame = (frame >> 8).astype(np.uint8)
        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)
        self.current_frame = frame
        height, width = frame.shape[:2]
        if width > 0 and height > 0:
            self._aspect = height / width
            self.updateGeometry()
        self.message = ""
        self.update()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return max(PREVIEW_MIN_HEIGHT, min(PREVIEW_MAX_HEIGHT, int(width * self._aspect)))

    def sizeHint(self) -> QSize:
        return QSize(760, self.heightForWidth(760))

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)

        if self.message:
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.message)
            return
        if self.current_frame is None:
            return

        height, width = self.current_frame.shape[:2]
        if self.current_frame.ndim == 2:
            image = QImage(
                self.current_frame.data,
                width,
                height,
                width,
                QImage.Format.Format_Grayscale8,
            )
        elif self.current_frame.ndim == 3 and self.current_frame.shape[2] >= 3:
            image = QImage(
                self.current_frame.data,
                width,
                height,
                width * 3,
                QImage.Format.Format_RGB888,
            )
        else:
            return

        widget_rect = self.rect()
        scale_x = widget_rect.width() / width if width > 0 else 1
        scale_y = widget_rect.height() / height if height > 0 else 1
        scale = min(scale_x, scale_y)
        final_width = int(width * scale)
        final_height = int(height * scale)
        x = (widget_rect.width() - final_width) // 2
        y = (widget_rect.height() - final_height) // 2
        self.frame_rect = QRect(x, y, final_width, final_height)

        scaled = image.scaled(
            self.frame_rect.width(),
            self.frame_rect.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        painter.drawImage(self.frame_rect, scaled)
        self.frame_painted.emit()


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
