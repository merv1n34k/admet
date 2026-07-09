from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from threading import Lock
from typing import Any

import numpy as np

from .acquisition import CameraAcquisitionThread
from .camera import Camera


log = logging.getLogger(__name__)


class CameraController:
    def __init__(self, camera: Any | None = None):
        self.camera = camera or Camera()
        self._acquisition: CameraAcquisitionThread | None = None
        self._preview_size = (0, 0)
        self._last_frame: np.ndarray | None = None
        self._stats: dict[str, Any] = {}
        self._preflight_cache: dict[str, Any] = {}
        self._frame_callbacks: list[Callable[[np.ndarray], None]] = []
        self._lock = Lock()

    @property
    def live(self) -> bool:
        return self._acquisition is not None and self._acquisition.is_alive()

    @property
    def acquisition(self) -> CameraAcquisitionThread | None:
        return self._acquisition

    def connect(self, settings: dict[str, Any]) -> dict[str, Any]:
        metadata = self.preflight()
        if not metadata["pypylon_available"]:
            metadata.update(
                {
                    "camera_connect_ok": False,
                    "camera_connect_message": metadata["camera_message"],
                }
            )
            return metadata
        if metadata["camera_count"] <= settings["camera_index"]:
            metadata.update(
                {
                    "camera_connect_ok": False,
                    "camera_connect_message": "Camera is not currently available; refresh after attaching it.",
                }
            )
            return metadata

        ok = self.camera.open(settings["camera_index"])
        metadata = self.preflight()
        metadata.update(
            {
                "camera_connect_ok": ok,
                "camera_connect_message": "Camera connected." if ok else "Camera open failed.",
                "camera_connected": self.camera.connected,
            }
        )
        if ok:
            try:
                self.apply_configuration(settings)
                metadata["camera_settings_ok"] = True
                metadata["camera_settings_message"] = "Camera defaults applied."
            except Exception as exc:
                metadata["camera_settings_ok"] = False
                metadata["camera_settings_message"] = str(exc)
            metadata.update(self.read_parameters())
        return metadata

    def disconnect(self) -> dict[str, Any]:
        self.stop_live()
        closer = getattr(self.camera, "close", None)
        if callable(closer):
            closer()
        return self.preflight()

    def apply_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        if not self.camera.connected:
            metadata = self.status_metadata()
            metadata["camera_settings_ok"] = False
            metadata["camera_settings_message"] = "Camera is not connected."
            return metadata

        was_live = self.live
        if was_live:
            self.stop_live()

        try:
            self.apply_configuration(settings)
            ok = True
            message = "Camera settings applied."
        except Exception as exc:
            ok = False
            message = str(exc)
        finally:
            if was_live:
                time.sleep(0.05)
                self.start_live()

        metadata = self.status_metadata()
        metadata.update(self.read_parameters())
        metadata["camera_settings_ok"] = ok
        metadata["camera_settings_message"] = message
        return metadata

    def start_live(self) -> None:
        if self.live:
            return
        if not self.camera.connected:
            raise RuntimeError("Camera is not connected")
        self._acquisition = CameraAcquisitionThread(
            self.camera,
            preview_callback=self._on_frame,
            stats_callback=self._on_stats,
        )
        self._acquisition.start()

    def stop_live(self) -> None:
        if self._acquisition:
            self._acquisition.stop()
        self._acquisition = None

    def apply_configuration(self, settings: dict[str, Any]) -> None:
        offset_x = _snap_offset(settings["camera_offset_x"])
        offset_y = _snap_offset(settings["camera_offset_y"])
        if offset_x != 0 or offset_y != 0:
            self.camera.set_parameter("OffsetX", 0)
            self.camera.set_parameter("OffsetY", 0)

        cam_settings: dict[str, Any] = {
            "Width": settings["camera_width"],
            "Height": settings["camera_height"],
            "OffsetX": offset_x,
            "OffsetY": offset_y,
            "BinningHorizontal": settings["camera_binning_h"],
            "BinningVertical": settings["camera_binning_v"],
            "ExposureTime": settings["camera_exposure_us"],
            "Gain": settings["camera_gain"],
            "PixelFormat": settings["camera_pixel_format"],
            "SensorReadoutMode": settings["camera_readout"],
        }
        if settings["camera_framerate_enabled"]:
            cam_settings["AcquisitionFrameRateEnable"] = True
            cam_settings["AcquisitionFrameRate"] = settings["camera_framerate_hz"]
        else:
            cam_settings["AcquisitionFrameRateEnable"] = False
        if settings["camera_throughput_enabled"]:
            cam_settings["DeviceLinkThroughputLimitMode"] = "On"
            cam_settings["DeviceLinkThroughputLimit"] = int(settings["camera_throughput_mbps"] * 1_000_000)
        else:
            cam_settings["DeviceLinkThroughputLimitMode"] = "Off"
        self.camera.apply_settings(cam_settings)

    def preflight(self) -> dict[str, Any]:
        camera_status = self.camera.preflight()
        camera_status_metadata = asdict(camera_status)
        metadata = {
            "camera": camera_status_metadata,
            "pypylon_available": camera_status.pypylon_available,
            "camera_refresh_ok": camera_status.refresh_ok,
            "camera_count": camera_status.camera_count,
            "cameras": list(camera_status.cameras),
            "camera_transport_layers": list(camera_status.transport_layers),
            "camera_connected": self.camera.connected,
            "pylon_module_loaded": camera_status.pylon_module_loaded,
            "camera_message": camera_status.message,
        }
        self._preflight_cache = metadata
        metadata.update(self.status_metadata())
        return metadata

    def status_metadata(self) -> dict[str, Any]:
        with self._lock:
            preview_size = self._preview_size
            stats = dict(self._stats)
            frame_shape = list(self._last_frame.shape) if self._last_frame is not None else []
        metadata = {
            **self._preflight_cache,
            "camera_connected": self.camera.connected,
            "camera_live": self.live,
            "camera_preview_width": preview_size[0],
            "camera_preview_height": preview_size[1],
            "camera_frame_shape": frame_shape,
            "camera_fps": self.camera.get_resulting_framerate() if self.camera.connected else 0.0,
            "camera_recording": bool(stats.get("recording", False)),
            "camera_recorded_frames": int(stats.get("frames", 0)),
            "camera_record_elapsed": float(stats.get("elapsed", 0.0)),
        }
        metadata.update(self.read_parameters())
        return metadata

    def read_parameters(self) -> dict[str, Any]:
        if not self.camera.connected:
            return {"camera_parameters": {}}
        names = [
            "Width",
            "Height",
            "OffsetX",
            "OffsetY",
            "ExposureTime",
            "Gain",
            "PixelFormat",
            "SensorReadoutMode",
            "BinningHorizontal",
            "BinningVertical",
            "AcquisitionFrameRate",
            "DeviceLinkThroughputLimit",
            "ResultingFrameRate",
        ]
        parameters = self.camera.get_settings(names)
        return {"camera_parameters": parameters}

    def recording_frame_size(self, settings: dict[str, Any]) -> tuple[int, int]:
        with self._lock:
            frame = self._last_frame
            if frame is not None:
                return int(frame.shape[1]), int(frame.shape[0])
            preview_size = self._preview_size
        if preview_size[0] and preview_size[1]:
            return preview_size
        return int(settings["camera_width"]), int(settings["camera_height"])

    def latest_frame(self) -> np.ndarray | None:
        with self._lock:
            return self._last_frame

    def subscribe_frames(
        self,
        callback: Callable[[np.ndarray], None],
    ) -> Callable[[], None]:
        with self._lock:
            self._frame_callbacks.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._frame_callbacks.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def acknowledge_frame(self) -> None:
        if self._acquisition:
            self._acquisition.frame_processed()

    def _on_frame(self, frame: np.ndarray) -> None:
        frame = frame.copy()
        with self._lock:
            self._last_frame = frame
            self._preview_size = (frame.shape[1], frame.shape[0])
            callbacks = tuple(self._frame_callbacks)
        if not callbacks:
            self.acknowledge_frame()
            return
        delivered = False
        for callback in callbacks:
            try:
                callback(frame)
                delivered = True
            except Exception:
                log.exception("camera frame callback failed")
        if not delivered:
            self.acknowledge_frame()

    def _on_stats(self, stats: dict[str, Any]) -> None:
        with self._lock:
            self._stats = dict(stats)


def _snap_offset(value: int) -> int:
    return round(int(value) / 16) * 16
