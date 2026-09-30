import csv
import json
from pathlib import Path
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

from admet.workflows.oil_density import (
    GRAVITY_CONVERSION_MBAR_PER_CM_PER_G_ML,
    OilDensityMeasurement,
    analyze_oil_density,
    relative_density,
    density_protocol,
    analyze_density_run,
)
from admet.workflows.json_protocol import normalize, resolve, template_documents
from admet.workflows.calculation_schema import declarations


def recorded_density(directory, *, densities=(1.2, 1.2), missing=False, unsettled=False, scouted=False, curvature=0):
    document = normalize(density_protocol("dsurf", scouted=scouted))
    resolved = resolve(document)
    rows, events = [], []
    origin = 100.0
    for index, point in enumerate(resolved["analysis"]["points"]):
        start = index * 50 + 30
        step = resolved["steps"][point["step"] - 1]
        duration = step["trigger_params"]["duration_s"]
        flow = step["sensor_setpoints"]["1"]
        density = densities[max(0, point["pass"] - 1)]
        pressure = 7 + density * 0.980665 * point["height_cm"] + (flow - 2) * 0.4 + curvature * flow**2
        events.extend([
            {"monotonic": origin + start - 20, "step_index": point["step"] - 1,
             "step_name": step["name"], "state": "running", "outcome": "running",
             "confirmation_message": "height gate"},
            {"monotonic": origin + start, "step_index": point["step"] - 1,
             "step_name": step["name"], "state": "running", "outcome": "running"},
            {"monotonic": origin + start + duration, "step_index": point["step"] - 1,
             "step_name": step["name"], "state": "running", "outcome": "completed"},
        ])
        rows.append((start - 10, 9999, 9999))  # Gate data must not enter the fit.
        for tick in range(int(duration * 5)):
            elapsed = tick / 5
            value = pressure if elapsed >= 10 else pressure + 100
            if unsettled and index == 3 and elapsed >= 15:
                value += 5
            rows.append((start + elapsed, "" if missing and index == 3 else value, flow))
    csv_path = Path(directory) / "fluidics.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["elapsed_s", "pressure_1_mbar", "flow_1_ul_min"])
        writer.writerows(rows)
    events_path = Path(directory) / "events.jsonl"
    events_path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return document, csv_path, events_path, origin


