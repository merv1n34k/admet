import json
import tempfile
import unittest
from pathlib import Path

from admet.analyze import AnalyzeBatchRunner, AnalyzeTarget, infer_engine
from admet.core.engine import ActionSpec, EngineRegistry, RunJob, RunResult
from admet.core.project import ProjectStore
from admet.core.schema import Param, ParamKind, ParamSchema
from admet.core.session import load_session
from admet.ui.analyze.renderer import AnalyzeWorkflowView
from admet.workflows import create_analyze_workflow


class AnalyzeBatchRunnerTests(unittest.TestCase):
    def test_infer_engine_from_source_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_dir = Path(tmpdir) / "images"
            image_dir.mkdir()

            self.assertEqual(infer_engine(Path(tmpdir) / "sample.avi"), "opencv")
            self.assertEqual(infer_engine(image_dir), "cellpose")

    def test_runner_writes_raw_project_run_without_project_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.avi"
            source.write_bytes(b"video")
            cache_root = root / "external-cache"
            project = root / "study.admetp"
            engine = FakeAnalyzeEngine("opencv")
            registry = EngineRegistry()
            registry.register("opencv", lambda: engine)

            report = AnalyzeBatchRunner(registry, cache_root=cache_root).run(
                (
                    AnalyzeTarget(
                        project_path=project,
                        source_path=source,
                        engine="opencv",
                        sample_id="set01",
                        settings={
                            "microns_per_pixel": 1.2,
                            "fps": 30.0,
                            "max_frames": 3,
                        },
                    ),
                )
            )

            session = load_session(project)
            raw_rows = [
                json.loads(line)
                for line in report.projects[0].raw_path.read_text(encoding="utf-8").splitlines()
            ]
            analysis_metadata = json.loads(
                (project / "analysis" / "metadata.json").read_text(encoding="utf-8")
            )

        self.assertEqual(len(report.projects), 1)
        self.assertEqual(report.jobs[0].status, "complete")
        self.assertEqual(report.jobs[0].sample_id, "set01")
        self.assertEqual(raw_rows[0]["engine"], "opencv")
        self.assertEqual(raw_rows[0]["file_id"], session.files[0].id)
        self.assertEqual(session.files[0].role, "analysis_video")
        self.assertTrue(session.files[0].metadata["external"])
        self.assertEqual(session.items[0].project_type, "analysis_run")
        self.assertEqual(session.items[0].files, (session.files[0].id,))
        self.assertIn("analysis/runs/", session.items[0].metadata["raw_path"])
        self.assertFalse((project / "cache").exists())
        self.assertEqual(analysis_metadata["run_count"], 1)
        self.assertTrue(engine.calls[0]["cache_dir"].is_relative_to(cache_root))

    def test_runner_groups_rows_by_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_a = root / "a.avi"
            source_b = root / "b.avi"
            source_a.write_bytes(b"a")
            source_b.write_bytes(b"b")
            registry = EngineRegistry()
            registry.register("opencv", lambda: FakeAnalyzeEngine("opencv"))

            report = AnalyzeBatchRunner(registry, cache_root=root / "cache-root").run(
                (
                    AnalyzeTarget(root / "one.admetp", source_a, engine="opencv"),
                    AnalyzeTarget(root / "two.admetp", source_b, engine="opencv"),
                )
            )

        self.assertEqual({project.project_path.name for project in report.projects}, {"one.admetp", "two.admetp"})
        self.assertEqual(len(report.jobs), 2)


class AnalyzeWorkflowViewTests(unittest.TestCase):
    def test_new_project_button_handler_creates_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "study.admetp"
            view = AnalyzeWorkflowView(
                create_analyze_workflow(),
                create_analyze_workflow().initial_state(),
                EngineRegistry(),
            )
            view.project_path = str(target)
            view._refresh = lambda: None

            view._new_project()

            self.assertEqual(Path(view.project_path).name, "study.admetp")
            self.assertEqual(view.notice_kind, "success")
            self.assertTrue((target / "manifest.json").is_file())

    def test_load_project_button_handler_populates_matrix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = root / "study.admetp"
            source = root / "sample.avi"
            source.write_bytes(b"video")
            store = ProjectStore.create(project, "study")
            store.register_analysis_file(source, engine="opencv", sample_id="set01")
            store.save()
            view = AnalyzeWorkflowView(
                create_analyze_workflow(),
                create_analyze_workflow().initial_state(),
                EngineRegistry(),
            )
            view.project_path = str(project)
            view._refresh = lambda: None

            view._load_project()

        self.assertEqual(view.notice_kind, "success")
        self.assertEqual(len(view.matrix), 1)
        self.assertEqual(view.matrix[0].sample_id, "set01")


class FakeAnalyzeEngine:
    name = "Fake Analyze"
    settings = ParamSchema(
        (
            Param("video_path", "Video Path", ParamKind.PATH, default="", required=True),
            Param("microns_per_pixel", "Microns Per Pixel", ParamKind.FLOAT, default=1.0),
            Param("fps", "FPS", ParamKind.FLOAT, default=0.0),
            Param("max_frames", "Max Frames", ParamKind.INTEGER, default=None),
        )
    )
    actions = (
        ActionSpec(
            "analyze",
            "Analyze",
            "analysis",
            params=("video_path", "microns_per_pixel", "fps", "max_frames"),
        ),
    )

    def __init__(self, engine_id: str):
        self.id = engine_id
        self.calls = []

    def run(self, job: RunJob) -> RunResult:
        self.calls.append(
            {
                "settings": dict(job.settings),
                "inputs": dict(job.inputs),
                "cache_dir": job.cache_dir,
                "metadata": dict(job.metadata),
            }
        )
        if job.sink is not None:
            job.sink.write(
                {
                    "job_id": job.id,
                    "item_id": job.metadata["item_id"],
                    "file_id": job.metadata["file_id"],
                    "engine": self.id,
                    "kind": "frame",
                    "values": {"frame": 0, "count": 1},
                }
            )
        return RunResult(
            job_id=job.id,
            engine=self.id,
            action=job.action,
            metadata={"row_count": 1, "cache_dir": str(job.cache_dir)},
        )


if __name__ == "__main__":
    unittest.main()
