from __future__ import annotations

import unittest
from types import SimpleNamespace

from admet.ui.analyze_matrix import cast_matrix_value, matrix_columns, matrix_params, schema_matrix_row
from admet.core.engine import ParamKind
from admet.ui.data import matrix_row, recording_video_key, prefer_video_row, video_metadata, video_row


class AnalyzeDataTests(unittest.TestCase):
    def test_matrix_row_shapes_batch_target(self) -> None:
        row = SimpleNamespace(
            uid="row-1",
            project_path="/tmp/project.admetp",
            source_path="/tmp/data/movie.avi",
            engine="opencv",
            sample_id="sample-a",
            active=True,
        )

        self.assertEqual(
            matrix_row(row),
            {
                "uid": "row-1",
                "project": "project.admetp",
                "project_path": "/tmp/project.admetp",
                "source": "movie.avi",
                "source_path": "/tmp/data/movie.avi",
                "engine": "opencv",
                "sample_id": "sample-a",
                "active": True,
            },
        )

    def test_analyze_matrix_helpers_shape_schema_rows(self) -> None:
        row = SimpleNamespace(
            uid="row-1",
            project_path="/tmp/project.admetp",
            source_path="/tmp/data/movie.avi",
            engine="opencv",
            sample_id="sample-a",
            active=True,
            settings={"start_frame": "10", "microns_per_pixel": "1.25"},
        )
        params = matrix_params("opencv")
        shaped = schema_matrix_row(row, params)

        self.assertEqual(matrix_columns()[0]["name"], "active")
        self.assertEqual(shaped["source"], "movie.avi")
        self.assertEqual(shaped["start_frame"], 10)
        self.assertEqual(shaped["microns_per_pixel"], 1.25)
        self.assertEqual(cast_matrix_value(ParamKind.INTEGER, "5"), 5)


class ControlDataTests(unittest.TestCase):
    def test_video_metadata_and_row_shape_recording(self) -> None:
        recording = {
            "video_path": "/tmp/control/camera/set01_rep02.avi",
            "width": 640,
            "height": 480,
            "acquisition_fps": 29.95,
            "converted_fps": 30.0,
            "frames_recorded": 120,
            "duration_s": 4.0,
        }

        metadata = video_metadata(recording)
        self.assertEqual(metadata["dimensions"], "640x480")
        self.assertEqual(video_row(recording)["video"], "set01_rep02.avi")
        self.assertEqual(video_row(recording)["duration"], "4.00 s")

    def test_recording_video_key_and_preference(self) -> None:
        row = {"video": "set01_rep02.avi"}

        self.assertEqual(recording_video_key({"recording_id": "set01_rep02"}, row), "set01_rep02")
        self.assertTrue(prefer_video_row({"video": "set01_rep02"}, row))
        self.assertFalse(prefer_video_row({"video": "set01_rep02.avi"}, {"video": "set01_rep02"}))


if __name__ == "__main__":
    unittest.main()
