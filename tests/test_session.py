import tempfile
import unittest
from pathlib import Path

from admet.core.engine import EngineContext
from admet.core.session import (
    AdmetSession,
    SessionCache,
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
            project_type="analysis",
            files=(
                SessionFile(
                    id="video-1",
                    path="data/video.avi",
                    role="input",
                    media_type="video/avi",
                ),
            ),
            caches=(
                SessionCache(
                    id="opencv-cache",
                    path="cache/opencv",
                    engine="opencv",
                    file_id="video-1",
                ),
            ),
            items=(
                SessionItem(
                    id="sample-1",
                    project_type="analysis",
                    engine="opencv",
                    settings={"video_path": "data/video.avi"},
                    files=("video-1",),
                    caches=("opencv-cache",),
                ),
            ),
            metadata={"operator": "admet"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = save_session(Path(tmpdir) / "project", session)
            loaded = load_session(project_path)
            self.assertTrue(project_path.is_dir())
            self.assertTrue((project_path / "manifest.json").is_file())
            self.assertTrue((project_path / "media").is_dir())
            self.assertTrue((project_path / "cache").is_dir())

        self.assertEqual(project_path.name, "project.admetp")
        self.assertEqual(loaded.project_id, "project-1")
        self.assertEqual(loaded.files[0].path, "data/video.avi")
        self.assertEqual(loaded.caches[0].file_id, "video-1")
        self.assertEqual(loaded.items[0].settings["video_path"], "data/video.avi")
        self.assertTrue(loaded.created_at)
        self.assertTrue(loaded.updated_at)

    def test_session_validates_references(self):
        session = new_session("project-1", "analysis")
        broken = AdmetSession(
            project_id=session.project_id,
            project_type=session.project_type,
            items=(SessionItem("sample-1", "analysis", "opencv", files=("missing",)),),
        )

        with self.assertRaises(ValueError):
            validate_session(broken)

    def test_resolve_session_path_uses_project_directory(self):
        resolved = resolve_session_path("/tmp/project.admetp", "cache/opencv")

        self.assertEqual(resolved, Path("/tmp/project.admetp/cache/opencv"))

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
            media_path = project_root / "media" / "video.avi"
            cache_path = project_root / "cache" / "opencv"
            session = AdmetSession(
                project_id="study",
                project_type="combined",
                files=(SessionFile("video-1", str(media_path), "input"),),
                caches=(SessionCache("cache-1", str(cache_path), "opencv", "video-1"),),
            )

            project_path = save_session(project_root, session)
            loaded = load_session(project_path / "manifest.json")

        self.assertEqual(loaded.files[0].path, "media/video.avi")
        self.assertEqual(loaded.caches[0].path, "cache/opencv")

    def test_absolute_external_path_requires_metadata(self):
        session = AdmetSession(
            project_id="study",
            project_type="combined",
            files=(SessionFile("video-1", "/nas/video.avi", "input"),),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(ValueError):
                save_session(Path(tmpdir) / "study", session)

            external = AdmetSession(
                project_id="study",
                project_type="combined",
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
            project_type="combined",
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

        self.assertEqual(loaded.project_type, "combined")
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

    def test_engine_context_can_carry_session(self):
        session = new_session("project-1", "analysis")
        context = EngineContext(session=session)

        self.assertEqual(context.session.project_id, "project-1")


if __name__ == "__main__":
    unittest.main()
