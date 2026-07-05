import json
import tempfile
import unittest
from pathlib import Path

from admet.core.engine import ActionSpec, EngineRegistry, Param, ParamKind, ParamSchema
from admet.core.run import RunJob, RunResult
from admet.core.session import load_session
from admet.workflows.analyze import AnalyzeBatchRunner, AnalyzeTarget, infer_engine


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
                            "start_frame": 0,
                            "end_frame": 3,
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

    def test_runner_reports_monotonic_intra_file_progress(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_a = root / "a.avi"
            source_b = root / "b.avi"
            source_a.write_bytes(b"a")
            source_b.write_bytes(b"b")
            registry = EngineRegistry()
            registry.register("opencv", lambda: FakeAnalyzeEngine("opencv"))

            seen: list[float] = []
            AnalyzeBatchRunner(registry, cache_root=root / "cache-root").run(
                (
                    AnalyzeTarget(root / "one.admetp", source_a, engine="opencv"),
                    AnalyzeTarget(root / "two.admetp", source_b, engine="opencv"),
                ),
                on_progress=lambda pct: seen.append(round(pct, 3)),
            )

        self.assertEqual(seen[0], 0.0)
        self.assertEqual(seen[-1], 100.0)
        self.assertEqual(seen, sorted(seen))
        # two files: 50% of file 1 -> 25% overall, 50% of file 2 -> 75% overall
        self.assertIn(25.0, seen)
        self.assertIn(75.0, seen)

    def test_runner_skips_unknown_source_without_aborting_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "notes.txt"
            source.write_text("not analysis input", encoding="utf-8")
            registry = EngineRegistry()
            registry.register("opencv", lambda: FakeAnalyzeEngine("opencv"))

            report = AnalyzeBatchRunner(registry, cache_root=root / "cache-root").run(
                (
                    AnalyzeTarget(root / "study.admetp", source),
                )
            )
            session = load_session(root / "study.admetp")
            metadata = json.loads(
                (root / "study.admetp" / "analysis" / "metadata.json").read_text(encoding="utf-8")
            )

        self.assertEqual(len(report.jobs), 1)
        self.assertEqual(report.jobs[0].status, "skipped")
        self.assertIn("cannot infer analyze engine", report.jobs[0].warnings[0])
        self.assertEqual(session.files, ())
        self.assertEqual(metadata["runs"][0]["metadata"]["jobs"][0]["status"], "skipped")


class FakeAnalyzeEngine:
    name = "Fake Analyze"
    settings = ParamSchema(
        (
            Param("video_path", "Video Path", ParamKind.PATH, default="", required=True),
            Param("microns_per_pixel", "Microns Per Pixel", ParamKind.FLOAT, default=1.0),
            Param("fps", "FPS", ParamKind.FLOAT, default=0.0),
            Param("start_frame", "Start Frame", ParamKind.INTEGER, default=0),
            Param("end_frame", "End Frame", ParamKind.INTEGER, default=None),
        )
    )
    actions = (
        ActionSpec(
            "analyze",
            "Analyze",
            "analysis",
            params=("video_path", "microns_per_pixel", "fps", "start_frame", "end_frame"),
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
        if job.progress is not None:
            job.progress(50, "half")
            job.progress(100, "done")
        return RunResult(
            job_id=job.id,
            engine=self.id,
            action=job.action,
            metadata={"row_count": 1, "cache_dir": str(job.cache_dir)},
        )


if __name__ == "__main__":
    unittest.main()
