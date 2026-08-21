import unittest

import numpy as np

from admet.engines.acquisition.camera.acquisition import CameraAcquisitionThread
from admet.engines.acquisition.recording import _frame_limit, _positive_or_none


class FakeCamera:
    def start_grabbing(self, **_kwargs) -> None:
        pass

    def stop_grabbing(self) -> None:
        pass

    def grab_frame(self):
        return None


class FakeWriter:
    def __init__(self) -> None:
        self.frame_count = 0

    def start(self) -> bool:
        return True

    def write(self, _frame) -> bool:
        self.frame_count += 1
        return True

    def stop(self) -> None:
        pass


def _thread() -> tuple[CameraAcquisitionThread, FakeWriter]:
    return CameraAcquisitionThread(FakeCamera()), FakeWriter()


FRAME = np.zeros((4, 4), dtype=np.uint8)


class RecordingLimitTests(unittest.TestCase):
    def test_max_frames_stops_the_recording(self):
        thread, writer = _thread()
        thread.start_recording(writer, max_frames=10)

        for _ in range(50):
            thread.process_frame(FRAME)

        self.assertEqual(writer.frame_count, 10)
        self.assertFalse(thread.recording)

    def test_zero_and_missing_limits_mean_unlimited(self):
        self.assertIsNone(_positive_or_none(0))
        self.assertIsNone(_positive_or_none(None))
        self.assertIsNone(_positive_or_none("nonsense"))
        self.assertEqual(_positive_or_none(2.5), 2.5)
        self.assertEqual(_frame_limit(100_000), 100_000)
        self.assertIsInstance(_frame_limit(100_000), int)


class RecordingPauseTests(unittest.TestCase):
    def test_pause_stops_writing_and_resume_continues_same_recording(self):
        thread, writer = _thread()
        thread.start_recording(writer)
        for _ in range(5):
            thread.process_frame(FRAME)

        thread.pause_recording()
        for _ in range(1000):
            thread.process_frame(FRAME)
        paused_total = writer.frame_count

        thread.resume_recording()
        thread.process_frame(FRAME)

        self.assertEqual(paused_total, 5)
        self.assertEqual(writer.frame_count, 6)
        self.assertTrue(thread.recording)
        self.assertIs(thread.writer, writer)

    def test_new_recording_does_not_inherit_a_pause(self):
        thread, writer = _thread()
        thread.start_recording(writer)
        thread.pause_recording()
        thread.stop_recording()

        second = FakeWriter()
        thread.start_recording(second)
        thread.process_frame(FRAME)

        self.assertFalse(thread.recording_paused)
        self.assertEqual(second.frame_count, 1)


if __name__ == "__main__":
    unittest.main()
