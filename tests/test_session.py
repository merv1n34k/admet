import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from admet.core.project import ProjectStore
from admet.core.run import JsonlRunSink
from admet.core.session import (
    AdmetSession,
    SessionFile,
    SessionItem,
    content_cache_key,
    load_session,
    missing_files,
    new_session,
    resolve_session_path,
    save_session,
    session_path,
    validate_session,
)


class SessionProjectTests(unittest.TestCase):
    def test_session_path_uses_admetp_extension(self):
        self.assertEqual(session_path("run").name, "run.admetp")
        self.assertEqual(session_path("run.admetp").name, "run.admetp")

    def test_save_and_load_session_round_trip(self):
        session = AdmetSession(
            project_id="project-1",
            files=(
                SessionFile(
                    id="video-1",
                    path="data/video.avi",
                    role="input",
                    media_type="video/avi",
                ),
            ),
            items=(
                SessionItem(
                    id="sample-1",
                    project_type="analysis",
                    engine="opencv",
                    settings={"video_path": "data/video.avi"},
                    files=("video-1",),
                ),
            ),
            metadata={"operator": "admet"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = save_session(Path(tmpdir) / "project", session)
            loaded = load_session(project_path)
            self.assertTrue(project_path.is_dir())
            self.assertTrue((project_path / "manifest.json").is_file())
            self.assertTrue((project_path / "records").is_dir())
            self.assertFalse((project_path / "cache").exists())

        self.assertEqual(project_path.name, "project.admetp")
        self.assertEqual(loaded.project_id, "project-1")
        self.assertEqual(loaded.files[0].path, "data/video.avi")
        self.assertEqual(loaded.items[0].settings["video_path"], "data/video.avi")
        self.assertTrue(loaded.created_at)
        self.assertTrue(loaded.updated_at)

    def test_session_validates_references(self):
        session = new_session("project-1")
        broken = AdmetSession(
            project_id=session.project_id,
            items=(SessionItem("sample-1", "analysis", "opencv", files=("missing",)),),
        )

        with self.assertRaises(ValueError):
            validate_session(broken)

    def test_resolve_session_path_uses_project_directory(self):
        resolved = resolve_session_path("/tmp/project.admetp", "analysis/runs/run-1/raw.jsonl")

        self.assertEqual(resolved, Path("/tmp/project.admetp/analysis/runs/run-1/raw.jsonl"))

    def test_resolve_session_path_can_use_media_root(self):
        resolved = resolve_session_path(
            "/tmp/project.admetp",
            "media/video.avi",
            media_root="/srv/admet-media",
        )

        self.assertEqual(resolved, Path("/srv/admet-media/media/video.avi"))

    def test_save_relativizes_paths_inside_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "study.admetp"
            media_path = project_root / "records" / "video.avi"
            session = AdmetSession(
                project_id="study",
                files=(SessionFile("video-1", str(media_path), "input"),),
            )

            project_path = save_session(project_root, session)
            loaded = load_session(project_path / "manifest.json")

        self.assertEqual(loaded.files[0].path, "records/video.avi")

    def test_absolute_external_path_requires_metadata(self):
        session = AdmetSession(
            project_id="study",
            files=(SessionFile("video-1", "/nas/video.avi", "input"),),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(ValueError):
                save_session(Path(tmpdir) / "study", session)

            external = AdmetSession(
                project_id="study",
                files=(
                    SessionFile(
                        "video-1",
                        "/nas/video.avi",
                        "input",
                        metadata={"external": True},
                    ),
                ),
            )
            project_path = save_session(Path(tmpdir) / "study", external)
            loaded = load_session(project_path)

        self.assertEqual(loaded.files[0].path, "/nas/video.avi")

    def test_missing_files_reports_unavailable_media(self):
        session = AdmetSession(
            project_id="study",
            files=(
                SessionFile("present", "media/present.avi", "input"),
                SessionFile("missing", "media/missing.avi", "input"),
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "study.admetp"
            (root / "media").mkdir(parents=True)
            (root / "media" / "present.avi").write_bytes(b"video")
            missing = missing_files(session, root)

        self.assertEqual([item[0].id for item in missing], ["missing"])

    def test_from_dict_ignores_unknown_keys_and_defaults_missing_fields(self):
        loaded = AdmetSession.from_dict(
            {
                "project_id": "study",
                "unknown": "ignored",
                "files": [{"path": "media/video.avi", "extra": "ignored"}],
            }
        )

        self.assertEqual(loaded.files[0].id, "file-1")
        self.assertEqual(loaded.files[0].role, "media")

    def test_content_cache_key_uses_content_and_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path_a = Path(tmpdir) / "a.avi"
            path_b = Path(tmpdir) / "b.avi"
            path_a.write_bytes(b"same-content")
            path_b.write_bytes(b"same-content")

            key_a = content_cache_key(path_a, {"threshold": 1})
            key_b = content_cache_key(path_b, {"threshold": 1})
            key_c = content_cache_key(path_b, {"threshold": 2})

        self.assertEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_c)

    def test_project_store_registers_control_recordings_under_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectStore.create(Path(tmpdir) / "study", "study")
            target = store.control_recording_target("set01_rep02")
            target.video_path.parent.mkdir(parents=True)
            target.fluidics_csv_path.parent.mkdir(parents=True)
            target.video_path.write_bytes(b"avi")
            target.fluidics_csv_path.write_text("time,pressure\n0,1\n", encoding="utf-8")

            store.append_control_recording(
                {
                    "recording_id": target.recording_id,
                    "video_path": str(target.video_path),
                    "fluidics_csv": str(target.fluidics_csv_path),
                    "width": 640,
                    "height": 480,
                    "acquisition_fps": 29.8,
                    "converted_fps": 30.0,
                }
            )

            loaded = load_session(store.path)
            records = json.loads(
                (store.path / "records" / "metadata.json").read_text(encoding="utf-8")
            )

        self.assertEqual(records["recording_count"], 1)
        self.assertEqual(records["recordings"][0]["video_path"], f"records/camera/{target.recording_id}.avi")
        self.assertEqual(records["recordings"][0]["fluidics_csv"], f"records/fluidics/{target.recording_id}.csv")
        self.assertEqual(len(loaded.files), 2)
        self.assertEqual(loaded.files[0].path, f"records/camera/{target.recording_id}.avi")
        self.assertEqual(loaded.files[1].path, f"records/fluidics/{target.recording_id}.csv")
        self.assertEqual(loaded.items[0].id, "acq-records")
        self.assertEqual(loaded.items[0].metadata["recording_count"], 1)

    def test_project_store_registers_analysis_run_raw_sink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ProjectStore.create(Path(tmpdir) / "study", "study")
            store.session = replace(
                store.session,
                files=(
                    SessionFile(
                        "video-1",
                        "records/camera/video_01.avi",
                        "control_video",
                    ),
                ),
            )
            target = store.analysis_run_target("screen")
            with JsonlRunSink(target.raw_path) as sink:
                sink.write({"job_id": "job-1", "engine": "opencv", "values": {"count": 3}})
            store.finish_analysis_run(
                target,
                files=("video-1",),
                settings={"matrix": [{"file_id": "video-1", "engine": "opencv"}]},
                metadata={"row_count": 1},
            )

            loaded = load_session(store.path)
            analysis = json.loads(
                (store.path / "analysis" / "metadata.json").read_text(encoding="utf-8")
            )
            run_metadata = json.loads(target.run_metadata_path.read_text(encoding="utf-8"))

        self.assertEqual(analysis["run_count"], 1)
        self.assertEqual(run_metadata["raw_path"], f"analysis/runs/{target.run_id}/raw.jsonl")
        self.assertEqual(loaded.items[0].id, target.run_id)
        self.assertEqual(loaded.items[0].files, ("video-1",))
        self.assertEqual(loaded.items[0].metadata["run_path"], f"analysis/runs/{target.run_id}")
        self.assertEqual(loaded.items[0].metadata["raw_path"], f"analysis/runs/{target.run_id}/raw.jsonl")

if __name__ == "__main__":
    unittest.main()