class RecordedDensityTests(unittest.TestCase):
    def test_templates_plan_without_actuation_and_have_correct_budget(self):
        from admet.core.service import Admet

        admet = Admet()
        for oil in ("dSurf", "EvaGreen", "custom mix"):
            document = template_documents()["density"]
            document["parameter_values"]["oil_name"] = oil
            self.assertEqual(declarations(resolve(document))[0]["oil_id"], oil)
            self.assertEqual(normalize(document), document)
            self.assertNotIn("temperature", json.dumps(document))
            self.assertEqual(document["pressure_limits_mbar"], {})
            with patch.object(admet, "engine_action", side_effect=AssertionError("actuation")):
                plan = admet.plan_protocol(operation_id="run_json_protocol", settings={"protocol": document})
            self.assertEqual(plan["expected_duration_s"], 540)
            self.assertEqual(len(plan["required_confirmations"]), 6)
            self.assertAlmostEqual(sum(s["sensor_setpoints"].get("1", 0)
                                       * s["trigger_params"]["duration_s"] / 60
                                       for s in resolve(document)["steps"]), 270)
            self.assertEqual(len(declarations(document)[0]["points"]), 18)
            self.assertTrue(all(s["on_complete"] == "zero" for s in document["steps"]))

    def test_scouted_density_parameters_gates_and_curved_pressure_response(self):
        source = normalize(density_protocol("dsurf", scouted=True))
        source["parameter_values"].update(oil_base_flow=10, height_low_cm=6, settling_s=12)
        resolved = resolve(source)
        self.assertEqual(resolved["steps"][1]["sensor_setpoints"], {"1": 10})
        self.assertEqual(resolved["steps"][1]["trigger_params"]["duration_s"], 32)
        self.assertEqual(resolved["analysis"]["points"][0]["height_cm"], 6)
        self.assertIn("6 cm ABOVE", resolved["steps"][1]["confirm_message"])
        for step in resolved["steps"]:
            self.assertEqual(set(step["sensor_setpoints"]), {"1"})
            if step.get("confirm_message"):
                self.assertIn("open to atmosphere", step["confirm_message"])
        with tempfile.TemporaryDirectory() as tmp:
            result = analyze_density_run(*recorded_density(
                tmp, scouted=True, densities=(1.6, 1.6), curvature=0.008), completed=True)
        self.assertEqual(result["status"], "consistent", result["issues"])
        self.assertAlmostEqual(result["density_g_ml"], 1.6)
        self.assertEqual(len(result["points"]), 18)
        source["steps"][2]["sensor_setpoints"]["1"] = 25
        with self.assertRaisesRegex(ValueError, "identical flow targets"):
            normalize(source)

    def test_invalid_analysis_is_refused(self):
        for mutate in (
            lambda d: d["analysis"].update(type="unknown"),
            lambda d: d["analysis"].update(temperature=20),
            lambda d: d["analysis"]["points"][0].update(step=1),
            lambda d: d["analysis"]["points"][0].update(height_cm=float("nan")),
            lambda d: d["analysis"]["points"][0].update(settle_s=19),
            lambda d: d["steps"][1].update(repeat=2),
            lambda d: d["steps"][1].update(on_complete="hold"),
            lambda d: d["steps"][1].update(sensor_setpoints={"0": 5}),
            lambda d: d["steps"][1].update(timeout_s=10),
            lambda d: d["analysis"]["points"][-1].update(height_cm=15),
        ):
            document = density_protocol("dsurf")
            mutate(document)
            with self.assertRaises(ValueError):
                normalize(document)

    def test_gate_uses_same_height_as_calculation_and_does_not_mutate_source(self):
        document = density_protocol("dsurf")
        before = deepcopy(document)
        normalized = normalize(document)
        self.assertEqual(document, before)
        for point in normalized["analysis"]["points"]:
            if point["height_cm"] == 5:
                point["height_cm"] = 6
        normalized = normalize(normalized)
        first_measurement = normalized["analysis"]["points"][3]["step"] - 1
        self.assertIn("6 cm ABOVE", normalized["steps"][first_measurement]["confirm_message"])

    def test_density_never_targets_other_channels(self):
        document = normalize(density_protocol("dsurf"))
        for step in document["steps"]:
            self.assertLessEqual(set(step["sensor_setpoints"]) | set(step["pressure_setpoints"]), {"1"})
        for step_index, step in enumerate(document["steps"]):
            if step.get("confirm_message"):
                self.assertEqual(document["steps"][step_index - 1]["sensor_setpoints"], {"1": 0})
        self.assertEqual(document["steps"][-1]["sensor_setpoints"], {"1": 0})
        self.assertTrue(all(not s["pressure_setpoints"] for s in document["steps"]))

    def test_archived_pressure_zero_protocols_remain_analyzable(self):
        document = density_protocol("dsurf")
        for step in document["steps"]:
            if step["sensor_setpoints"] == {"1": 0}:
                step.pop("sensor_setpoints")
                step["pressure_setpoints"] = {"1": 0}
        self.assertEqual(len(normalize(document)["analysis"]["points"]), 21)

    def test_measured_windows_remove_settling_gates_resistance_and_constant_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            result = analyze_density_run(*recorded_density(directory), completed=True)
        self.assertEqual(result["status"], "consistent", result["issues"])
        self.assertAlmostEqual(result["density_g_ml"], 1.2)
        self.assertAlmostEqual(result["repeat_difference_percent"], 0)
        self.assertIsNone(result["ci95_g_ml"])
        self.assertEqual(len(result["points"]), 21)
        self.assertGreater(result["points"][0]["samples"], 40)

    def test_missing_and_unsettled_data_are_not_valid_densities(self):
        for options, issue in (({"missing": True}, "missing pressure"),
                               ({"unsettled": True}, "unsettled pressure"),
                               ({"densities": (1.0, 1.4)}, "disagree")):
            with tempfile.TemporaryDirectory() as directory:
                result = analyze_density_run(*recorded_density(directory, **options), completed=True)
            self.assertEqual(result["status"], "inconclusive")
            self.assertIsNone(result["density_g_ml"])
            self.assertIn(issue, " ".join(result["issues"]))
            if options.get("missing"):
                self.assertIsNone(result["points"][3]["pressure_mean_mbar"])

    def test_failed_paused_skipped_or_missing_clock_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            args = recorded_density(directory)
            for event in ({"state": "paused"}, {"outcome": "skipped"}):
                with args[2].open("a") as handle:
                    handle.write(json.dumps(event) + "\n")
                result = analyze_density_run(*args, completed=True)
                self.assertIsNone(result["density_g_ml"])
            self.assertIsNone(analyze_density_run(*args, completed=False)["density_g_ml"])
            self.assertIsNone(analyze_density_run(*args[:3], None, completed=True)["density_g_ml"])


