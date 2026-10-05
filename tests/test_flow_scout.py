from copy import deepcopy
import csv
import json
import math
from pathlib import Path
import random
import tempfile
import unittest

from admet.workflows.calculation_schema import declarations
from admet.workflows.flow_scout import analyze_scout
from admet.workflows.json_protocol import normalize, resolve, template_documents


class ImperfectOil:
    """Test-only plant: corrected flow, hydrostatic head, settling and correlated noise."""

    def __init__(self, *, density=1.6000, height=5, scale=2.25, seed=16000, drift=0):
        self.density, self.height, self.scale = density, height, scale
        self.rng = random.Random(seed)
        self.target = 0
        self.started = 0
        self.previous = 0
        self.noise = 0
        self.drift = drift

    def set_flow(self, target, now):
        self.previous = self.target
        self.target = target
        self.started = now

    def sample(self, now):
        elapsed = max(0, now - self.started)
        self.noise = 0.65 * self.noise + self.rng.gauss(0, 0.06)
        physical = self.target + (self.previous - self.target) * math.exp(-elapsed / 3)
        raw_ipa = physical / self.scale + self.noise
        if 0 < self.target < 10:
            raw_ipa += 0.65 * math.sin(elapsed * 1.7)
        measured_flow = self.scale * raw_ipa
        pressure = (3 + self.density * 0.980665 * self.height + physical * 0.4
                    + 1.5 * math.exp(-elapsed / 4) + self.rng.gauss(0, 0.12)
                    + 0.06 * math.sin(elapsed / 4) + self.drift * elapsed)
        return round(pressure / 0.02) * 0.02, measured_flow


def scout_context(directory, *, drift=0, missing=False, short=False, curved=False, return_offset=0):
    source = template_documents()["flow_stability_scout"]
    if short:
        source["parameter_values"]["point_duration_s"] = 15
    document = resolve(source)
    rows, events = [], []
    clock, origin = 20.0, 100.0
    plant = ImperfectOil(drift=drift)
    for point in declarations(document)[0]["points"]:
        index = point["step"] - 1
        step = document["steps"][index]
        target = step["sensor_setpoints"]["1"]
        duration = step["trigger_params"]["duration_s"]
        plant.set_flow(target, clock)
        events += [{"monotonic": origin + clock, "step_index": index, "state": "running", "outcome": "running"},
                   {"monotonic": origin + clock + duration, "step_index": index,
                    "state": "running", "outcome": "completed"}]
        for tick in range(int(duration * 10)):
            now = clock + tick / 10
            p, q = plant.sample(now)
            if curved:
                q = target + 0.15 * math.sin(now * 3)
                p = (8 + 0.55 * target + 0.008 * target**2 + 0.1 * math.sin(now)
                     + return_offset * (point["pass"] - 1))
            rows.append((now, "" if missing else p, q))
        clock += duration + 2
    directory = Path(directory)
    csv_path = directory / "fluidics.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["elapsed_s", "pressure_1_mbar", "flow_1_ul_min"])
        writer.writerows(rows)
    (directory / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    return {"directory": directory, "document": source, "csv": csv_path,
            "summary": {"state": "completed", "artifacts": {"polling_origin_monotonic": origin}}}


def density_recovery(directory, *, seed=16000, drift=0):
    from admet.workflows.fluid_density import analyze_density_run
    from tests.test_fluid_density import density_document

    document = density_document("simulation_16000")
    plant = ImperfectOil(seed=seed, drift=drift)
    rows, events = [], []
    origin, clock = 100, 10
    for point in declarations(document)[0]["points"]:
        step = document["steps"][point["step"] - 1]
        duration = step["trigger_params"]["duration_s"]
        plant.height = point["height_cm"]
        plant.set_flow(0, clock - 2)
        plant.set_flow(step["sensor_setpoints"]["1"], clock)
        events.extend([{"monotonic": origin + clock, "step_index": point["step"] - 1,
                        "step_name": step["name"], "state": "running", "outcome": "running"},
                       {"monotonic": origin + clock + duration, "step_index": point["step"] - 1,
                        "step_name": step["name"], "state": "running", "outcome": "completed"}])
        for tick in range(int(duration * 10)):
            now = clock + tick / 10
            pressure, flow = plant.sample(now)
            rows.append((now, pressure, flow))
        clock += duration + 2
    directory = Path(directory)
    csv_path, events_path = directory / "density.csv", directory / "density_events.jsonl"
    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["elapsed_s", "pressure_1_mbar", "flow_1_ul_min"])
        writer.writerows(rows)
    events_path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return analyze_density_run(document, csv_path, events_path, origin, completed=True)


