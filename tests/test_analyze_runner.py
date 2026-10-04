import json
import tempfile
import unittest
from pathlib import Path

from admet.core.engine import ActionSpec, EngineRegistry, Param, ParamKind, ParamSchema
from admet.core.run import RunJob, RunResult
from admet.core.session import load_session
from admet.workflows.analyze_runner import AnalyzeBatchRunner, AnalyzeTarget, infer_engine


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
        # single, stable analysis output -- no timestamped run directories
        self.assertEqual(session.items[0].metadata["raw_path"], "analysis/raw.jsonl")
        self.assertFalse((project / "analysis" / "runs").exists())
        self.assertFalse((project / "cache").exists())
        self.assertEqual(analysis_metadata["run_count"], 1)
        self.assertEqual(session.metadata["cache_root"], str(cache_root))
        self.assertTrue(engine.calls[0]["cache_dir"].is_relative_to(cache_root))

    def test_rerunning_same_batch_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.avi"
            source.write_bytes(b"video")
            project = root / "study.admetp"
            registry = EngineRegistry()
            registry.register("opencv", lambda: FakeAnalyzeEngine("opencv"))
            runner = AnalyzeBatchRunner(registry, cache_root=root / "cache")
            target = AnalyzeTarget(
                project_path=project, source_path=source, engine="opencv", sample_id="s1"
            )

            first = runner.run((target,))
            first_rows = first.projects[0].raw_path.read_text(encoding="utf-8").splitlines()
            second = runner.run((target,))
            raw_path = second.projects[0].raw_path
            second_rows = raw_path.read_text(encoding="utf-8").splitlines()

            analysis_metadata = json.loads(
                (project / "analysis" / "metadata.json").read_text(encoding="utf-8")
            )
            session = load_session(project)

        # one stable output path, reused across runs
        self.assertEqual(first.projects[0].raw_path, raw_path)
        self.assertEqual(raw_path.name, "raw.jsonl")
        # data is rewritten in place, not duplicated
        self.assertGreater(len(first_rows), 0)
        self.assertEqual(len(second_rows), len(first_rows))
        # exactly one run / one analysis item, no timestamped run dirs
        self.assertEqual(analysis_metadata["run_count"], 1)
        self.assertEqual(len(session.items), 1)
        self.assertFalse((project / "analysis" / "runs").exists())

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

        # the live report surfaces the skip for this run...
        self.assertEqual(len(report.jobs), 1)
        self.assertEqual(report.jobs[0].status, "skipped")
        self.assertIn("cannot infer analyze engine", report.jobs[0].warnings[0])
        self.assertEqual(session.files, ())
        # ...but nothing is persisted for a skipped target -- no data means no record
        self.assertEqual(metadata["runs"][0]["metadata"]["jobs"], [])


class StopTests(unittest.TestCase):
    """Ctrl+C on the server: finish the file in hand, or stop now, and keep finished work."""

    def batch(self, root, engine, stop):
        registry = EngineRegistry()
        registry.register("opencv", lambda: engine)
        sources = []
        for name in ("a", "b"):
            source = root / f"{name}.avi"
            source.write_bytes(name.encode())
            sources.append(source)
        targets = tuple(AnalyzeTarget(project_path=root / "study.admetp", source_path=source,
                                      engine="opencv", sample_id=source.stem) for source in sources)
        return AnalyzeBatchRunner(registry, cache_root=root / "cache").run(targets, stop=stop)

    def test_stop_after_the_current_file_saves_it_and_skips_the_rest(self):
        from admet.workflows.analyze_runner import AnalysisStop

        stop = AnalysisStop()
        engine = FakeAnalyzeEngine("opencv")
        original = engine.run
        engine.run = lambda job: (stop.after_file.set(), original(job))[1]   # Ctrl+C during file a
        with tempfile.TemporaryDirectory() as tmpdir:
            report = self.batch(Path(tmpdir), engine, stop)
            raw = report.projects[0].raw_path.read_text(encoding="utf-8")

        self.assertEqual([job.status for job in report.jobs], ["complete", "stopped"])
        self.assertIn('"item_id": "a"', raw)
        self.assertNotIn('"item_id": "b"', raw)

    def test_stop_now_abandons_the_file_in_hand_and_resumes_from_cache(self):
        from admet.workflows.analyze_runner import AnalysisStop

        stop = AnalysisStop()
        engine = FakeAnalyzeEngine("opencv")
        original = engine.run

        def run(job):
            if job.metadata["item_id"] == "b":
                stop.now.set()                     # second Ctrl+C while file b runs
                job.progress(50, "half way")
            return original(job)

        engine.run = run
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report = self.batch(root, engine, stop)
            self.assertEqual([job.status for job in report.jobs], ["complete", "stopped"])
            self.assertEqual(len(list((root / "cache").rglob("result.json"))), 1)   # b left unfinished
            self.assertIn('"item_id": "a"', report.projects[0].raw_path.read_text(encoding="utf-8"))

            engine.run = original
            resumed = self.batch(root, engine, AnalysisStop())

        self.assertEqual([job.status for job in resumed.jobs], ["cached", "complete"])


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
            "run_analysis",
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
