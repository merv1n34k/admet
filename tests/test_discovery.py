import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from admet.core.discovery import ENV_ROOT, discover_projects, project_ref_label, projects_root, prune_recent_projects


class DiscoveryTests(unittest.TestCase):
    def test_counts_protocol_runs_and_all_registered_files_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "fluidics.admetp"
            _write_project(project, project_id="fluidics", updated="2026-09-30", recording_count=2, run_count=1)
            manifest = project / "manifest.json"
            data = json.loads(manifest.read_text())
            data["files"] = [
                {"role": "control_fluidics_csv", "path": "records/flow.csv"},
                {"role": "control_video", "path": "records/video.avi"},
                {"role": "protocol_run", "path": "records/protocols/run_a/summary.json"},
                {"role": "analysis_video", "path": "records/video.avi"},
            ]
            manifest.write_text(json.dumps(data))
            for name in ("run_a", "run_b"):
                directory = project / "records" / "protocols" / name
                directory.mkdir(parents=True)
                (directory / "summary.json").write_text('{"state": "cancelled"}')
            (project / "records" / "protocols" / "empty").mkdir()
            (project / "plans").mkdir()
            (project / "plans" / "unexecuted.json").write_text('{}')
            ref, = discover_projects(root)
            self.assertEqual(ref.run_count, 3)
            self.assertEqual(ref.file_count, 3)

    def test_recent_external_projects_are_merged_sorted_and_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            default = root / "projects"
            default.mkdir()
            local = default / "local.admetp"
            external = root / "external.admetp"
            for project in (local, external):
                _write_project(project, project_id=project.stem, updated="2026-09-30", recording_count=0, run_count=0)
            history = {str(local): "2026-09-29T10:00:00+00:00", str(external): "2026-09-30T10:00:00+00:00",
                       str(root / "missing.admetp"): "2026-01-01", str(root): "invalid project", "bad": None}
            recent = prune_recent_projects(history)
            self.assertEqual(set(recent), {str(local), str(external)})
            refs = discover_projects(default, recent=recent)
            self.assertEqual([ref.path for ref in refs], [external, local])
            self.assertEqual(refs[0].last_opened, history[str(external)])
            self.assertIn("last opened: 2026-09-30", project_ref_label(refs[0]))
            self.assertIn("0 runs", project_ref_label(refs[0]))
            self.assertNotIn("files", project_ref_label(refs[0]))
            self.assertEqual(len(discover_projects(root / "absent", recent=recent)), 2)
            self.assertEqual(prune_recent_projects(["bad"]), {})

    def test_projects_root_precedence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_root = root / "env"
            explicit_root = root / "explicit"
            with patch.dict(os.environ, {ENV_ROOT: str(env_root)}):
                self.assertEqual(projects_root(), env_root.resolve())
                self.assertEqual(projects_root(explicit_root), explicit_root.resolve())

    def test_projects_root_defaults_to_projects_dir_when_present(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {}, clear=True):
            root = Path(tmpdir)
            (root / "projects").mkdir()
            with patch("pathlib.Path.cwd", return_value=root):
                self.assertEqual(projects_root(), (root / "projects").resolve())

    def test_discover_projects_orders_by_updated_and_skips_bare_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_project(
                root / "old.admetp",
                project_id="old",
                updated="2026-07-01T00:00:00",
                recording_count=2,
                run_count=1,
            )
            _write_project(
                root / "new.admetp",
                project_id="new",
                updated="2026-07-02T00:00:00",
                recording_count=0,
                run_count=3,
            )
            (root / "bare.admetp").mkdir()
            (root / "notes").mkdir()

            refs = discover_projects(root)

        self.assertEqual([ref.project_id for ref in refs], ["new", "old"])
        self.assertEqual(refs[0].recording_count, 0)
        self.assertEqual(refs[0].run_count, 3)
        self.assertEqual(refs[1].recording_count, 2)
        self.assertEqual(refs[1].run_count, 1)


def _write_project(
    path: Path,
    *,
    project_id: str,
    updated: str,
    recording_count: int,
    run_count: int,
) -> None:
    path.mkdir()
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "project_id": project_id,
                "updated_at": updated,
            }
        ),
        encoding="utf-8",
    )
    (path / "records").mkdir()
    (path / "records" / "metadata.json").write_text(
        json.dumps({"recording_count": recording_count}),
        encoding="utf-8",
    )
    (path / "analysis").mkdir()
    (path / "analysis" / "metadata.json").write_text(
        json.dumps({"run_count": run_count}),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