class ScoutTests(unittest.TestCase):
    def test_density_16000_recovery_with_noise_and_drift_rejection(self):
        for seed in range(16000, 16010):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory() as tmp:
                result = density_recovery(tmp, seed=seed)
            self.assertEqual(result["status"], "consistent", result["issues"])
            self.assertLessEqual(abs(result["density_g_ml"] - 1.6000), 0.016)
        with tempfile.TemporaryDirectory() as tmp:
            bad = density_recovery(tmp, drift=0.4)
        self.assertEqual(bad["status"], "inconclusive")
        self.assertIsNone(bad["density_g_ml"])

    def test_template_is_single_height_m1_only_with_parameterized_targets(self):
        source = template_documents()["flow_stability_scout"]
        self.assertEqual(normalize(source), source)
        document = resolve(source)
        self.assertEqual(declarations(document)[0]["height_cm"], 5)
        self.assertEqual([s["sensor_setpoints"]["1"] for s in document["steps"]],
                         [0, 5, 15, 20, 30, 45, 45, 30, 20, 15, 5, 0])
        self.assertEqual(sum(bool(s.get("confirm_message")) for s in document["steps"]), 1)
        for mutation in (lambda d: declarations(d)[0].update(height_cm=-1),
                         lambda d: d["steps"][2].update(sensor_setpoints={"0": 15}),
                         lambda d: declarations(d)[0]["points"].pop(),
                         lambda d: d["parameter_values"].update(point_duration_s=5)):
            invalid = deepcopy(source)
            mutation(invalid)
            with self.assertRaises(ValueError):
                normalize(invalid)

    def test_noisy_data_reject_low_flow_and_recommend_a_stable_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = analyze_scout(scout_context(tmp))
        self.assertEqual(result["status"], "usable")
        self.assertIsNone(result["density_g_ml"])
        self.assertNotIn(5, result["recommendation"]["targets_ul_min"])
        self.assertGreaterEqual(len(result["recommendation"]["targets_ul_min"]), 3)
        self.assertTrue(result["recommendation"]["requires_operator_approval"])
        self.assertGreater(result["recommendation"]["settling_s"], 0)
        self.assertGreaterEqual(result["recommendation"]["averaging_s"], 20)
        self.assertIn("diagnostic only", " ".join(next(f for f in result["fits"] if f["averaging_s"] == 10)["issues"]))
        fit = next(f for f in result["fits"] if f["averaging_s"] == result["recommendation"]["averaging_s"])
        self.assertEqual(len(fit["verification_passes"]), 2)
        self.assertLess(fit["settling_s"] + 2 * fit["averaging_s"], 60)

    def test_successive_window_shift_is_rejected_even_when_each_window_is_stable(self):
        from admet.workflows.flow_scout import _window

        with tempfile.TemporaryDirectory() as tmp:
            context = scout_context(tmp)
            # A slow ramp can pass individual half-window drift/SD tests while
            # successive complete windows disagree. Do not call this settled.
            with context["csv"].open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            for row in rows:
                elapsed = (float(row["elapsed_s"]) - 20) % 62
                row["pressure_1_mbar"] = 10 + 0.4 * float(row["flow_1_ul_min"]) + 0.065 * elapsed
            with context["csv"].open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            result = analyze_scout(context)
        stable = [(tick / 10, 10 + 0.065 * tick / 10, 20) for tick in range(600)]
        self.assertFalse(_window(stable, 10, 20, 20)["issues"])
        self.assertFalse(_window(stable, 30, 20, 20)["issues"])
        self.assertIsNone(result["recommendation"])
        self.assertIn("successive", " ".join(next(f for f in result["fits"] if f["averaging_s"] == 20)["issues"]))

    def test_mild_curvature_and_sub_mbar_return_shift_are_usable_with_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = analyze_scout(scout_context(tmp, curved=True, return_offset=0.6))
        self.assertEqual(result["status"], "usable", result["issues"])
        self.assertEqual(result["thresholds"]["fit_r_squared_min"], 0.95)
        self.assertEqual(result["recommendation"]["averaging_s"], 20)
        self.assertIn("curvature", " ".join(result["warnings"]))
        self.assertIn("intercept difference", " ".join(result["warnings"]))
        self.assertIsNone(result["density_g_ml"])
        with tempfile.TemporaryDirectory() as tmp:
            bad = analyze_scout(scout_context(tmp, curved=True, return_offset=90))
        self.assertEqual(bad["status"], "inconclusive")
        self.assertIsNone(bad["recommendation"])
        self.assertIn("longer averaging alone may not help", " ".join(bad["issues"]))

    def test_drift_missing_and_short_recordings_do_not_become_precise(self):
        for settings in ({"drift": 0.3}, {"missing": True}, {"short": True}):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as tmp:
                result = analyze_scout(scout_context(tmp, **settings))
            self.assertEqual(result["status"], "inconclusive")
            self.assertIsNone(result["recommendation"])
            if settings.get("missing"):
                self.assertIsNone(result["points"][0]["windows"][0]["pressure_mean_mbar"])

    def test_incomplete_clock_and_pause_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = scout_context(tmp)
            context["summary"]["artifacts"]["polling_origin_monotonic"] = None
            self.assertIsNone(analyze_scout(context)["recommendation"])
            context["summary"]["artifacts"]["polling_origin_monotonic"] = 100
            with (Path(tmp) / "events.jsonl").open("a") as handle:
                handle.write(json.dumps({"state": "paused"}) + "\n")
            self.assertIsNone(analyze_scout(context)["recommendation"])
