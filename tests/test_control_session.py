import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from admet.core.engine import EngineResult
from admet.core.schema import ResultSet
from admet.core.session import load_session, new_session, save_session
from admet.core.workflow import StageStatus
from admet.engines.control import RecordingSession
from admet.engines.control.settings import CAMERA_SETTINGS, CORRECTION_SETTINGS
from admet.ui.control.window import ControlWindow


class FakeWriter:
    def __init__(self, video_dir, prefix, width, height, fps):
        self.video_dir = Path(video_dir)
        self.prefix = prefix
        self.width = width
        self.height = height
        self.fps = fps
        self.frame_count = 7
        self.path = self.video_dir / f"{prefix}.avi"

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
            csv_path = str(Path(settings["fluidics_dir"]) / settings["csv_filename"])
            return EngineResult(ResultSet(), artifacts={"csv_path": csv_path})
        return EngineResult(ResultSet())


class RecordingSessionTests(unittest.TestCase):
    def test_recording_keeps_preview_on_by_default(self):
        self.assertFalse(CAMERA_SETTINGS.defaults()["camera_preview_off_recording"])

    def test_correction_action_params_show_calibration_scale_first(self):
        window = ControlWindow.__new__(ControlWindow)
        stage = SimpleNamespace(id="corrections")

        ordered = window._ordered_params(stage, list(CORRECTION_SETTINGS.params))
        collapsed = window._collapsed_params(stage, ordered)

        self.assertEqual(
            [param.name for param in collapsed],
            [
                "oil_l_calibration",
                "oil_l_scale",
                "cells_m_calibration",
                "cells_m_scale",
                "beads_m_calibration",
                "beads_m_scale",
            ],
        )
        self.assertEqual(
            [param.name for param in ordered[6:]],
            [
                "oil_l_offset",
                "oil_l_quadratic",
                "cells_m_offset",
                "cells_m_quadratic",
                "beads_m_offset",
                "beads_m_quadratic",
            ],
        )

    def test_runs_pipeline_completion_waits_for_run_confirmation(self):
        window = ControlWindow.__new__(ControlWindow)
        window.workflow = SimpleNamespace(current_stage=lambda _state: SimpleNamespace(id="runs"))
        window.workflow_state = SimpleNamespace(statuses={"runs": StageStatus.ACTIVE})
        window._latest_pipeline_event = object()
        window._runs_completion_confirmed = False
        window._clear_pipeline_confirmation = lambda: None
        window._dismiss_notification = lambda: None
        completions = []
        window._complete_current_stage = lambda: completions.append(True)

        window._complete_completed_pipeline_stage()
        self.assertEqual(completions, [])

        window._runs_completion_confirmed = True
        window._complete_completed_pipeline_stage()

        self.assertEqual(completions, [True])
        self.assertFalse(window._runs_completion_confirmed)

    def test_completed_pipeline_stage_finish_is_deferred(self):
        window = ControlWindow.__new__(ControlWindow)
        stage = SimpleNamespace(id="priming")
        window.workflow = SimpleNamespace(current_stage=lambda _state: stage)
        window.workflow_state = SimpleNamespace(statuses={"priming": StageStatus.ACTIVE})
        window._runs_completion_confirmed = False
        window._completion_pending = False
        window._latest_pipeline_event = object()
        window._clear_pipeline_confirmation = lambda: None
        window._dismiss_notification = lambda: None
        completions = []
        refreshes = []
        window._refresh_action_box = lambda refreshed_stage: refreshes.append(refreshed_stage.id)
        window._complete_current_stage = lambda: completions.append(True)
        callbacks = []

        with patch("admet.ui.control.window.QTimer.singleShot", side_effect=lambda ms, cb: callbacks.append((ms, cb))):
            window._schedule_completed_pipeline_stage_finish(stage)
            window._schedule_completed_pipeline_stage_finish(stage)

        self.assertEqual(refreshes, ["priming"])
        self.assertEqual(completions, [])
        self.assertTrue(window._completion_pending)
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0][0], 500)

        callbacks[0][1]()

        self.assertEqual(completions, [True])
        self.assertFalse(window._completion_pending)

    def test_action_box_refresh_syncs_without_rebuild_when_structure_matches(self):
        window = ControlWindow.__new__(ControlWindow)
        stage = SimpleNamespace(id="priming")
        calls = []
        window._mounted_signature = ("priming",)
        window._refresh_runtime_state = lambda: calls.append("runtime")
        window._structure_signature = lambda refreshed_stage: ("priming",)
        window._sync_action_box = lambda refreshed_stage: calls.append("sync")
        window._sync_toc = lambda: calls.append("toc")
        window._render_current_stage = lambda: calls.append("render")

        window._refresh_action_box(stage)

        self.assertEqual(calls, ["runtime", "sync", "toc"])

    def test_start_and_stop_recording_writes_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_dir = Path(tmpdir) / "control_20260701_120000"
            camera = FakeCameraRecorder()
            control = FakeControlBackend()
            session = RecordingSession(
                report_dir,
                camera,
                control,
                writer_factory=FakeWriter,
            )

            started = session.start_recording(
                "set01_rep01",
                width=640,
                height=240,
                fps=120.0,
                max_frames=10,
            )
            stopped = session.stop_recording()

            report_dir = session.report_dir
            metadata = json.loads((report_dir / "metadata.json").read_text(encoding="utf-8"))

        self.assertTrue(started.video_prefix.startswith("set01_rep01_"))
        self.assertEqual(started.fluidics_csv, str(report_dir / "fluidics" / f"{started.video_prefix}.csv"))
        self.assertEqual(report_dir.name, "control_20260701_120000")
        self.assertEqual(camera.start_args, (10, None))
        self.assertEqual(control.actions[0][0], "start_recording")
        self.assertEqual(Path(control.actions[0][1]["fluidics_dir"]).name, "fluidics")
        self.assertEqual(control.actions[1][0], "stop_recording")
        self.assertEqual(stopped.frames_recorded, 5)
        self.assertEqual(stopped.frames_written, 7)
        self.assertEqual(stopped.converted_fps, 120.0)
        self.assertGreater(stopped.acquisition_fps, 0.0)
        self.assertEqual(Path(stopped.video_path).name, f"{started.video_prefix}.avi")
        self.assertEqual(metadata["recording_count"], 1)
        self.assertEqual(metadata["recordings"][0]["video_prefix"], started.video_prefix)
        self.assertEqual(metadata["recordings"][0]["converted_fps"], 120.0)
        self.assertGreater(metadata["recordings"][0]["acquisition_fps"], 0.0)

    def test_camera_auto_stop_finalizes_full_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_dir = Path(tmpdir) / "control_20260701_120000"
            camera = FakeCameraRecorder()
            control = FakeControlBackend()
            session = RecordingSession(
                report_dir,
                camera,
                control,
                writer_factory=FakeWriter,
            )

            session.start_recording(
                "set01_rep01",
                width=640,
                height=240,
                fps=120.0,
                max_frames=10,
            )
            camera.complete_from_camera_limit()

            report_dir = session.report_dir
            metadata = json.loads((report_dir / "metadata.json").read_text(encoding="utf-8"))

        self.assertIsNone(session.current)
        self.assertEqual(control.actions[0][0], "start_recording")
        self.assertEqual(control.actions[1][0], "stop_recording")
        self.assertEqual(metadata["recording_count"], 1)
        self.assertEqual(metadata["recordings"][0]["frames_recorded"], 5)
        self.assertEqual(metadata["recordings"][0]["frames_written"], 7)

    def test_recording_artifact_registers_video_csv_and_acquisition_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "study.admetp"
            session = save_session(project, new_session("study", "combined"))
            acq_dir = session / "media" / "control" / "control_20260701_120000"
            video_path = acq_dir / "video" / "set01_rep02_20260701_120000.avi"
            csv_path = acq_dir / "fluidics" / "set01_rep02_20260701_120000.csv"
            second_video_path = acq_dir / "video" / "set01_rep03_20260701_120030.avi"
            second_csv_path = acq_dir / "fluidics" / "set01_rep03_20260701_120030.csv"
            video_path.parent.mkdir(parents=True)
            csv_path.parent.mkdir(parents=True)
            video_path.write_bytes(b"AVI")
            csv_path.write_text("timestamp,elapsed_s\n", encoding="utf-8")
            second_video_path.write_bytes(b"AVI")
            second_csv_path.write_text("timestamp,elapsed_s\n", encoding="utf-8")

            window = ControlWindow.__new__(ControlWindow)
            window.api = SimpleNamespace(
                session=load_session(session),
                engine=SimpleNamespace(id="fluidics"),
                workdir=str(session),
            )
            window.project_path = session
            window._append_log = lambda _message: None

            window._store_recording_artifact(
                {
                    "recording_id": "set01_rep02_20260701_120000",
                    "report_dir": str(acq_dir),
                    "video_path": str(video_path),
                    "fluidics_csv": str(csv_path),
                    "width": 640,
                    "height": 240,
                    "converted_fps": 120.0,
                    "acquisition_fps": 118.5,
                }
            )
            window._store_recording_artifact(
                {
                    "recording_id": "set01_rep03_20260701_120030",
                    "report_dir": str(acq_dir),
                    "video_path": str(second_video_path),
                    "fluidics_csv": str(second_csv_path),
                    "width": 640,
                    "height": 240,
                    "converted_fps": 120.0,
                    "acquisition_fps": 117.5,
                }
            )

            saved = load_session(session)

        video_files = [file for file in saved.files if file.role == "control_video"]
        csv_files = [file for file in saved.files if file.role == "control_fluidics_csv"]
        self.assertEqual(len(video_files), 2)
        self.assertEqual(len(csv_files), 2)
        self.assertIn(
            "media/control/control_20260701_120000/video/set01_rep02_20260701_120000.avi",
            {file.path for file in video_files},
        )
        self.assertIn(
            "media/control/control_20260701_120000/fluidics/set01_rep02_20260701_120000.csv",
            {file.path for file in csv_files},
        )
        self.assertEqual(len(saved.items), 1)
        self.assertEqual(saved.items[0].project_type, "control_acquisition")
        self.assertEqual(len(saved.items[0].files), 4)

    def test_new_project_uses_selected_path_name_and_saves_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "study.admetp"
            window = ControlWindow.__new__(ControlWindow)
            window.api = SimpleNamespace(session=None, workdir=None)
            window.project_path = None
            window.project_badge = None
            window._set_status = lambda *_args: None
            window._notify = lambda *_args, **_kwargs: None
            window._append_log = lambda _message: None
            window._render_current_stage = lambda: None

            selected_paths = []

            def select_path(_parent, _title, suggested, _filter):
                selected_paths.append(Path(suggested).name)
                return str(target), ""

            with patch(
                "admet.ui.control.window.QFileDialog.getSaveFileName",
                side_effect=select_path,
            ):
                window._new_project()

            saved = load_session(target)
            manifest_exists = (target / "manifest.json").exists()

        self.assertEqual(window.api.session.project_id, "study")
        self.assertEqual(saved.project_id, "study")
        self.assertEqual(window.project_path, target)
        self.assertEqual(window.api.workdir, str(target))
        self.assertTrue(manifest_exists)
        self.assertRegex(selected_paths[0], r"^admet_\d{8}_\d{6}\.admetp$")


if __name__ == "__main__":
    unittest.main()