def synthetic_oil(oil_id: str, role: str, density: float):
    slope = density * GRAVITY_CONVERSION_MBAR_PER_CM_PER_G_ML
    rows = []
    for height in (-20.0, 0.0, 20.0):
        zero_pressure = 5.0 + slope * height
        for offset, direction in ((-2.0, "up"), (-1.0, "up"), (1.0, "down"), (2.0, "down")):
            pressure = zero_pressure + offset
            rows.append(
                OilDensityMeasurement(
                    oil_id=oil_id,
                    role=role,
                    height_cm=height,
                    pressure_mbar=pressure,
                    flow_ul_min=2.0 * offset,
                    direction=direction,
                )
            )
    return rows


class OilDensityAnalysisTests(unittest.TestCase):
    def test_recovers_density_from_two_stage_regression(self):
        result = analyze_oil_density(synthetic_oil("oil-a", "candidate", 1.2))[0]

        self.assertAlmostEqual(result.density_g_ml, 1.2)
        self.assertAlmostEqual(result.intercept_mbar, 5.0)
        self.assertAlmostEqual(result.r_squared, 1.0)
        self.assertEqual(len(result.balances), 3)
        self.assertEqual(result.warnings, ())

    def test_compares_candidate_with_reference(self):
        results = analyze_oil_density(
            synthetic_oil("candidate", "candidate", 1.2)
            + synthetic_oil("reference", "reference", 0.8)
        )
        by_role = {result.role: result for result in results}

        ratio, uncertainty = relative_density(by_role["candidate"], by_role["reference"])

        self.assertAlmostEqual(ratio, 1.5)
        self.assertAlmostEqual(uncertainty or 0.0, 0.0)

    def test_excludes_unfittable_height_and_requires_three_valid_heights(self):
        rows = synthetic_oil("oil-a", "candidate", 1.0)
        rows = [row for row in rows if row.height_cm != 20.0]
        rows.extend(
            [
                OilDensityMeasurement("oil-a", "candidate", 20.0, 4.0, 1.0),
                OilDensityMeasurement("oil-a", "candidate", 20.0, 4.0, 2.0),
            ]
        )

        self.assertEqual(analyze_oil_density(rows), ())

    def test_rejects_missing_oil_identity(self):
        with self.assertRaisesRegex(ValueError, "oil ID"):
            analyze_oil_density(
                [OilDensityMeasurement("", "candidate", 0.0, 1.0, 2.0)]
            )


if __name__ == "__main__":
    unittest.main()
