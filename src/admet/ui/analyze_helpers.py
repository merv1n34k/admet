from __future__ import annotations

import json
import time
from pathlib import Path

from admet.core.discovery import ProjectRef
from admet.ui.render import MatrixRow


FIELD_SETTING_KEYS = {
    "microns_per_pixel": "opencv_microns_per_pixel",
    "fps": "opencv_fps",
    "config_path": "cellpose_config_path",
    "px_to_um": "cellpose_px_to_um",
    "frame_limit": "cellpose_frame_limit",
    "use_cache": "cellpose_use_cache",
    "detect_inclusions": "cellpose_detect_inclusions",
}


def project_ref_label(ref: ProjectRef) -> str:
    return (
        f"{ref.project_id} · {(ref.updated or 'unknown')[:10]} · "
        f"{ref.recording_count} recordings / {ref.run_count} runs"
    )


def video_metadata(path: Path) -> dict[str, int]:
    if not path.exists():
        return {"frames": 500, "width": 1280, "height": 720}
    try:
        import cv2
    except Exception:
        return {"frames": 500, "width": 1280, "height": 720}
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return {"frames": 500, "width": 1280, "height": 720}
        frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 500)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)
        return {"frames": max(frames, 1), "width": max(width, 1), "height": max(height, 1)}
    finally:
        capture.release()


def video_placeholder(target: MatrixRow) -> str:
    label = json.dumps(
        {
            "sample": target.sample_id,
            "source": target.source_path,
        },
        sort_keys=True,
    )
    return f"""
    <div class="admet-video-placeholder">
      <div class="admet-video-bubbles"></div>
      <div class="admet-video-caption">{label}</div>
    </div>
    """


def uid() -> str:
    return f"target-{time.time_ns()}"
