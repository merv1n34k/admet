"""Synthetic recorded-run acceptance tests; no device connections."""

import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from admet.core.protocol_store import write_json
from admet.core.service import Admet
from admet.workflows.calculations import calculate_run, calculation_readiness, saved_results
from admet.workflows.json_protocol import normalize, resolve, template_documents


def archive(root, name, *, flow=None, slope=2, multiplier=1.2, run_id=None, parameters=None):
    source = template_documents()[name]
    source["parameter_values"] = parameters or {}
    document = resolve(source)
    directory = Path(root) / "records" / "protocols" / (run_id or name)
    directory.mkdir(parents=True)
    with patch.object(Admet, "engine_action", side_effect=AssertionError("hardware")):
        plan = Admet().plan_protocol("run_json_protocol", {"protocol": source})
    rows, events = [], []
    clock = 2.0
    config = next(c for c in document["calculations"] if c["type"] == name)
    channel = config["channel"]
    flows = {}
    for sample in config["samples"]:
        step = document["steps"][sample["step"] - 1]
        duration = step["trigger_params"]["duration_s"]
        events.extend([{"step_index": sample["step"] - 1, "step_name": step["name"], "state": "running",
                        "outcome": outcome, "monotonic": 100 + t}
                       for outcome, t in (("running", clock), ("completed", clock + duration))])
        target_pressure = step["pressure_setpoints"].get(str(channel))
        q = ((flow if flow is not None else step["sensor_setpoints"][str(channel)])
             if target_pressure is None else (target_pressure - 5) / slope)
        flows[sample["step"]] = q
        for tick in range(int((duration + 2) * 10) + 1):
            t = clock - 1 + tick / 10
            noise = 0.002 * ((tick % 5) - 2)
            rows.append((t, q * (1 + noise), (5 + slope * q) * (1 + noise / 10)))
        clock += duration + 4
    csv_path = directory / "fluidics.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["elapsed_s", f"flow_{channel}_ul_min", f"pressure_{channel}_mbar"])
        writer.writerows(rows)
    (directory / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    write_json(directory / "protocol.json", source)
    write_json(directory / "summary.json", {**plan, "state": "completed", "run_id": run_id or name,
               "rig_fingerprint": {"simulated": True,
                                   "channel_mapping": [{"sensor_index": ch, "sensor_device_sn": 41 + ch,
                                                        "sensor_type": "Flow_L" if ch == 0 else "Flow_M",
                                                        "controller_sn": 1001 + ch} for ch in range(3)],
                                   "correction_settings": {f"{prefix}_{key}": value
                                                           for prefix in ("oil_l", "cells_m", "beads_m")
                                                           for key, value in (("scale", 2.25), ("calibration", "IPA"),
                                                                              ("offset", 0), ("quadratic", 0))}},
               "artifacts": {"fluidics_csv": str(csv_path), "recording_closed": True,
                             "polling_origin_monotonic": 100}})
    values = {key: None for key in source.get("measurements", {})}
    if name == "gravimetry":
        values["density_g_ml"] = 1.6
        for sample in config["samples"]:
            duration = document["steps"][sample["step"] - 1]["trigger_params"]["duration_s"]
            values[sample["before"]] = 1000
            values[sample["after"]] = 1000 + flows[sample["step"]] * duration / 60 * multiplier * 1.6
    elif name == "dead_volume":
        values["flow_multiplier"] = multiplier
        for sample in config["samples"]:
            values[sample["injection"]] = 10
            values[sample["arrival"]] = 30
            values[sample["timing_uncertainty"]] = 0.2
    elif name == "viscosity":
        values["flow_multiplier"] = multiplier
    write_json(directory / "measurements.json", {"fields": source.get("measurements", {}),
               "values": values, "revision": 1})
    return directory


class MetrologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_gravimetry_recovers_multiplier_from_noisy_recorded_flow(self):
        directory = archive(self.tmp.name, "gravimetry")
        with patch.object(Admet, "engine_action", side_effect=AssertionError("hardware")):
            result = calculate_run(directory, "gravimetry")["result"]
        self.assertEqual(result["status"], "usable")
        self.assertAlmostEqual(result["multiplier"], 1.2, places=3)
        self.assertEqual(len(result["samples"]), 9)
        self.assertGreaterEqual(result["flow_fit"]["r_squared"], 0.95)
        self.assertEqual([row["target_ul_min"] for row in result["targets"]], [15, 41, 67])
        self.assertTrue(all(row["true_flow"]["repeats"] == 3 for row in result["targets"]))
        self.assertIsNone(result["uncertainty"])
        self.assertTrue(all(row["true_volume_ul"] > 0 for row in result["samples"]))

    def test_gravimetry_missing_and_invalid_collection_never_produces_a_correction(self):
        directory = archive(self.tmp.name, "gravimetry")
        path = directory / "measurements.json"
        payload = json.loads(path.read_text())
        payload["values"]["mass_after_1"] = None
        write_json(path, payload)
        self.assertIn("Missing measurement", calculation_readiness(directory, "gravimetry"))
        with self.assertRaises(ValueError):
            calculate_run(directory, "gravimetry")
        payload["values"]["mass_after_1"] = 900
        write_json(path, payload)
        result = calculate_run(directory, "gravimetry")["result"]
        self.assertEqual(result["status"], "inconclusive")
        self.assertIsNone(result["multiplier"])
        payload["values"]["mass_after_1"] = 1019.2
        write_json(path, payload)
        self.assertTrue(saved_results(directory)[0]["outdated"])

    def test_gravimetry_rejects_wrong_units_and_binding(self):
        document = template_documents()["gravimetry"]
        document["measurements"]["density_g_ml"]["unit"] = "s"
        with self.assertRaisesRegex(ValueError, "unit"):
            normalize(document)

    def test_gravimetry_channel_selection_budget_and_nonconstant_calibration(self):
        for channel, working in ((0, 250), (1, 67), (2, 67)):
            source = template_documents()["gravimetry"]
            source["parameter_values"].update(channel=channel, working_flow=working)
            resolved = resolve(source)
            self.assertEqual(resolved["calculations"][0]["channel"], channel)
            self.assertTrue(all(set(s["sensor_setpoints"]) == {str(channel)} for s in resolved["steps"]))
            self.assertAlmostEqual(sum(s["sensor_setpoints"][str(channel)] * s["trigger_params"]["duration_s"] / 60
                                       for s in resolved["steps"]), 900)
            self.assertEqual([s["sensor_setpoints"][str(channel)] for s in resolved["steps"][:3]],
                             [15, (15 + working) / 2, working])
        directory = archive(self.tmp.name, "gravimetry")
        path = directory / "measurements.json"
        data = json.loads(path.read_text())
        doc = resolve(json.loads((directory / "protocol.json").read_text()))
        for sample in doc["calculations"][0]["samples"]:
            step = doc["steps"][sample["step"] - 1]
            q = step["sensor_setpoints"]["1"]
            data["values"][sample["after"]] = 1000 + (q * 1.1 + 3) * step["trigger_params"]["duration_s"] / 60 * 1.6
        write_json(path, data)
        result = calculate_run(directory, "gravimetry")["result"]
        self.assertEqual(result["status"], "usable")
        self.assertIsNone(result["multiplier"])
        self.assertAlmostEqual(result["flow_fit"]["slope"], 1.1, places=3)
        self.assertAlmostEqual(result["flow_fit"]["intercept"], 3, places=3)
        self.assertEqual(len(result["calibration_curve"]), 3)
        data["values"]["mass_after_1"] *= 1.3
        write_json(path, data)
        self.assertEqual(calculate_run(directory, "gravimetry")["result"]["status"], "inconclusive")

    def test_dead_volume_integrates_marker_interval_and_reports_uncertainty(self):
        directory = archive(self.tmp.name, "dead_volume")
        result = calculate_run(directory, "dead_volume")["result"]
        self.assertEqual(result["status"], "usable")
        self.assertAlmostEqual(result["volume_ul"], 12, places=3)
        self.assertGreater(result["samples"][0]["timing_uncertainty_ul"], 0)
        self.assertIsNone(result["uncertainty"])
        path = directory / "measurements.json"
        payload = json.loads(path.read_text())
        for arrival in (5, 100):
            payload["values"]["arrival_1"] = arrival
            write_json(path, payload)
            result = calculate_run(directory, "dead_volume")["result"]
            self.assertEqual(result["status"], "inconclusive")
            self.assertIsNone(result["volume_ul"])
        payload["values"]["arrival_1"] = None
        write_json(path, payload)
        self.assertIn("Missing measurement", calculation_readiness(directory, "dead_volume"))

    def test_dead_volume_explicit_calibration_reference_and_staleness(self):
        calibration = archive(self.tmp.name, "gravimetry")
        reference = calculate_run(calibration, "gravimetry")
        directory = archive(self.tmp.name, "dead_volume")
        path = directory / "measurements.json"
        payload = json.loads(path.read_text())
        payload["values"]["flow_multiplier"] = None
        write_json(path, payload)
        with self.assertRaisesRegex(ValueError, "Missing measurement"):
            calculate_run(directory, "dead_volume")
        refs = {"calibration": reference["path"]}
        result = calculate_run(directory, "dead_volume", references=refs)
        self.assertAlmostEqual(result["result"]["volume_ul"], 12, places=2)
        summary_path = directory / "summary.json"
        summary = json.loads(summary_path.read_text())
        summary["rig_fingerprint"]["correction_settings"]["cells_m_scale"] = 3
        write_json(summary_path, summary)
        with self.assertRaisesRegex(ValueError, "matching recorded"):
            calculate_run(directory, "dead_volume", references=refs)
        calibration.joinpath("measurements.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "outdated"):
            calculate_run(directory, "dead_volume", references=refs)

    def test_trace_ignores_missing_values_outside_exact_window(self):
        from admet.workflows.calculation_inputs import trace, volume_ul

        path = Path(self.tmp.name) / "trace.csv"
        path.write_text("elapsed_s,flow_1_ul_min\n0,\n1,30\n2,30\n3,\n")
        self.assertEqual(volume_ul(trace({"csv": path}, 1, 1, 2)), 0.5)
        with self.assertRaisesRegex(ValueError, "Missing samples"):
            trace({"csv": path}, 1, 0.5, 2)

    def test_viscosity_recovers_resistance_ratio_and_absolute_reference(self):
        reference_dir = archive(self.tmp.name, "viscosity", run_id="reference", slope=2)
        path = reference_dir / "measurements.json"
        payload = json.loads(path.read_text())
        payload["values"]["known_viscosity"] = 2
        write_json(path, payload)
        reference = calculate_run(reference_dir, "viscosity")
        self.assertEqual(reference["result"]["status"], "usable")
        self.assertAlmostEqual(reference["result"]["resistance_mbar_min_ul"], 2 / 1.2, places=3)
        self.assertIsNone(reference["result"]["viscosity_mpa_s"])
        sample_dir = archive(self.tmp.name, "viscosity", run_id="sample", slope=5)
        refs = {"reference": reference["path"]}
        result = calculate_run(sample_dir, "viscosity", references=refs)["result"]
        self.assertEqual(result["status"], "usable")
        self.assertAlmostEqual(result["relative_viscosity"], 2.5, places=3)
        self.assertAlmostEqual(result["viscosity_mpa_s"], 5, places=3)
        self.assertTrue(all(p["r_squared"] >= 0.95 for p in result["passes"]))
        self.assertIsNone(result["uncertainty"])
        other = archive(self.tmp.name, "viscosity", run_id="other", parameters={"path_id": "different"})
        with self.assertRaisesRegex(ValueError, "same identified path"):
            calculate_run(other, "viscosity", references=refs)

    def test_viscosity_rejects_unstable_incomplete_and_uncalibrated_data(self):
        directory = archive(self.tmp.name, "viscosity")
        path = directory / "measurements.json"
        payload = json.loads(path.read_text())
        payload["values"]["flow_multiplier"] = None
        write_json(path, payload)
        self.assertIn("Missing measurement", calculation_readiness(directory, "viscosity"))
        payload["values"]["flow_multiplier"] = 1
        write_json(path, payload)
        csv_path = directory / "fluidics.csv"
        with csv_path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        for i, row in enumerate(rows):
            row["flow_1_ul_min"] = float(row["flow_1_ul_min"]) * (1.4 if i % 2 else 0.6)
        with csv_path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        result = calculate_run(directory, "viscosity")["result"]
        self.assertEqual(result["status"], "inconclusive")
        self.assertIsNone(result["resistance_mbar_min_ul"])
        self.assertIsNone(result["relative_viscosity"])
        self.assertIn("5%", " ".join(result["issues"]))

    def test_viscosity_schema_requires_reverse_pass_and_averaging(self):
        source = template_documents()["viscosity"]
        source["parameter_values"] = {"average_s": 1}
        with self.assertRaisesRegex(ValueError, "5 seconds"):
            normalize(source)
        source["parameter_values"] = {}
        source["steps"][5]["sensor_setpoints"]["{channel}"] = "working_flow"
        with self.assertRaisesRegex(ValueError, "reverse pass"):
            normalize(source)

    def test_changed_execution_and_calculation_bindings_are_refused(self):
        for name in ("gravimetry", "dead_volume", "viscosity"):
            directory = archive(self.tmp.name, name)
            path = directory / "protocol.json"
            document = json.loads(path.read_text())
            document["steps"][0]["on_complete"] = "hold"
            write_json(path, document)
            with self.assertRaises(ValueError):
                calculate_run(directory, name)
        directory = archive(self.tmp.name, "gravimetry", run_id="changed-binding")
        path = directory / "protocol.json"
        document = json.loads(path.read_text())
        sample = document["calculations"][0]["samples"][0]
        sample["before"], sample["after"] = sample["after"], sample["before"]
        write_json(path, document)
        with self.assertRaisesRegex(ValueError, "declarations disagree"):
            calculate_run(directory, "gravimetry")

    def test_viscosity_reuses_flow_curve_and_checks_coverage(self):
        calibration = archive(self.tmp.name, "gravimetry")
        reference = calculate_run(calibration, "gravimetry")
        directory = archive(self.tmp.name, "viscosity")
        refs = {"calibration": reference["path"]}
        result = calculate_run(directory, "viscosity", references=refs)["result"]
        self.assertEqual(result["status"], "usable", result["issues"])
        self.assertIsNone(result["flow_multiplier"])
        self.assertTrue(all(abs(p["flow_multiplier"] - 1.2) < 0.001 for p in result["samples"]))
        outside = archive(self.tmp.name, "viscosity", run_id="outside", parameters={"working_flow": 100})
        result = calculate_run(outside, "viscosity", references=refs)["result"]
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("calibrated range", " ".join(result["issues"]))

    def test_viscosity_channel_selection_and_archived_pressure_mode(self):
        for channel, working in ((0, 250), (1, 67), (2, 67)):
            source = template_documents()["viscosity"]
            source["parameter_values"].update(channel=channel, working_flow=working)
            resolved = resolve(source)
            self.assertTrue(all(set(s["sensor_setpoints"]) == {str(channel)} for s in resolved["steps"]))
            self.assertEqual(resolved["steps"][2]["sensor_setpoints"][str(channel)], working)
        source = template_documents()["viscosity"]
        for step in source["steps"]:
            step["pressure_setpoints"] = step.pop("sensor_setpoints")
        self.assertEqual(len(normalize(source)["calculations"][0]["samples"]), 6)

    def test_reference_identity_and_results_survive_project_move(self):
        import shutil
        from admet.workflows.calculations import result_text

        calibration = archive(self.tmp.name, "gravimetry")
        ref = calculate_run(calibration, "gravimetry")
        directory = archive(self.tmp.name, "dead_volume")
        result = calculate_run(directory, "dead_volume", references={"calibration": ref["path"]})
        self.assertIn("TIMING UNCERTAINTY", result_text(result))
        copied = Path(self.tmp.name) / "copied"
        shutil.copytree(Path(self.tmp.name) / "records", copied / "records")
        self.assertFalse(saved_results(copied / "records" / "protocols" / "dead_volume")[0]["outdated"])
        path = directory / "summary.json"
        summary = json.loads(path.read_text())
        summary["rig_fingerprint"]["channel_mapping"][1]["sensor_device_sn"] = 99
        write_json(path, summary)
        with self.assertRaisesRegex(ValueError, "sensor identity"):
            calculate_run(directory, "dead_volume", references={"calibration": ref["path"]})

    def test_new_schema_expanded_repeats_and_invalid_sample_units(self):
        from copy import deepcopy

        for name in ("gravimetry", "dead_volume", "viscosity"):
            source = template_documents()[name]
            sample = source["calculations"][0]["samples"][0]
            sample["step"] = 999
            with self.assertRaisesRegex(ValueError, "expanded step"):
                normalize(source)
        source = template_documents()["gravimetry"]
        source["calculations"][0]["samples"] = source["calculations"][0]["samples"][:3]
        for sample in source["calculations"][0]["samples"]:
            sample.pop("pass")
        source["measurements"] = {k: v for k, v in source["measurements"].items() if v.get("step", 0) <= 3}
        first = deepcopy(source["steps"][0])
        first["repeat"] = 3
        source["steps"] = [first, source["steps"][-1]]
        self.assertEqual(len(normalize(source)["calculations"][0]["samples"]), 3)
        source["measurements"]["mass_after_3"]["unit"] = "s"
        with self.assertRaisesRegex(ValueError, "unit"):
            normalize(source)

    def test_viscosity_flags_hysteresis_and_nonlinearity_without_dropping_points(self):
        for case in ("hysteresis", "nonlinear"):
            directory = archive(self.tmp.name, "viscosity", run_id=case)
            events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
            windows = [(events[i]["monotonic"] - 101, events[i + 1]["monotonic"] - 99)
                       for i in range(0, len(events), 2)]
            path = directory / "fluidics.csv"
            with path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            for row in rows:
                t = float(row["elapsed_s"])
                affected = (3, 4, 5) if case == "hysteresis" else (1, 4)
                if any(windows[i][0] <= t <= windows[i][1] for i in affected):
                    row["flow_1_ul_min"] = float(row["flow_1_ul_min"]) * (2 if case == "hysteresis" else 0.1)
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            result = calculate_run(directory, "viscosity")["result"]
            self.assertEqual(result["status"], "inconclusive")
            self.assertEqual(len(result["samples"]), 6)
            self.assertIsNone(result["resistance_mbar_min_ul"])
            self.assertIn("10%" if case == "hysteresis" else "R²", " ".join(result["issues"]))
