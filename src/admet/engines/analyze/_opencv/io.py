"""
Video I/O Module - Simplified
Handles video reading and frame preprocessing
"""

import cv2
import numpy as np
import random
from pathlib import Path
from typing import Optional, Tuple, List, Generator
from .logger import get_logger

logger = get_logger("VideoIO")


class VideoReader:
    """Simple video file reader"""

    def __init__(
        self,
        source: str = None,
        scale_factor: float = 1.0,
        roi: Optional[Tuple[int, int, int, int]] = None,
    ):
        """
        Initialize video reader

        Args:
            source: Path to video file
            scale_factor: Resize factor (1.0 = no resize)
            roi: Region of interest (x, y, width, height)
        """
        self.source = source
        self.scale_factor = scale_factor
        self.roi = roi
        self.cap = None
        self.total_frames = 0
        self.fps = 0
        self.width = 0
        self.height = 0
        self.current_frame = 0

        logger.debug(f"VideoReader initialized: {source}")

    def open(self) -> bool:
        """Open video file"""
        if not self.source:
            logger.error("No source specified")
            return False

        # Check file exists
        if not Path(self.source).exists():
            logger.error(f"File not found: {self.source}")
            return False

        self.cap = cv2.VideoCapture(self.source)

        if not self.cap.isOpened():
            logger.error(f"Failed to open: {self.source}")
            return False

        # Get properties
        self.fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))

        if self.fps <= 0:
            self.fps = 30

        logger.info(
            f"Opened: {self.width}x{self.height}, {self.total_frames} frames, {self.fps:.1f} fps"
        )
        return True

    def read_frame(self) -> Optional[np.ndarray]:
        """Read and preprocess single frame"""
        if not self.cap:
            return None

        ret, frame = self.cap.read()
        if not ret:
            return None

        self.current_frame += 1

        # Convert to grayscale
        if len(frame.shape) == 3:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Scale
        if self.scale_factor != 1.0:
            new_w = int(frame.shape[1] * self.scale_factor)
            new_h = int(frame.shape[0] * self.scale_factor)
            frame = cv2.resize(frame, (new_w, new_h))

        # Apply ROI
        if self.roi:
            x, y, w, h = self.roi
            frame = frame[y : y + h, x : x + w]

        return frame

    def read_frames(self, n: int) -> List[np.ndarray]:
        """Read n frames"""
        frames = []
        for _ in range(n):
            frame = self.read_frame()
            if frame is None:
                break
            frames.append(frame)
        logger.debug(f"Read {len(frames)} frames")
        return frames

    def read_sampled_frames(
        self, n: int, seed: int = 0, start_frame: int = 0, end_frame: int = None
    ) -> List[np.ndarray]:
        """Read up to n frames sampled across the video."""
        if not self.cap or n <= 0:
            return []

        total = self.total_frames or int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0:
            return self.read_frames(n)

        start_frame = max(int(start_frame or 0), 0)
        end_frame = total if end_frame is None else int(end_frame)
        end_frame = min(max(end_frame, start_frame), total)
        available = max(end_frame - start_frame, 0)
        if available <= 0:
            return []

        if n >= available:
            indices = list(range(start_frame, end_frame))
        else:
            rng = random.Random(seed)
            indices = sorted(rng.sample(range(start_frame, end_frame), n))

        frames = []
        for idx in indices:
            if self.seek_frame(idx):
                frame = self.read_frame()
                if frame is not None:
                    frames.append(frame)

        logger.debug(f"Read {len(frames)} sampled frames")
        return frames

    def get_frame_generator(self) -> Generator[Tuple[int, np.ndarray], None, None]:
        """Yield frames with index"""
        frame_idx = 0
        while True:
            frame = self.read_frame()
            if frame is None:
                break
            yield frame_idx, frame
            frame_idx += 1

    def seek_frame(self, frame_number: int) -> bool:
        """Seek to frame number"""
        if not self.cap:
            return False
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
        self.current_frame = frame_number
        return True

    def get_properties(self) -> dict:
        """Get video properties"""
        return {
            "source": self.source,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "total_frames": self.total_frames,
        }

    def release(self):
        """Release video"""
        if self.cap:
            self.cap.release()
            self.cap = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.release()
