import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from admet.core.engine import RunJob
from admet.core.run import JsonlRunSink
from admet.engines.analyze.cellpose.cache import Cache
from admet.engines.analyze.cellpose.config import load_config
from admet.engines.analyze.cellpose.correction import update_results_with_inclusions
from admet.engines.analyze.cellpose.detection import CellposeDetection, CellposeUnavailableError
from admet.engines.analyze.cellpose.engine import create_engine
from admet.engines.analyze.cellpose.scanprotocol import build_layout, field_cells


class CellposeHelperTests(unittest.TestCase):
    def test_load_config_deep_merges_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "min_droplet_diameter": 100,
                        "cache": {"enabled": False, "dir": "custom-cache"},
                        "settings": {"inclusions": False},
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config["min_droplet_diameter"], 100)
        self.assertFalse(config["cache"]["enabled"])
        self.assertEqual(config["cache"]["dir"], "custom-cache")
        self.assertEqual(config["cache"]["max_frames"], 100)
        self.assertFalse(config["settings"]["inclusions"])
        self.assertEqual(config["settings"]["dilution"], 500)

    def test_cache_uses_configurable_dir_and_inclusion_hash_inputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "dropdrop-cache"
            config = {
                "cellpose_flow_threshold": 0.4,
                "cellpose_cellprob_threshold": 0.0,
                "min_droplet_diameter": 80,
                "max_droplet_diameter": 200,
                "erosion_pixels": 5,
                "kernel_size": 7,
                "tophat_threshold": 30,
                "min_inclusion_area": 7,
                "max_inclusion_area": 50,
                "edge_buffer": 5,
                "px_to_um": 1.14,
                "cache": {"enabled": True, "max_frames": 1, "dir": str(cache_dir)},
            }

            cache = Cache(config)
            cache.save_frame(
                "field-001.tif",
                np.array([[1, 2], [3, 4]], dtype=np.uint8),
                [(10, 12, 20)],
            )

            self.assertEqual(cache.cache_dir, cache_dir)
            self.assertTrue(cache.is_valid("field-001.tif"))
            cached = cache.load_frame("field-001.tif")
            self.assertTrue(
                np.array_equal(cached["min_projection"], np.array([[1, 2], [3, 4]]))
            )
            self.assertEqual(cached["droplet_coords"], [(10, 12, 20)])

            changed = dict(config)
            changed["min_inclusion_area"] = 9
            self.assertNotEqual(cache.get_config_hash(), Cache(changed).get_config_hash())

    def test_field_cells_supports_serpentine_vertical_layout(self):
        cells = field_cells(6, cols=2, rows=3, pattern="SerpentineVertical")

        self.assertEqual(cells, [(0, 0), (1, 0), (2, 0), (2, 1), (1, 1), (0, 1)])

    def test_build_layout_parses_evos_scanprotocol(self):
        xml = """\
<ScanProtocol>
  <FieldSequencePattern>SerpentineVertical</FieldSequencePattern>
  <Extents>
    <Point><_x>0</_x><_y>0</_y></Point>
    <Point><_x>100</_x><_y>0</_y></Point>
    <Point><_x>100</_x><_y>200</_y></Point>
    <Point><_x>0</_x><_y>200</_y></Point>
  </Extents>
</ScanProtocol>
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            protocol = Path(tmpdir) / "sample.scanprotocol"
            protocol.write_text(xml, encoding="utf-8")

            layout = build_layout(tmpdir, n_fields=6)

        self.assertIsNotNone(layout)
        self.assertEqual(layout["pattern"], "SerpentineVertical")
        self.assertEqual(layout["cols"], 2)
        self.assertEqual(layout["rows"], 4)
        self.assertEqual(layout["cells"][3], [3, 0])

    def test_update_results_with_inclusions_counts_points_and_skips_disabled(self):
        results = [
            {"frame": 0, "droplet_id": 1, "center_x": 10, "center_y": 10, "diameter_px": 20},
            {"frame": 0, "droplet_id": 2, "center_x": 100, "center_y": 100, "diameter_px": 10},
            {"frame": 1, "droplet_id": 3, "center_x": 30, "center_y": 30, "diameter_px": 10},
        ]
        inclusions = {
            0: [(10, 10), (19, 10), (30, 30)],
            1: [(31, 31)],
        }

        corrected = update_results_with_inclusions(
            results,
            inclusions,
            disabled_droplets={0: {2}},
        )

        self.assertEqual([row["droplet_id"] for row in corrected], [1, 3])
        self.assertEqual([row["inclusions"] for row in corrected], [2, 1])
        self.assertEqual([row["detected"] for row in corrected], [True, True])

    def test_detection_parses_and_groups_evos_filenames(self):
        detector = CellposeDetection(use_cache=False)

        self.assertEqual(detector.parse_filename("image_z01_a01f05d4.tif"), (1, 5))
        self.assertEqual(detector.parse_filename("image_a01f10d4.tif"), (0, 10))
        self.assertEqual(detector.parse_filename("not-a-field.tif"), (0, None))

        with tempfile.TemporaryDirectory() as tmpdir:
            for name in ("image_z02_a01f05d4.tif", "image_z01_a01f05d4.tif", "skip.tif"):
                (Path(tmpdir) / name).touch()

            groups = detector.load_and_group_images(tmpdir)

        self.assertEqual(list(groups), [5])
        self.assertEqual([z_index for z_index, _ in groups[5]], [1, 2])

    def test_missing_cellpose_raises_import_error(self):
        detector = CellposeDetection(use_cache=False)
        original_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "cellpose.models":
                raise ImportError("cellpose unavailable")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import), self.assertRaises(
            CellposeUnavailableError
        ):
            detector.detect_droplets_cellpose(np.zeros((4, 4), dtype=np.uint8))

    def test_cellpose_engine_returns_empty_result_for_empty_input(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "empty-sample"
            input_dir.mkdir()

            result = create_engine().run_action("analyze", {"input_dir": str(input_dir)})

        stats = {stat.name: stat.value for stat in result.result_set.stats}
        self.assertEqual(result.result_set.records, ())
        self.assertEqual(stats["total_droplets"], 0)
        self.assertEqual(stats["total_inclusions"], 0)
        self.assertEqual(result.result_set.metadata["sample_id"], "empty-sample")
        self.assertTrue(result.artifacts["cache_dir"])

    def test_cellpose_engine_writes_raw_rows_to_job_sink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "empty-sample"
            raw_path = Path(tmpdir) / "analysis" / "raw.jsonl"
            cache_dir = Path(tmpdir) / "cache" / "cellpose"
            input_dir.mkdir()

            with JsonlRunSink(raw_path) as sink:
                result = create_engine().run(
                    RunJob(
                        id="job-cellpose",
                        engine="cellpose",
                        action="analyze",
                        inputs={"input_dir": input_dir},
                        cache_dir=cache_dir,
                        sink=sink,
                        metadata={"file_id": "image-dir-1", "item_id": "sample-1"},
                    )
                )

            self.assertEqual(result.status, "complete")
            self.assertEqual(result.metadata["sample_id"], "empty-sample")
            self.assertEqual(result.metadata["row_count"], 0)
            self.assertTrue(raw_path.is_file())
            self.assertEqual(raw_path.read_text(encoding="utf-8"), "")
            self.assertEqual(Path(result.metadata["cache_dir"]), cache_dir)


if __name__ == "__main__":
    unittest.main()
