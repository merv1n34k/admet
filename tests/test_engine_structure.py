import unittest
from pathlib import Path


ENGINE_ROOT = Path(__file__).resolve().parents[1] / "src" / "admet" / "engines"


class EngineStructureTests(unittest.TestCase):
    def test_engine_targets_are_directory_packages(self):
        root_entries = {path.name for path in ENGINE_ROOT.iterdir() if path.name != "__pycache__"}

        self.assertEqual(
            root_entries,
            {"__init__.py", "registry.py", "acquisition", "opencv", "cellpose"},
        )
        for package in ("acquisition", "opencv", "cellpose"):
            self.assertTrue((ENGINE_ROOT / package / "__init__.py").is_file())

    def test_removed_engine_group_dirs_do_not_exist(self):
        self.assertFalse((ENGINE_ROOT / "analyze").exists())
        self.assertFalse((ENGINE_ROOT / "control").exists())

    def test_engine_packages_do_not_define_ui_or_workflow_schemas(self):
        forbidden = ("admet.ui", "admet.workflows", "Param(", "ParamKind", "ParamOption", "ParamSchema(")
        for path in ENGINE_ROOT.rglob("*.py"):
            if path.name == "registry.py":
                continue
            source = path.read_text(encoding="utf-8")
            for token in forbidden:
                with self.subTest(path=path.relative_to(ENGINE_ROOT), token=token):
                    self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
