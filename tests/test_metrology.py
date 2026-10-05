"""Synthetic recorded-run acceptance tests; no device connections."""

import csv
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from admet.core.protocol_store import write_json
from admet.core.service import Admet
from admet.workflows.calculations import calculate_run, calculation_readiness, saved_results
from admet.workflows.gravimetry import unit_configs
from admet.workflows.json_protocol import normalize, resolve, template_documents


def collection_s(step, channel):
    params = step["trigger_params"]
    if step["trigger_type"] == "volume":
        return params["target_volume_ul"] * 60 / step["sensor_setpoints"][str(channel)]
    return params["duration_s"]


def archive(root, name, *, flow=None, slope=2, multiplier=1.2, run_id=None, parameters=None, tail_s=0.0):
    source = template_documents()[name]
    source["parameter_values"] = {**({"units": "010"} if name == "gravimetry" else {}), **(parameters or {})}
    document = resolve(source)
    directory = Path(root) / "records" / "protocols" / (run_id or name)
    directory.mkdir(parents=True)
    with patch.object(Admet, "engine_action", side_effect=AssertionError("hardware")):
        plan = Admet().plan_protocol("run_json_protocol", {"protocol": source})
    rows, events = [], []
    clock = 2.0
    config = next(c for c in document["calculations"] if c["type"] == name)
    configs = unit_configs(config) if name == "gravimetry" else [config]
    channels = [c["channel"] for c in configs]
    flows = {}
    tail = (clock, {channel: 0.0 for channel in channels})
    for sample in configs[0].get("samples", []):
        step = document["steps"][sample["step"] - 1]
        duration = collection_s(step, channels[0])
        events.extend([{"step_index": sample["step"] - 1, "step_name": step["name"], "state": "running",
                        "outcome": outcome, "monotonic": 100 + t}
                       for outcome, t in (("running", clock), ("completed", clock + duration))])
        q = {}
        for channel in channels:
            target_pressure = step["pressure_setpoints"].get(str(channel))
            q[channel] = ((flow if flow is not None else step["sensor_setpoints"][str(channel)])
                          if target_pressure is None else (target_pressure - 5) / slope)
        flows[sample["step"]] = q
        # Nothing before the start, the setpoint until the stop, then a tail
        # that dies away (an exponential with time constant tail_s).
        end = clock + duration
        times = sorted({clock - 1 + tick / 10 for tick in range(int((duration + 4) * 10))} | {end, end + 1e-3})
        for tick, t in enumerate(times):
            noise = 0.002 * ((tick % 5) - 2)
            row = [t]
            for channel in channels:
                stop, stopped_q = (tail[0], tail[1][channel]) if t < clock else (end, q[channel])
                value = (q[channel] if clock <= t <= end else stopped_q * math.exp(-(t - stop) / tail_s)
                         if tail_s > 0 and t > stop else 0.0)
                row += [value * (1 + noise), (5 + slope * value) * (1 + noise / 10)]
            rows.append(row)
        tail = (end, q)
        clock += duration + 4
    csv_path = directory / "fluidics.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["elapsed_s", *[f"{kind}_{channel}_{unit}" for channel in channels
                                        for kind, unit in (("flow", "ul_min"), ("pressure", "mbar"))]])
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
    fields = document.get("measurements", {})
    values = {key: None for key in fields}
    if name == "gravimetry":
        values["density_g_ml"] = 1.6
        for config in configs:
            for sample in config["samples"]:
                duration = collection_s(document["steps"][sample["step"] - 1], config["channel"])
                values[sample["before"]] = 1000
                values[sample["after"]] = (1000 + flows[sample["step"]][config["channel"]] * (duration + tail_s) / 60
                                           * multiplier * 1.6)
    elif name == "dead_volume":
        values.update(zip(configs[0]["volumes"], (80, 82, 84)))
    write_json(directory / "measurements.json", {"fields": fields, "values": values, "revision": 1})
    return directory


class MetrologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_gravimetry_recovers_multiplier_from_noisy_recorded_flow(self):
        directory = archive(self.tmp.name, "gravimetry")
        with patch.object(Admet, "engine_action", side_effect=AssertionError("hardware")):
            payload = calculate_run(directory, "gravimetry")["result"]
        self.assertEqual((payload["status"], list(payload["units"])), ("usable", ["1"]))
        self.assertIsNone(payload["uncertainty"])
        result = payload["units"]["1"]
        self.assertAlmostEqual(result["multiplier"], 1.2, places=3)
        self.assertEqual(len(result["samples"]), 9)
        self.assertGreaterEqual(result["flow_fit"]["r_squared"], 0.95)
        self.assertEqual([row["target_ul_min"] for row in result["targets"]], [15, 41, 67])
        self.assertTrue(all(row["true_flow"]["repeats"] == 3 for row in result["targets"]))
        self.assertTrue(all(row["true_volume_ul"] > 0 for row in result["samples"]))

    def test_gravimetry_counts_the_flow_that_arrives_after_the_stop(self):
        # 0.68 s is the tail measured on Cells M; the vessel catches it too.
        directory = archive(self.tmp.name, "gravimetry", tail_s=0.68)
        result = calculate_run(directory, "gravimetry")["result"]["units"]["1"]

        self.assertEqual(result["status"], "usable")
        self.assertAlmostEqual(result["multiplier"], 1.2, delta=0.0012)   # step-only window: ~1.209
        for row in result["samples"]:
            self.assertAlmostEqual(row["tail_volume_ul"], row["target_ul_min"] * 0.68 / 60, delta=0.1 * row["target_ul_min"] * 0.68 / 60)
            self.assertGreater(row["settled_elapsed_s"], row["window_elapsed_s"][1])

    def test_gravimetry_flags_a_tail_that_never_settles(self):
        directory = archive(self.tmp.name, "gravimetry", tail_s=5)    # still flowing when the next step starts
        result = calculate_run(directory, "gravimetry")["result"]["units"]["1"]

        self.assertEqual(result["status"], "inconclusive")
        self.assertIsNone(result["multiplier"])
        self.assertIn("did not settle", result["samples"][0]["issues"][0])

    def test_gravimetry_missing_and_invalid_collection_never_produces_a_correction(self):
        directory = archive(self.tmp.name, "gravimetry")
        path = directory / "measurements.json"
        payload = json.loads(path.read_text())
        payload["values"]["mass_after_ch1_1"] = None
        write_json(path, payload)
        self.assertIn("Missing measurement", calculation_readiness(directory, "gravimetry"))
        with self.assertRaises(ValueError):
            calculate_run(directory, "gravimetry")
        payload["values"]["mass_after_ch1_1"] = 900
        write_json(path, payload)
        result = calculate_run(directory, "gravimetry")["result"]["units"]["1"]
        self.assertEqual(result["status"], "inconclusive")
        self.assertIsNone(result["multiplier"])
        payload["values"]["mass_after_ch1_1"] = 1019.2
        write_json(path, payload)
        self.assertTrue(saved_results(directory)[0]["outdated"])

    def test_gravimetry_rejects_wrong_units_and_binding(self):
        document = template_documents()["gravimetry"]
        document["measurements"]["density_g_ml"]["unit"] = "s"
        with self.assertRaisesRegex(ValueError, "unit"):
            normalize(document)

    def test_gravimetry_unit_mask_flows_and_nonconstant_calibration(self):
        for units, flows in (("001", {0: 250}), ("010", {1: 67}), ("100", {2: 67}),
                             ("110", {1: 67, 2: 67}), ("111", {0: 250, 1: 67, 2: 67})):
            source = template_documents()["gravimetry"]
            source["parameter_values"].update(units=units)
            resolved = resolve(source)
            self.assertEqual(resolved["calculations"][0]["units"], list(flows))
            self.assertTrue(all(set(s["sensor_setpoints"]) == {str(c) for c in flows} for s in resolved["steps"]))
            self.assertEqual({int(c): q for c, q in resolved["steps"][2]["sensor_setpoints"].items()}, flows)
            # Each collection lasts as long as the slowest unit needs for the nominal volume.
            self.assertEqual([s["trigger_type"] for s in resolved["steps"]], ["time"] * 10)
            self.assertAlmostEqual(resolved["steps"][2]["trigger_params"]["duration_s"], 100 * 60 / min(flows.values()))
            self.assertEqual(len(resolved["measurements"]), 1 + 18 * len(flows))
        for bad in ("000", "12", "abc"):
            source = template_documents()["gravimetry"]
            source["parameters"]["units"]["options"].append(bad)
            source["parameter_values"].update(units=bad)
            with self.assertRaisesRegex(ValueError, "mask"):
                resolve(source)
        directory = archive(self.tmp.name, "gravimetry")
        path = directory / "measurements.json"
        data = json.loads(path.read_text())
        doc = resolve(json.loads((directory / "protocol.json").read_text()))
        for sample in unit_configs(doc["calculations"][0])[0]["samples"]:
            step = doc["steps"][sample["step"] - 1]
            q = step["sensor_setpoints"]["1"]
            data["values"][sample["after"]] = 1000 + (q * 1.1 + 3) * collection_s(step, 1) / 60 * 1.6
        write_json(path, data)
        result = calculate_run(directory, "gravimetry")["result"]["units"]["1"]
        self.assertEqual(result["status"], "usable")
        self.assertIsNone(result["multiplier"])
        self.assertAlmostEqual(result["flow_fit"]["slope"], 1.1, places=3)
        self.assertAlmostEqual(result["flow_fit"]["intercept"], 3, places=3)
        self.assertEqual(len(result["calibration_curve"]), 3)
        data["values"]["mass_after_ch1_1"] *= 1.3
        write_json(path, data)
        self.assertEqual(calculate_run(directory, "gravimetry")["result"]["status"], "inconclusive")

    def test_gravimetry_calculates_every_unit_alone_from_its_own_recording(self):
        directory = archive(self.tmp.name, "gravimetry", parameters={"units": "111"})
        path = directory / "measurements.json"
        payload = json.loads(path.read_text())
        payload["values"]["mass_after_ch2_4"] = None
        write_json(path, payload)
        self.assertEqual(calculation_readiness(directory, "gravimetry"), "")
        result = calculate_run(directory, "gravimetry")["result"]
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual({ch: u["status"] for ch, u in result["units"].items()},
                         {"0": "usable", "1": "usable", "2": "inconclusive"})
        self.assertAlmostEqual(result["units"]["0"]["multiplier"], 1.2, places=3)
        self.assertEqual([row["target_ul_min"] for row in result["units"]["0"]["targets"]], [15, 132.5, 250])
        self.assertTrue(any("Beads M" in issue for issue in result["issues"]))

    def test_dead_volume_summarises_the_values_entered_by_hand(self):
        directory = archive(self.tmp.name, "dead_volume")
        result = calculate_run(directory, "dead_volume")["result"]

        self.assertEqual(result["status"], "usable")
        self.assertEqual(result["volumes_ul"], [80, 82, 84])
        self.assertAlmostEqual(result["volume_ul"], 82)
        self.assertAlmostEqual(result["sd_ul"], 2)
        self.assertAlmostEqual(result["ci95_ul"][1] - 82, 4.303 * 2 / 3 ** 0.5, places=3)
        path = directory / "measurements.json"
        payload = json.loads(path.read_text())
        payload["values"]["dead_volume_2"] = None
        write_json(path, payload)
        self.assertIn("Missing measurement", calculation_readiness(directory, "dead_volume"))
        payload["values"]["dead_volume_2"] = 0
        write_json(path, payload)
        with self.assertRaisesRegex(ValueError, "positive"):
            calculate_run(directory, "dead_volume")

    def test_dead_volume_steps_only_ask_and_leave_the_channels_alone(self):
        for channel in (0, 1, 2):
            source = template_documents()["dead_volume"]
            source["parameter_values"].update(channel=channel)
            resolved = resolve(source)
            self.assertEqual(resolved["calculations"][0]["channel"], channel)
            for step in resolved["steps"]:
                self.assertEqual((step["sensor_setpoints"], step["pressure_setpoints"], step["on_complete"]),
                                 ({}, {}, "hold"))
                self.assertIn(f"channel {channel}", step["confirm_message"])

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
        self.assertAlmostEqual(reference["result"]["resistance_mbar_min_ul"], 2, places=3)   # recorded flow as is
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

    def test_viscosity_rejects_unstable_data(self):
        directory = archive(self.tmp.name, "viscosity")
        path = directory / "measurements.json"
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
        for name in ("gravimetry", "viscosity"):
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

    def test_viscosity_channel_selection_and_flow_control_only(self):
        for channel, working in ((0, 250), (1, 67), (2, 67)):
            source = template_documents()["viscosity"]
            source["parameter_values"].update(channel=channel, working_flow=working)
            resolved = resolve(source)
            self.assertTrue(all(set(s["sensor_setpoints"]) == {str(channel)} for s in resolved["steps"]))
            self.assertEqual(resolved["steps"][2]["sensor_setpoints"][str(channel)], working)
        source = template_documents()["viscosity"]
        for step in source["steps"]:
            step["pressure_setpoints"] = step.pop("sensor_setpoints")
        with self.assertRaisesRegex(ValueError, "flow control"):        # flow-controlled only
            normalize(source)

    def test_results_survive_project_move(self):
        import shutil
        from admet.workflows.calculations import result_text

        directory = archive(self.tmp.name, "gravimetry")
        result = calculate_run(directory, "gravimetry")
        self.assertIn("MULTIPLIER", result_text(result))
        copied = Path(self.tmp.name) / "copied"
        shutil.copytree(Path(self.tmp.name) / "records", copied / "records")
        self.assertFalse(saved_results(copied / "records" / "protocols" / "gravimetry")[0]["outdated"])

    def test_sample_steps_passes_and_units_are_checked(self):
        from copy import deepcopy

        for name in ("gravimetry", "viscosity"):
            source = template_documents()[name]
            sample = source["calculations"][0]["samples"][0]
            sample["step"] = 999
            with self.assertRaisesRegex(ValueError, "expanded step"):
                normalize(source)
        source = template_documents()["gravimetry"]
        single_rate = deepcopy(source)
        for sample in single_rate["calculations"][0]["samples"]:
            sample.pop("pass")
        with self.assertRaisesRegex(ValueError, "pass"):              # only the three-pass series
            normalize(single_rate)
        source["measurements"]["mass_after_{unit}_3"]["unit"] = "s"
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
