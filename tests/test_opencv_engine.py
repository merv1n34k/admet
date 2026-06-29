import tempfile
import unittest
from pathlib import Path

try:
    import cv2
    import numpy as np
except ModuleNotFoundError:
    cv2 = None
    np = None

def write_synthetic_video(path: Path, frames=30):
    writer = cv2.VideoWriter(
        str(path),
        cv2.VideoWriter_fourcc(*"MJPG"),
        30,
        (120, 80),
        isColor=False,
    )
    for idx in range(frames):
        frame = np.full((80, 120), 180, dtype=np.uint8)
        cv2.circle(frame, (15 + idx * 3, 40), 9, 80, -1)
        writer.write(frame)
    writer.release()


@unittest.skipIf(cv2 is None, "OpenCV is not installed")
class OpenCVEngineTests(unittest.TestCase):
    def test_engine_returns_shared_result_set(self):
        from admet.engines.analyze.opencv import create_engine

        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "droplets.avi"
            write_synthetic_video(video_path)

            engine = create_engine()
            result = engine.run_action(
                "analyze",
                {
                    "video_path": str(video_path),
                    "output_dir": str(Path(tmpdir) / "output"),
                    "microns_per_pixel": 1.0,
                    "fps": 30.0,
                    "max_frames": 30,
                },
            )

            stats = {stat.name: stat.value for stat in result.result_set.stats}
            self.assertGreater(stats["total_detections"], 0)
            self.assertGreaterEqual(stats["frames_processed"], 1)
            self.assertEqual(result.result_set.metadata["sample_id"], "droplets")
            self.assertTrue(result.artifacts["cache_dir"])


if __name__ == "__main__":
    unittest.main()
