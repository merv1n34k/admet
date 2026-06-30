import json
import tempfile
import unittest
from pathlib import Path

from admet.core.engine import EngineResult
from admet.core.schema import ResultSet
from admet.engines.control import RecordingSession


class FakeWriter:
    def __init__(self, video_dir, prefix, width, height, fps):
        self.video_dir = Path(video_dir)
        self.prefix = prefix
        self.width = width
        self.height = height
        self.fps = fps
        self.frame_count = 7
        self.path = self.video_dir / f"{prefix}_fake.avi"

    def start(self):
        self.video_dir.mkdir(parents=True, exist_ok=True)
        return True

    def write(self, frame):
        return True

    def stop(self):
        self.path.write_bytes(b"AVI")
        return str(self.path)


class FakeCameraRecorder:
    def __init__(self):
        self.recording = False
        self.writer = None
        self.start_args = None
        self.recording_complete_callback = None
        self.last_recording_frames = None
        self.last_writer_frame_count = None

    def start_recording(self, writer, *, max_frames=None, max_time=None):
        self.writer = writer
        self.start_args = (max_frames, max_time)
        self.recording = writer.start()
        return self.recording

    def stop_recording(self):
        self.last_recording_frames = 5
        self.last_writer_frame_count = self.writer.frame_count
        self.recording = False
        self.writer.stop()
        self.writer = None
        return 5

    def set_recording_complete_callback(self, callback):
        self.recording_complete_callback = callback

    def complete_from_camera_limit(self):
        self.last_recording_frames = 5
        self.last_writer_frame_count = self.writer.frame_count
        self.recording = False
        self.writer.stop()
        self.writer = None
        self.recording_complete_callback()


class FakeControlBackend:
    def __init__(self):
        self.actions = []

    def run_action(self, action, settings, context=None):
        self.actions.append((action, dict(settings)))
        if action == "start_recording":
            csv_path = str(Path(settings["log_dir"]) / "droplegen_fake.csv")
            return EngineResult(ResultSet(), artifacts={"csv_path": csv_path})
        return EngineResult(ResultSet())


class RecordingSessionTests(unittest.TestCase):
    def test_start_and_stop_recording_writes_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            camera = FakeCameraRecorder()
            control = FakeControlBackend()
            session = RecordingSession(
                tmpdir,
                camera,
                control,
                writer_factory=FakeWriter,
            )

            started = session.start_recording(
                "run1",
                width=640,
                height=240,
                fps=120.0,
                max_frames=10,
            )
            stopped = session.stop_recording()

            report_dir = session.report_dir
            summary = json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))

        self.assertEqual(started.video_prefix, "run1")
        self.assertEqual(camera.start_args, (10, None))
        self.assertEqual(control.actions[0][0], "start_recording")
        self.assertEqual(control.actions[1][0], "stop_recording")
        self.assertEqual(stopped.frames_recorded, 5)
        self.assertEqual(stopped.frames_written, 7)
        self.assertEqual(stopped.converted_fps, 120.0)
        self.assertGreater(stopped.acquisition_fps, 0.0)
        self.assertTrue(stopped.video_path.endswith("run1_fake.avi"))
        self.assertEqual(summary["recording_count"], 1)
        self.assertEqual(summary["recordings"][0]["video_prefix"], "run1")
        self.assertEqual(summary["recordings"][0]["converted_fps"], 120.0)
        self.assertGreater(summary["recordings"][0]["acquisition_fps"], 0.0)

    def test_camera_auto_stop_finalizes_full_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            camera = FakeCameraRecorder()
            control = FakeControlBackend()
            session = RecordingSession(
                tmpdir,
                camera,
                control,
                writer_factory=FakeWriter,
            )

            session.start_recording(
                "run1",
                width=640,
                height=240,
                fps=120.0,
                max_frames=10,
            )
            camera.complete_from_camera_limit()

            report_dir = session.report_dir
            summary = json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))

        self.assertIsNone(session.current)
        self.assertEqual(control.actions[0][0], "start_recording")
        self.assertEqual(control.actions[1][0], "stop_recording")
        self.assertEqual(summary["recording_count"], 1)
        self.assertEqual(summary["recordings"][0]["frames_recorded"], 5)
        self.assertEqual(summary["recordings"][0]["frames_written"], 7)


if __name__ == "__main__":
    unittest.main()
