import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from admet.core.discovery import ENV_ROOT
from admet.core.engine import ActionSpec, EngineRegistry, Param, ParamKind, ParamSchema
from admet.core.project import ProjectStore
from admet.core.run import RunJob, RunResult
from admet.core.session import load_session
from admet.ui.analyze.renderer import (
    AnalyzeWorkflowView,
    MatrixRow,
    StoredRun,
    read_raw_rows,
    summarize_raw_rows,
)
from admet.workflows import create_analyze_workflow
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

    def test_new_project_defaults_to_discovery_root(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {ENV_ROOT: tmpdir}):
            view = AnalyzeWorkflowView(
                create_analyze_workflow(),
                create_analyze_workflow().initial_state(),
                EngineRegistry(),
            )
            view._refresh = lambda: None

            view._new_project()

            project_path = Path(view.project_path)
            self.assertEqual(project_path.parent, Path(tmpdir).resolve())
            self.assertTrue((project_path / "manifest.json").is_file())

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

    def test_target_to_run_uses_row_specific_engine_settings(self):
        view = AnalyzeWorkflowView(
            create_analyze_workflow(),
            create_analyze_workflow().initial_state(),
            EngineRegistry(),
        )
        row = MatrixRow(
            uid="target-1",
            project_path="/tmp/study.admetp",
            source_path="/tmp/video.avi",
            engine="opencv",
            sample_id="set01",
            settings={
                "microns_per_pixel": 2.5,
                "fps": 150.0,
                "start_frame": 10,
                "end_frame": 120,
                "roi_x": 16,
                "roi_y": 24,
                "roi_width": 320,
                "roi_height": 0,
            },
        )

        target = view._target_to_run(row)

        self.assertEqual(target.settings["microns_per_pixel"], 2.5)
        self.assertEqual(target.settings["fps"], 150.0)
        self.assertEqual(target.settings["start_frame"], 10)
        self.assertEqual(target.settings["end_frame"], 120)
        self.assertEqual(target.settings["roi_x"], 16)
        self.assertEqual(target.settings["roi_y"], 24)
        self.assertEqual(target.settings["roi_width"], 320)
        self.assertEqual(target.settings["roi_height"], 720)

    def test_target_to_run_uses_row_specific_cellpose_settings(self):
        view = AnalyzeWorkflowView(
            create_analyze_workflow(),
            create_analyze_workflow().initial_state(),
            EngineRegistry(),
        )
        row = MatrixRow(
            uid="target-1",
            project_path="/tmp/study.admetp",
            source_path="/tmp/images",
            engine="cellpose",
            sample_id="set01",
            settings={
                "config_path": "/tmp/cellpose.json",
                "px_to_um": 1.9,
                "frame_limit": 20,
                "use_cache": False,
                "detect_inclusions": False,
            },
        )

        target = view._target_to_run(row)

        self.assertEqual(target.settings["config_path"], "/tmp/cellpose.json")
        self.assertEqual(target.settings["px_to_um"], 1.9)
        self.assertEqual(target.settings["frame_limit"], 20)
        self.assertFalse(target.settings["use_cache"])
        self.assertFalse(target.settings["detect_inclusions"])

    def test_raw_rows_are_summarized_for_view_plots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "study.admetp"
            run = StoredRun(project, "run01", project / "analysis" / "runs" / "run01" / "raw.jsonl", ())
            rows = [
                {
                    "engine": "cellpose",
                    "item_id": "set01",
                    "kind": "droplet",
                    "values": {"frame": 1, "diameter_um": 10.0, "inclusions": 2},
                },
                {
                    "engine": "cellpose",
                    "item_id": "set01",
                    "kind": "droplet",
                    "values": {"frame": 2, "diameter_um": 14.0, "inclusions": 1},
                },
            ]

            summaries = summarize_raw_rows(run, rows)

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].project, "study.admetp")
        self.assertEqual(summaries[0].sample_id, "set01")
        self.assertEqual(summaries[0].frames, 2)
        self.assertEqual(summaries[0].droplets, 2)
        self.assertEqual(summaries[0].inclusions, 3)
        self.assertEqual(summaries[0].mean_diameter, 12.0)
        self.assertGreater(summaries[0].cv_percent, 0)

    def test_raw_row_reader_ignores_invalid_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "study.admetp"
            raw_path = project / "analysis" / "runs" / "run01" / "raw.jsonl"
            raw_path.parent.mkdir(parents=True)
            raw_path.write_text(
                "\n".join(
                    (
                        json.dumps({"engine": "opencv", "values": {"frame": 1}}),
                        "not json",
                        "",
                        json.dumps({"engine": "opencv", "values": {"frame": 2}}),
                    )
                ),
                encoding="utf-8",
            )

            rows = read_raw_rows(project, raw_path)

        self.assertEqual([row["values"]["frame"] for row in rows], [1, 2])


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
        return RunResult(
            job_id=job.id,
            engine=self.id,
            action=job.action,
            metadata={"row_count": 1, "cache_dir": str(job.cache_dir)},
        )


if __name__ == "__main__":
    unittest.main()
