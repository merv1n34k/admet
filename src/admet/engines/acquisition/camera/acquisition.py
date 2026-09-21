from __future__ import annotations

import logging
import time
from collections.abc import Callable
from threading import Event, Thread
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


class CameraSource(Protocol):
    def start_grabbing(self, *, latest_only: bool = True) -> None:
        ...

    def stop_grabbing(self) -> None:
        ...

    def grab_frame(self, timeout_ms: int = 5) -> np.ndarray | None:
        ...


class FrameWriter(Protocol):
    frame_count: int

    def start(self) -> bool:
        ...

    def write(self, frame: np.ndarray) -> bool:
        ...

    def stop(self) -> str:
        ...


FrameCallback = Callable[[np.ndarray], None]
StatsCallback = Callable[[dict], None]
RecordingCompleteCallback = Callable[[], None]


class CameraAcquisitionThread(Thread):
    def __init__(
        self,
        camera: CameraSource,
        *,
        preview_callback: FrameCallback | None = None,
        stats_callback: StatsCallback | None = None,
        recording_complete_callback: RecordingCompleteCallback | None = None,
        sleep_s: float = 0.001,
        stats_interval_s: float = 0.2,
    ):
        super().__init__(daemon=True, name="CameraAcquisitionThread")
        self.camera = camera
        self.preview_callback = preview_callback
        self.stats_callback = stats_callback
        self.recording_complete_callback = recording_complete_callback
        self.sleep_s = sleep_s
        self.stats_interval_s = stats_interval_s

        self.writer: FrameWriter | None = None
        self.last_recording_frames: int | None = None
        self.last_writer_frame_count: int | None = None
        self.frame_count = 0
        self.total_frames = 0
        self.live_started_monotonic: float | None = None
        self.latest_frame_monotonic: float | None = None
        self.start_time = 0.0
        self.last_stats_time = 0.0
        self.max_frames: int | None = None
        self.max_time: float | None = None
        self.preview_enabled = True

        self._stop_event = Event()
        self._recording_event = Event()
        self._recording_paused = Event()
        self._frame_pending = Event()

    @property
    def recording(self) -> bool:
        return self._recording_event.is_set()

    @property
    def recording_paused(self) -> bool:
        return self._recording_paused.is_set()

    def pause_recording(self) -> None:
        """Stop writing frames while keeping the recording open.

        The writer, frame count and limits are preserved, so resuming continues the
        same recording instead of starting a new one.
        """
        self._recording_paused.set()

    def resume_recording(self) -> None:
        self._recording_paused.clear()

    def run(self) -> None:
        self._stop_event.clear()
        self.live_started_monotonic = time.monotonic()
        self.last_stats_time = time.time()
        self.camera.start_grabbing(latest_only=True)
        while not self._stop_event.is_set():
            frame = self.camera.grab_frame()
            if frame is None:
                time.sleep(self.sleep_s)
                continue
            self.process_frame(frame)
        self.camera.stop_grabbing()

    def process_frame(self, frame: np.ndarray) -> None:
        self.total_frames += 1
        self.latest_frame_monotonic = time.monotonic()
        if self._recording_event.is_set() and not self._recording_paused.is_set() and self.writer:
            if self.writer.write(frame):
                self.frame_count += 1
                if self._check_limits():
                    self.stop_recording(notify_complete=True)

        if self.preview_enabled and not self._frame_pending.is_set() and self.preview_callback:
            self._frame_pending.set()
            self.preview_callback(frame)

        current_time = time.time()
        if self.stats_callback and current_time - self.last_stats_time >= self.stats_interval_s:
            self.last_stats_time = current_time
            recording = self._recording_event.is_set()
            self.stats_callback(
                {
                    "recording": recording,
                    "frames": self.frame_count if recording else 0,
                    "elapsed": current_time - self.start_time if recording else 0,
                    "total_frames": self.total_frames,
                    "latest_frame_monotonic": self.latest_frame_monotonic,
                    "fps": (
                        self.total_frames
                        / max(time.monotonic() - self.live_started_monotonic, 1e-9)
                        if self.live_started_monotonic is not None
                        else None
                    ),
                }
            )

    def start_recording(
        self,
        writer: FrameWriter,
        *,
        max_frames: int | None = None,
        max_time: float | None = None,
    ) -> bool:
        self.writer = writer
        self.max_frames = max_frames
        self.max_time = max_time
        self.frame_count = 0
        self.start_time = time.time()
        self._recording_paused.clear()
        if not self.writer.start():
            self.writer = None
            return False
        self.camera.stop_grabbing()
        self.camera.start_grabbing(latest_only=False)
        self._recording_event.set()
        return True

    def stop_recording(self, *, notify_complete: bool = False) -> int:
        frames = self.frame_count
        self._recording_event.clear()
        self._recording_paused.clear()
        if self.writer:
            self.last_writer_frame_count = getattr(self.writer, "frame_count", None)
            self.writer.stop()
            self.writer = None
        self.last_recording_frames = frames
        if not self._stop_event.is_set():
            self.camera.stop_grabbing()
            self.camera.start_grabbing(latest_only=True)
        if notify_complete and self.recording_complete_callback:
            try:
                self.recording_complete_callback()
            except Exception:
                log.exception("recording completion callback failed")
        return frames

    def stop(self) -> None:
        self._stop_event.set()
        if self._recording_event.is_set():
            self.stop_recording()
        if self.is_alive():
            self.join(timeout=2.0)

    def set_preview_enabled(self, enabled: bool) -> None:
        self.preview_enabled = enabled

    def set_recording_complete_callback(self, callback: RecordingCompleteCallback | None) -> None:
        self.recording_complete_callback = callback

    def frame_processed(self) -> None:
        self._frame_pending.clear()

    def _check_limits(self) -> bool:
        if self.max_frames and self.frame_count >= self.max_frames:
            return True
        if self.max_time and time.time() - self.start_time >= self.max_time:
            return True
        return False
