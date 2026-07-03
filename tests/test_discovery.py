import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from admet.core.discovery import ENV_ROOT, discover_projects, projects_root


class DiscoveryTests(unittest.TestCase):
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
                project_type="combined",
                updated="2026-07-01T00:00:00",
                recording_count=2,
                run_count=1,
            )
            _write_project(
                root / "new.admetp",
                project_id="new",
                project_type="analysis",
                updated="2026-07-02T00:00:00",
                recording_count=0,
                run_count=3,
            )
            (root / "bare.admetp").mkdir()
            (root / "notes").mkdir()

            refs = discover_projects(root)

        self.assertEqual([ref.project_id for ref in refs], ["new", "old"])
        self.assertEqual(refs[0].project_type, "analysis")
        self.assertEqual(refs[0].recording_count, 0)
        self.assertEqual(refs[0].run_count, 3)
        self.assertEqual(refs[1].recording_count, 2)
        self.assertEqual(refs[1].run_count, 1)


def _write_project(
    path: Path,
    *,
    project_id: str,
    project_type: str,
    updated: str,
    recording_count: int,
    run_count: int,
) -> None:
    path.mkdir()
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "project_id": project_id,
                "project_type": project_type,
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
