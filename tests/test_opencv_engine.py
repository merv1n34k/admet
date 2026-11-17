import json
import tempfile
import unittest
from pathlib import Path

from admet.core.run import JsonlRunSink
from admet.core.run import RunJob
from admet.engines.opencv.settings import OPENCV_SETTINGS

try:
    import cv2
    import numpy as np
except ModuleNotFoundError:
    cv2 = None
    np = None


def write_synthetic_video(path: Path, frames=30):
    """One dark droplet crossing a bright channel."""
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30, (120, 80), isColor=False)
    for idx in range(frames):
        frame = np.full((80, 120), 180, dtype=np.uint8)
        cv2.circle(frame, (15 + idx * 3, 40), 9, 80, -1)
        writer.write(frame)
    writer.release()


def write_train_video(path: Path, *, frames=120, speed=90, spacing=114, radius=20, width=640, height=96,
                      satellite=True, dimming=0.0):
    """A monodisperse train: dark droplets moving right at speed px/frame, one every spacing px.

    With dimming, the lighting fades by that fraction over the video, as a lamp does.
    """
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30, (width, height), isColor=False)
    rng = np.random.default_rng(3)
    for idx in range(frames):
        level = 200 * (1 - dimming * idx / max(frames - 1, 1))
        frame = np.full((height, width), level, dtype=np.float32)
        frame[:12, :] = level * 0.7                                   # channel walls
        frame[-12:, :] = level * 0.7
        offset = (idx * speed) % spacing
        for x in range(offset - spacing, width + spacing, spacing):
            cv2.circle(frame, (x, height // 2), radius, level * 0.8, -1)          # the droplet, darker
            cv2.circle(frame, (x, height // 2), radius, level * 0.6, 3)           # its rim, darker still
            if satellite:
                cv2.circle(frame, (x - radius - 8, height // 2), 3, level * 0.75, -1)
        frame += rng.normal(0, 2.0, frame.shape)
        writer.write(np.clip(frame, 0, 255).astype(np.uint8))
    writer.release()


@unittest.skipIf(cv2 is None, "OpenCV is not installed")
class OpenCVEngineTests(unittest.TestCase):
    def test_engine_rejects_missing_video_input(self):
        from admet.engines.opencv import create_engine

        engine = create_engine(OPENCV_SETTINGS)

        with self.assertRaisesRegex(ValueError, "requires input 'video'"):
            engine.run(RunJob(id="job-opencv", engine="opencv", action="run_analysis",
                              inputs={"mask": Path("wrong.tif")}, settings={"microns_per_pixel": 1.0, "fps": 30.0}))

    def test_config_includes_frame_range_roi_and_detection(self):
        from admet.engines.opencv import create_engine

        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "droplets.avi"
            write_synthetic_video(video_path)
            config = create_engine(OPENCV_SETTINGS)._config_from_settings(
                {"video_path": str(video_path), "microns_per_pixel": 1.5, "fps": 100.0, "start_frame": 10,
                 "end_frame": 90, "roi_x": 12, "roi_y": 18, "roi_width": 320, "roi_height": 0,
                 "k_sigma": 5.0, "satellite_px": 9, "min_area": 80, "skip_left_pct": 20, "skip_right_pct": 5,
                 "gate_pct": 60},
                cache_dir=Path("/tmp/cache"),
            )

        self.assertEqual(config["video"]["start_frame"], 10)
        self.assertEqual(config["video"]["end_frame"], 90)
        self.assertEqual(config["video"]["roi"], (12, 18, 320, 80))
        self.assertEqual(config["analysis"]["fps"], 100.0)
        self.assertEqual(config["detection"], {"k_sigma": 5.0, "satellite": 9, "min_area": 80.0, "skip_left_pct": 20.0,
                                               "skip_right_pct": 5.0, "gate_pct": 60.0})

    def test_engine_writes_raw_rows_to_job_sink(self):
        from admet.engines.opencv import create_engine

        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "droplets.avi"
            raw_path = Path(tmpdir) / "analysis" / "raw.jsonl"
            cache_dir = Path(tmpdir) / "cache" / "opencv"
            write_synthetic_video(video_path)

            engine = create_engine(OPENCV_SETTINGS)
            with JsonlRunSink(raw_path) as sink:
                result = engine.run(RunJob(
                    id="job-opencv", engine="opencv", action="run_analysis", inputs={"video": video_path},
                    cache_dir=cache_dir, sink=sink, metadata={"file_id": "video-1", "item_id": "sample-1"},
                    settings={"microns_per_pixel": 1.0, "fps": 30.0, "start_frame": 0, "end_frame": 30},
                ))
            rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines() if line.strip()]

            self.assertEqual(result.status, "complete")
            self.assertEqual(result.metadata["sample_id"], "droplets")
            self.assertEqual(result.metadata["row_count"], len(rows))
            self.assertEqual(rows[0]["job_id"], "job-opencv")
            self.assertEqual(rows[0]["file_id"], "video-1")
            self.assertEqual((rows[0]["engine"], rows[0]["kind"]), ("opencv", "frame"))
            self.assertTrue(result.metadata["cache_dir"])
            detections = [row["values"] for row in rows if row["kind"] == "detection"]
            self.assertTrue(detections)
            self.assertTrue(all(d["contour_path"] and d["droplet_id"] == 1 for d in detections))   # one droplet, one id
            self.assertAlmostEqual(detections[0]["equivalent_diameter"], 18, delta=2)
            context = next(row["values"] for row in rows if row["kind"] == "run_context")
            self.assertIn("generation", context)
            self.assertEqual(result.metadata["total_droplets"], 1)

    def test_a_fast_train_is_counted_once_per_droplet_despite_dimming(self):
        """90 px a frame with 114 px between droplets, satellites, and a lamp fading 10 %."""
        from admet.engines.opencv import create_engine

        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "train.avi"
            write_train_video(video_path, dimming=0.10)
            result = create_engine(OPENCV_SETTINGS).run(RunJob(
                id="job", engine="opencv", action="run_analysis", inputs={"video": video_path},
                settings={"microns_per_pixel": 2.0, "fps": 200.0, "skip_left_pct": 5, "skip_right_pct": 5,
                          "gate_pct": 50, "satellite_px": 11},
            ))

        generation = result.metadata["generation"]
        expected_period = 114 / 90
        # 120 frames at one droplet per 1.27 frames: a few lost at the ends of the gate's history
        self.assertAlmostEqual(generation["crossings"], 120 / expected_period, delta=4)
        self.assertAlmostEqual(generation["period_frames"], expected_period, delta=0.03)
        self.assertLess(generation["period_sd_frames"], 0.05)
        self.assertAlmostEqual(result.metadata["speed_px_frame"], 90, delta=2)
        self.assertAlmostEqual(result.metadata["frequency_hz"], 200 / expected_period, delta=5)
        # The rate is the train's speed over its spacing, which needs no droplet to be followed.
        self.assertEqual(generation["method"], "spacing")
        self.assertAlmostEqual(generation["spacing_px"], 114, delta=3)
        self.assertGreater(generation["spacing_samples"], 100)
        self.assertAlmostEqual(result.metadata["total_droplets"], generation["crossings"], delta=6)


@unittest.skipIf(cv2 is None, "OpenCV is not installed")
class DetectionTests(unittest.TestCase):
    def test_a_frame_darker_from_a_dimmed_lamp_is_matched_before_differencing(self):
        from admet.engines.opencv.detection import detect, match_brightness

        background = np.full((60, 100), 200, np.uint8)
        background[:8, :] = 140
        frame = (background * 0.92).astype(np.uint8)                 # the lamp dropped 8 %
        cv2.circle(frame, (50, 34), 10, 150, -1)

        residual = np.abs(match_brightness(frame, background).astype(int) - background)
        residual[20:48, 36:64] = 0                                    # the droplet itself is meant to differ
        self.assertLessEqual(residual.max(), 5)
        found = detect(frame, background, {"satellite": 5, "min_area": 50})
        self.assertEqual(len(found["properties"]), 1)
        self.assertAlmostEqual(found["properties"][0]["equivalent_diameter"], 20, delta=1.5)
        self.assertEqual(found["properties"][0]["solidity"] > 0.9, True)

    def test_the_smallest_droplet_kept_defaults_to_800_px2(self):
        from admet.engines.opencv.detection import DEFAULTS
        from admet.engines.opencv.engine import pipeline_config

        self.assertEqual(DEFAULTS["min_area"], 800.0)
        self.assertEqual(OPENCV_SETTINGS.defaults()["min_area"], 800.0)
        self.assertEqual(pipeline_config({})["detection"]["min_area"], 800.0)

    def test_a_rim_open_on_its_bright_side_is_completed_to_the_droplet(self):
        """Lit from one side, a droplet shows a dark rim on that side only; its hull is the droplet."""
        from admet.engines.opencv.detection import detect

        background = np.full((60, 320), 157, np.uint8)
        background[:6, :] = 100                                      # channel walls
        background[-6:, :] = 100
        frame = background.copy()
        cv2.circle(frame, (160, 30), 22, 90, 3)                      # the rim, 3 px
        cv2.ellipse(frame, (160, 30), (26, 26), 0, -50, 50, 157, 10)  # erased on the right: the bright side
        self.assertLess(frame[30, 180], 100)                          # a gap in the rim, not a closed ring
        self.assertEqual(int(frame[30, 185]), 157)

        found = detect(frame, background, {"satellite": 9, "min_area": 600})

        self.assertEqual(found["completed"], 1)
        self.assertEqual(len(found["properties"]), 1)
        droplet = found["properties"][0]
        self.assertAlmostEqual(droplet["equivalent_diameter"], 46, delta=3)
        self.assertGreater(droplet["solidity"], 0.95)
        self.assertAlmostEqual(droplet["centroid_x"], 160, delta=2)

    def test_faint_rims_over_a_quiet_background_are_found(self):
        """Four levels of contrast on half a level of noise: the floor of 6 levels used to hide it."""
        from admet.engines.opencv.detection import detect

        rng = np.random.default_rng(1)
        background = np.full((60, 320), 80, np.uint8)
        background[:6, :] = 50
        background[-6:, :] = 50
        frame = background.astype(np.float32)
        cv2.circle(frame, (160, 30), 22, 76, 3)                      # a full rim, 4 levels darker
        frame = np.clip(frame + rng.normal(0, 0.5, frame.shape), 0, 255).astype(np.uint8)

        found = detect(frame, background, {"satellite": 9, "min_area": 600})

        self.assertLess(found["threshold"], 4)
        self.assertEqual(len(found["properties"]), 1)
        self.assertAlmostEqual(found["properties"][0]["equivalent_diameter"], 46, delta=3)
        self.assertEqual(detect(frame, background, {"satellite": 9, "min_area": 600, "min_threshold": 6})
                         ["properties"], [])

    def test_satellites_are_set_aside_and_the_window_is_respected(self):
        from admet.engines.opencv.detection import detect

        background = np.full((60, 200), 200, np.uint8)
        frame = background.copy()
        cv2.circle(frame, (100, 30), 12, 150, -1)                     # a droplet
        cv2.circle(frame, (82, 30), 3, 150, -1)                      # its satellite, close behind
        cv2.circle(frame, (10, 30), 12, 150, -1)                      # one at the nozzle, outside the window
        found = detect(frame, background, {"satellite": 9, "min_area": 100, "skip_left_pct": 15})

        self.assertEqual([round(p["centroid_x"]) for p in found["properties"]], [100])
        self.assertEqual(len(found["satellites"]), 1)
        self.assertEqual([r["why"] for r in found["rejected"]], ["outside window"])
        self.assertEqual(found["window"], (30, 200))


@unittest.skipIf(cv2 is None, "OpenCV is not installed")
class TrackerTests(unittest.TestCase):
    def test_a_train_moving_most_of_its_spacing_is_followed_forward(self):
        from admet.engines.opencv.tracking import Tracker

        tracker = Tracker(gate_x=300)
        spacing, speed = 114, 90
        for frame in range(30):
            offset = (frame * speed) % spacing
            detections = [{"centroid_x": float(x), "centroid_y": 48.0}
                          for x in range(offset, 640, spacing) if 60 < x < 580]
            tracker.update(detections, frame)
        tracks = tracker.finish()

        self.assertAlmostEqual(tracker.speed(), speed, delta=0.5)
        self.assertTrue(all(all(np.diff(t["cx"]) > 0) for t in tracks if len(t["cx"]) > 1))   # never backwards
        gaps = np.diff(tracker.crossings)
        self.assertAlmostEqual(float(np.mean(gaps)), spacing / speed, delta=0.02)

    def test_droplets_leaving_a_junction_in_different_directions_keep_their_tracks(self):
        from admet.engines.opencv.tracking import Tracker

        tracker = Tracker()
        for frame in range(20):
            # two droplets from one junction: one goes up, one goes down, 3 px a frame
            tracker.update([{"centroid_x": 100.0 + frame, "centroid_y": 50.0 - 3 * frame},
                            {"centroid_x": 100.0 + frame, "centroid_y": 50.0 + 3 * frame}], frame)
        tracks = tracker.finish()

        self.assertEqual(len(tracks), 2)
        self.assertEqual({len(t["frames"]) for t in tracks}, {20})


@unittest.skipIf(cv2 is None, "OpenCV is not installed")
class DetectionStagesTests(unittest.TestCase):
    def test_each_stage_of_a_frame_with_the_row_settings(self):
        from admet.engines.opencv.debug import STAGES, detection_stages

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "train.avi"
            write_train_video(path, frames=40, speed=9, spacing=140, width=420, height=100)
            settings = {"microns_per_pixel": 1.0, "fps": 24.0, "start_frame": 0, "end_frame": None,
                        "roi_x": 0, "roi_y": 5, "roi_width": 420, "roi_height": 90, "skip_left_pct": 10,
                        "skip_right_pct": 10, "gate_pct": 50, "satellite_px": 11, "min_area": 150, "k_sigma": 4.0}

            stages = detection_stages(path, settings, 12)
            missing = detection_stages(Path(tmp) / "gone.avi", settings, 0)

        self.assertEqual(stages["error"], "")
        self.assertEqual(tuple(stages["images"]), STAGES)
        self.assertTrue(all(image.shape[:2] == (90, 420) for image in stages["images"].values()))   # the crop
        self.assertGreater(stages["threshold"], 0)
        self.assertGreaterEqual(stages["kept"], 2)
        self.assertGreaterEqual(stages["satellites"], 1)
        self.assertIn("not readable", missing["error"])


if __name__ == "__main__":
    unittest.main()
