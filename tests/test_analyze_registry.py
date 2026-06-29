import unittest

from admet.engines.analyze import create_analyze_registry


class AnalyzeRegistryTests(unittest.TestCase):
    def test_registry_lists_migrated_analysis_engines(self):
        registry = create_analyze_registry()

        self.assertEqual(registry.ids(), ("cellpose", "opencv"))

    def test_registry_creates_real_analysis_engines(self):
        registry = create_analyze_registry()

        opencv = registry.create("opencv")
        self.assertEqual(opencv.id, "opencv")
        self.assertIn("video_path", opencv.settings.defaults())
        cellpose = registry.create("cellpose")
        self.assertEqual(cellpose.id, "cellpose")
        self.assertIn("input_dir", cellpose.settings.defaults())


if __name__ == "__main__":
    unittest.main()
