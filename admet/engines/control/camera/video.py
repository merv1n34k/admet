from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread

import numpy as np

from .constants import QUEUE_GET_TIMEOUT, WRITER_QUEUE_SIZE, WRITER_THREAD_TIMEOUT

log = logging.getLogger(__name__)

Encoder = Callable[[Path, Path, int, int, float], str]


class VideoWorker:
    def __init__(
        self,
        output_dir: str | Path,
        prefix: str,
        width: int,
        height: int,
        fps: float,
        *,
        encoder: Encoder | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.prefix = prefix
        self.width = width
        self.height = height
        self.fps = fps
        self.encoder = encoder

        self.frames_dir: Path | None = None
        self.queue = Queue(maxsize=WRITER_QUEUE_SIZE)
        self.thread: Thread | None = None
        self._stop_event = Event()
        self.frame_count = 0

    def start(self) -> bool:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.frames_dir = Path(tempfile.mkdtemp(prefix="admet_", dir=self.output_dir))
            self._stop_event.clear()
            self.frame_count = 0
            self.thread = Thread(target=self._writer_thread, daemon=True)
            self.thread.start()
            return True
        except Exception as exc:
            log.error("Failed to start video writer: %s", exc)
            return False

    def write(self, frame: np.ndarray) -> bool:
        if self._stop_event.is_set():
            return False
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except Empty:
                return False
        try:
            self.queue.put_nowait((frame, self.frame_count))
            self.frame_count += 1
            return True
        except Full:
            return False

    def stop(self) -> str:
        self._stop_event.set()
        if self.thread:
            self.thread.join(timeout=WRITER_THREAD_TIMEOUT)
            if self.thread.is_alive():
                log.warning("Video writer thread did not finish in time")
        return self._make_video()

    def _writer_thread(self) -> None:
        while not self._stop_event.is_set() or not self.queue.empty():
            try:
                frame, index = self.queue.get(timeout=QUEUE_GET_TIMEOUT)
                if frame.dtype == np.uint16:
                    frame = (frame >> 8).astype(np.uint8)
                assert self.frames_dir is not None
                path = self.frames_dir / f"{index:08d}.raw"
                with path.open("wb") as handle:
                    handle.write(frame.tobytes())
            except Empty:
                continue
            except Exception as exc:
                log.debug("Video frame write error: %s", exc)

    def _make_video(self) -> str:
        if self.frames_dir is None:
            return ""
        frames = sorted(self.frames_dir.glob("*.raw"))
        if not frames:
            shutil.rmtree(self.frames_dir, ignore_errors=True)
            return ""

        video_path = self.output_dir / f"{self.prefix}.avi"
        if self.encoder is not None:
            return self.encoder(self.frames_dir, video_path, self.width, self.height, self.fps)
        return self._start_ffmpeg_encode(video_path)

    def _start_ffmpeg_encode(self, video_path: Path) -> str:
        assert self.frames_dir is not None
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "image2",
            "-framerate",
            str(self.fps),
            "-pixel_format",
            "gray",
            "-video_size",
            f"{self.width}x{self.height}",
            "-i",
            str(self.frames_dir / "%08d.raw"),
            "-c:v",
            "rawvideo",
            "-pix_fmt",
            "gray",
            str(video_path),
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            log.error("Failed to start ffmpeg: %s", exc)
            return ""

        cleanup = Thread(
            target=self._cleanup_after_encode,
            args=(proc, self.frames_dir, video_path),
            daemon=True,
        )
        cleanup.start()
        return str(video_path)

    @staticmethod
    def _cleanup_after_encode(proc, frames_dir: Path, video_path: Path) -> None:
        try:
            return_code = proc.wait()
            if return_code == 0:
                shutil.rmtree(frames_dir, ignore_errors=True)
                return
            if video_path.exists():
                video_path.unlink()
            raw_dir = video_path.parent / f"raw_{video_path.stem}"
            frames_dir.rename(raw_dir)
            log.error("FFmpeg failed with exit code %s; raw frames kept at %s", return_code, raw_dir)
        except Exception as exc:
            log.error("Video cleanup error: %s", exc)
