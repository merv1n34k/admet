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

    def test_project_store_upserts_files_by_role_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = ProjectStore.create(root / "study", "study")
            media = store.path / "media" / "movie.avi"
            media.parent.mkdir()
            media.write_bytes(b"avi")
            store.update_metadata(cache_root=str(root / "cache"))

            stored = store.upsert_file_path(
                media,
                role="analysis_video",
                media_type="video/avi",
                metadata={"engine": "opencv"},
                id_hint="analysis-opencv-movie",
            )
            store.save()
            loaded = ProjectStore(store.path)

        self.assertEqual(loaded.session.metadata["cache_root"], str(root / "cache"))
        self.assertEqual(stored.path, "media/movie.avi")
        self.assertEqual(len(loaded.files_by_role(("analysis_video",))), 1)
        self.assertEqual(loaded.resolve_file_path(loaded.session.files[0]), media)

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
        # single, stable analysis output at the analysis root -- no timestamped dirs
        self.assertEqual(target.run_id, "analysis")
        self.assertEqual(run_metadata["raw_path"], "analysis/raw.jsonl")
        self.assertEqual(loaded.items[0].id, target.run_id)
        self.assertEqual(loaded.items[0].files, ("video-1",))
        self.assertEqual(loaded.items[0].metadata["run_path"], "analysis")
        self.assertEqual(loaded.items[0].metadata["raw_path"], "analysis/raw.jsonl")


class SystemCheckRecordTests(unittest.TestCase):
    def _store(self, tmpdir):
        return ProjectStore.create(Path(tmpdir) / "project", "project-1")

    def test_a_check_is_written_as_its_own_json_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)

            path = store.append_system_check(
                {"kind": "flow", "recorded_at": "2026-08-20T10:00:00+00:00", "flow_checks": []},
                summary="flow check: feasible",
            )

            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(path.parent, store.path / "records" / "checks")
            self.assertEqual(written["kind"], "flow")
            self.assertEqual(written["check_id"], path.stem)

    def test_checks_accumulate_instead_of_overwriting(self):
        # Comparing this chip against the last one is the point, so an earlier
        # check must survive a later one.
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)

            first = store.append_system_check({"kind": "flow"}, summary="first")
            second = store.append_system_check({"kind": "dispense"}, summary="second")

            self.assertNotEqual(first, second)
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())
            item = next(item for item in store.session.items if item.id == "system-checks")
            self.assertEqual(item.metadata["check_count"], 2)
            self.assertEqual(item.metadata["latest_summary"], "second")

    def test_a_run_rewrites_its_own_record_rather_than_adding_one(self):
        # A run writes as it starts and again as it ends. Both writes are the same
        # run, so they are the same record -- but a second run is a second record.
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)

            started = store.append_system_check({"kind": "flow", "status": "started"}, summary="started")
            check_id = json.loads(started.read_text(encoding="utf-8"))["check_id"]
            ended = store.append_system_check(
                {"kind": "flow", "status": "complete"}, summary="done", check_id=check_id
            )
            other_run = store.append_system_check({"kind": "flow"}, summary="second run")

            self.assertEqual(started, ended)
            self.assertNotEqual(started, other_run)
            self.assertEqual(json.loads(ended.read_text(encoding="utf-8"))["status"], "complete")
            item = next(item for item in store.session.items if item.id == "system-checks")
            self.assertEqual(item.metadata["check_count"], 2)

    def test_a_stored_check_survives_a_reload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            store.append_system_check({"kind": "flow"}, summary="flow check: feasible")

            loaded = load_session(store.path)

            file = next(file for file in loaded.files if file.role == "system_check")
            self.assertFalse(Path(file.path).is_absolute())
            self.assertEqual(file.media_type, "application/json")
            self.assertEqual(file.metadata["summary"], "flow check: feasible")
            item = next(item for item in loaded.items if item.id == "system-checks")
            self.assertEqual(item.files, (file.id,))


if __name__ == "__main__":
    unittest.main()
