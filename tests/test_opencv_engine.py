import json
import tempfile
import unittest
from pathlib import Path

from admet.core.engine import RunJob
from admet.core.run import JsonlRunSink

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
    def test_engine_writes_raw_rows_to_job_sink(self):
        from admet.engines.analyze.opencv import create_engine

        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "droplets.avi"
            raw_path = Path(tmpdir) / "analysis" / "raw.jsonl"
            cache_dir = Path(tmpdir) / "cache" / "opencv"
            write_synthetic_video(video_path)

            engine = create_engine()
            with JsonlRunSink(raw_path) as sink:
                result = engine.run(
                    RunJob(
                        id="job-opencv",
                        engine="opencv",
                        action="analyze",
                        inputs={"video": video_path},
                        cache_dir=cache_dir,
                        sink=sink,
                        metadata={"file_id": "video-1", "item_id": "sample-1"},
                        settings={
                            "microns_per_pixel": 1.0,
                            "fps": 30.0,
                            "max_frames": 30,
                        },
                    )
                )

            rows = [
                json.loads(line)
                for line in raw_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

            self.assertEqual(result.status, "complete")
            self.assertEqual(result.metadata["sample_id"], "droplets")
            self.assertGreater(result.metadata["row_count"], 0)
            self.assertEqual(result.metadata["row_count"], len(rows))
            self.assertEqual(rows[0]["job_id"], "job-opencv")
            self.assertEqual(rows[0]["file_id"], "video-1")
            self.assertEqual(rows[0]["engine"], "opencv")
            self.assertEqual(rows[0]["kind"], "frame")
            self.assertTrue(result.metadata["cache_dir"])

    def test_action_returns_shared_result_set(self):
        from admet.engines.analyze.opencv import create_engine

        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "droplets.avi"
            write_synthetic_video(video_path)

            result = create_engine().run_action(
                "analyze",
                {
                    "video_path": str(video_path),
                    "microns_per_pixel": 1.0,
                    "fps": 30.0,
                    "max_frames": 30,
                },
            )

            stats = {stat.name: stat.value for stat in result.result_set.stats}
            self.assertGreater(stats["total_detections"], 0)
            self.assertGreaterEqual(stats["frames_processed"], 1)
            self.assertEqual(result.result_set.metadata["sample_id"], "droplets")
            self.assertIsNone(result.artifacts["cache_dir"])


if __name__ == "__main__":
    unittest.main()
