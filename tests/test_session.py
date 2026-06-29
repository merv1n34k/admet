import tempfile
import unittest
from pathlib import Path

from admet.core.engine import EngineContext
from admet.core.session import (
    AdmetSession,
    SessionCache,
    SessionFile,
    SessionItem,
    load_session,
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

        self.assertEqual(resolved, Path("/tmp/cache/opencv"))

    def test_engine_context_can_carry_session(self):
        session = new_session("project-1", "analysis")
        context = EngineContext(session=session)

        self.assertEqual(context.session.project_id, "project-1")


if __name__ == "__main__":
    unittest.main()
