import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from admet.core.protocol_store import write_json
from admet.workflows.calculations import calculate_run, recorded_runs, saved_results
from tests.test_oil_density import recorded_density


def archived_density(project):
    directory = Path(project) / "records" / "protocols" / "synthetic"
    directory.mkdir(parents=True, exist_ok=True)
    document, csv_path, _events, origin = recorded_density(directory)
    document.pop("analysis")
    write_json(directory / "protocol.json", document)
    write_json(directory / "summary.json", {
        "state": "completed", "completed_at": "2026-09-28T12:00:00+00:00", "plan_id": "synthetic",
        "normalized_settings": {"protocol": document},
        "artifacts": {"fluidics_csv": str(csv_path), "polling_origin_monotonic": origin,
                      "recording_closed": True},
    })
    return directory


class CalculationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = archived_density(self.tmp.name)

    def test_plain_protocol_calculates_and_preserves_sources_and_history(self):
        before = {path: path.read_bytes() for path in self.directory.iterdir() if path.is_file()}
        with patch("admet.core.service.Admet.engine_action", side_effect=AssertionError("hardware")):
            first = calculate_run(self.directory, "oil_density")
            second = calculate_run(self.directory, "oil_density")
            overview = calculate_run(self.directory, "recording_summary")
        self.assertEqual(first["result"]["status"], "consistent", first["result"]["issues"])
        self.assertAlmostEqual(first["result"]["density_g_ml"], 1.2)
        self.assertIsNone(first["result"]["ci95_g_ml"])
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(len(saved_results(self.directory)), 3)
        self.assertEqual(len(recorded_runs(self.tmp.name)), 1)
        self.assertEqual(len(first["inputs"]), 4)
        self.assertEqual(first["calculation_version"], 2)
        self.assertGreater(overview["result"]["statistics"]["flow_1_ul_min"]["samples"], 0)
        for path, contents in before.items():
            self.assertEqual(path.read_bytes(), contents)

    def test_relocated_windows_recording_rebases_inside_project(self):
        path = self.directory / "summary.json"
        summary = json.loads(path.read_text())
        summary["artifacts"]["fluidics_csv"] = "D:\\old\\experiment.admetp\\records\\protocols\\synthetic\\fluidics.csv"
        write_json(path, summary)
        self.assertAlmostEqual(calculate_run(self.directory, "oil_density")["result"]["density_g_ml"], 1.2)

    def test_current_density_metadata_takes_precedence_over_historical_labels(self):
        from admet.workflows.compat import density_analysis

        path = self.directory / "protocol.json"
        document = json.loads(path.read_text())
        document["analysis"] = density_analysis(document)
        for step in document["steps"]:
            step["name"] = "Current step without legacy geometry label"
        write_json(path, document)
        summary_path = self.directory / "summary.json"
        summary = json.loads(summary_path.read_text())
        summary["run_id"] = "run_current"
        write_json(summary_path, summary)
        before = path.read_bytes()
        result = calculate_run(self.directory, "oil_density")
        self.assertAlmostEqual(result["result"]["density_g_ml"], 1.2)
        self.assertEqual(result["run_id"], "run_current")
        self.assertEqual(path.read_bytes(), before)

    def test_historical_density_rejects_mismatched_targets_and_timing(self):
        path = self.directory / "protocol.json"
        original = path.read_text()
        for field, value, message in (("sensor_setpoints", {"1": 999}, "flow target disagree"),
                                      ("trigger_params", {"duration_s": 5}, "20-second")):
            with self.subTest(field=field):
                document = json.loads(original)
                step = next(step for step in document["steps"] if step["name"].startswith("Scout"))
                step[field] = value
                write_json(path, document)
                with self.assertRaisesRegex(ValueError, message):
                    calculate_run(self.directory, "oil_density")

    def test_active_or_unclosed_recording_is_refused(self):
        path = self.directory / "summary.json"
        summary = json.loads(path.read_text())
        summary["state"] = "executing"
        write_json(path, summary)
        self.assertEqual(recorded_runs(self.tmp.name), [])
        with self.assertRaisesRegex(ValueError, "finished"):
            calculate_run(self.directory, "oil_density")
        summary["state"] = "completed"
        summary["artifacts"]["recording_closed"] = False
        write_json(path, summary)
        with self.assertRaisesRegex(ValueError, "closed"):
            calculate_run(self.directory, "oil_density")

    def test_missing_origin_produces_inconclusive_not_invented_alignment(self):
        path = self.directory / "summary.json"
        summary = json.loads(path.read_text())
        summary["artifacts"].pop("polling_origin_monotonic")
        write_json(path, summary)
        result = calculate_run(self.directory, "oil_density")["result"]
        self.assertIsNone(result["density_g_ml"])
        self.assertIn("missing recording clock origin", result["issues"])

    def test_changed_height_label_or_unrecognized_run_is_refused(self):
        path = self.directory / "protocol.json"
        document = json.loads(path.read_text())
        document["steps"][1]["confirm_message"] = "Set outlet 8 cm ABOVE the oil"
        write_json(path, document)
        with self.assertRaisesRegex(ValueError, "height.*disagree"):
            calculate_run(self.directory, "oil_density")
        document["steps"][1]["name"] = "Unknown geometry"
        write_json(path, document)
        with self.assertRaisesRegex(ValueError, "recognized density"):
            calculate_run(self.directory, "oil_density")

    def test_summary_missing_values_remain_null(self):
        (self.directory / "fluidics.csv").write_text("elapsed_s,pressure_1_mbar,flow_1_ul_min\n0,,\n1,nan,\n")
        stats = calculate_run(self.directory, "recording_summary")["result"]["statistics"]
        for entry in stats.values():
            self.assertEqual(entry["missing"], 2)
            self.assertEqual(entry["samples"], 0)
            self.assertIsNone(entry["mean"])
            self.assertIsNone(entry["std"])

    def test_changed_inputs_do_not_save_result(self):
        from admet.workflows.calculations import CALCULATIONS

        original = CALCULATIONS["recording_summary"]["calculate"]

        def modifying(context):
            result = original(context)
            with context["csv"].open("a") as handle:
                handle.write("99999,0,0\n")
            return result

        with patch.dict(CALCULATIONS["recording_summary"], calculate=modifying):
            with self.assertRaisesRegex(ValueError, "changed"):
                calculate_run(self.directory, "recording_summary")
        self.assertEqual(saved_results(self.directory), [])
