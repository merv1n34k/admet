from copy import deepcopy
import tempfile
import json
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from admet.core.service import Admet
from admet.workflows.json_protocol import normalize, template_documents, validate_channels


DOCUMENT = {
    "name": "short_run",
    "steps": [{
        "sensor_setpoints": {"0": 10},
        "trigger_type": "time", "trigger_params": {"duration_s": 0.1},
        "confirm_message": "Start short run?",
    }],
}


class JsonProtocolTests(unittest.TestCase):
    def test_metrology_templates_execute_on_simulator_and_save_separate_results(self):
        from admet.ui.backend import DesktopBackend
        from admet.workflows.calculations import calculate_run

        with tempfile.TemporaryDirectory() as tmp:
            backend = DesktopBackend(simulated=True)
            backend.create_project(Path(tmp) / "metrology.admetp")
            with patch.object(backend.engine.sdk, "detect_instruments", side_effect=AssertionError("live discovery")):
                try:
                    backend.call("connect_fluidics")
                    self.assertTrue(backend.engine.hardware.state.simulated)
                    backend.call("apply_corrections")
                    for name, parameters in (("gravimetry", {"collection_ul": 0.1}),
                                             ("dead_volume", {"window_ul": 0.1, "settle_s": 0}),
                                             ("viscosity", {"settle_s": 0, "average_s": 5})):
                        source = template_documents()[name]
                        source["parameter_values"].update(parameters)
                        with patch.object(backend.engine, "run", side_effect=AssertionError("planning actuated")):
                            plan = backend.preview("experiment_1", source)
                        self.assertFalse(backend.engine.recording_active)
                        backend.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"],
                                                          "timeout_s": 0.1})
                        deadline = time.monotonic() + 40
                        last_gate = None
                        while time.monotonic() < deadline:
                            current = backend.call("planned_protocols", {"plan_id": plan["plan_id"]})["plans"][0]
                            status = backend.engine.observation()["protocol"]
                            if status.get("confirmation_message") and status["step_index"] != last_gate:
                                last_gate = status["step_index"]
                                time.sleep(0.15)  # Preserve samples on both sides of operator gates.
                                backend.call("control_protocol", {"action": "confirm", "timeout_s": 0.1})
                            if current["state"] in {"completed", "failed", "cancelled"}:
                                break
                            time.sleep(0.01)
                        self.assertEqual(current["state"], "completed", current.get("error"))
                        self.assertFalse(backend.engine.recording_active)
                        channels = backend.engine.observation()["channels"]
                        self.assertTrue(all(c["requested_flow_ul_min"] in (None, 0) and
                                            c["requested_pressure_mbar"] in (None, 0) for c in channels))
                        data = backend.measurements(current["run_id"])
                        self.assertTrue(all(value is None for value in data["values"].values()))
                        values = {}
                        for key in data["values"]:
                            if key == "density_g_ml":
                                values[key] = 1.6
                            elif key == "flow_multiplier":
                                values[key] = 1
                            elif key.startswith("mass_"):
                                values[key] = 100 if "before" in key else 100.24
                            elif key.startswith("injection"):
                                values[key] = 0.05
                            elif key.startswith("arrival"):
                                values[key] = 0.25
                            elif key.startswith("timing_uncertainty"):
                                values[key] = 0.01
                        backend.measurements(current["run_id"], values)
                        directory = Path(backend.workdir) / "records" / "protocols" / current["run_id"]
                        with patch.object(backend.engine, "run", side_effect=AssertionError("calculation actuated")):
                            result = calculate_run(directory, name)
                        self.assertTrue(Path(result["path"]).is_file())
                        self.assertEqual(result["measurement_revision"], 1)
                        self.assertIn(result["result"]["status"], {"usable", "inconclusive"})
                        self.assertTrue((directory / "events.jsonl").read_text())
                finally:
                    backend.shutdown()

    def test_templates_load_outside_checkout_and_from_packaged_resources(self):
        from contextlib import chdir

        with tempfile.TemporaryDirectory() as tmp:
            with chdir(tmp):
                self.assertEqual(set(template_documents()), {
                    "dead_volume", "density", "dropseq", "flow_stability_scout", "gravimetry", "viscosity",
                })
            directory = Path(tmp) / "templates"
            directory.mkdir()
            (directory / "example.json").write_text(json.dumps(DOCUMENT))
            with patch("importlib.resources.files", return_value=Path(tmp)):
                self.assertEqual(set(template_documents()), {"short_run"})

    def test_dropseq_template_preserves_existing_recipe_with_bounded_execution(self):
        document = template_documents()["dropseq"]
        self.assertEqual(len(document["steps"]), 2)
        self.assertEqual(document["steps"][0]["trigger_type"], "volume")
        self.assertEqual(document["steps"][0]["trigger_params"], {"sensor_index": 0, "target_volume_ul": 150})
        self.assertEqual(document["steps"][1]["sensor_setpoints"], {})
        self.assertEqual(document["steps"][1]["trigger_type"], "time")
        self.assertEqual(document["steps"][1]["trigger_params"], {"duration_s": 0})
        for step in document["steps"]:
            self.assertEqual(step["on_complete"], "zero")
            self.assertTrue(step["confirm_message"])
        self.assertEqual(document["steps"][0]["timeout_s"], 120)
        admet = Admet()
        with patch.object(admet, "engine_action", side_effect=AssertionError("planning actuated")):
            plan = admet.plan_protocol(operation_id="run_json_protocol", settings={"protocol": document})
        self.assertEqual(plan["expected_duration_s"], 30)
        self.assertEqual(plan["steps"][0]["flow_setpoints_ul_min"], {"0": 300, "1": 40, "2": 40})

    def test_normalization_is_detached_and_rejects_unknown_fields(self):
        document = deepcopy(DOCUMENT)
        result = normalize(document)
        self.assertEqual(result["steps"][0]["on_complete"], "zero")
        self.assertNotIn("on_complete", document["steps"][0])
        document["extra"] = "unused"
        with self.assertRaisesRegex(ValueError, "unknown"):
            normalize(document)

    def test_calculation_declarations_and_measurement_units(self):
        document = deepcopy(DOCUMENT)
        document["calculations"] = [{"type": "recording_summary"}]
        document["measurements"] = {"mass": {"label": "Mass", "unit": "mg", "min": 0,
                                               "required": True, "step": 1}}
        self.assertEqual(normalize(document)["measurements"], document["measurements"])
        for change in ({"calculations": [{"type": "unknown"}]},
                       {"calculations": [{"type": "recording_summary", "unused": 1}]},
                       {"analysis": {"type": "recording_summary"}},
                       {"measurements": {"mass": {"label": "Mass", "unit": "kg"}}},
                       {"measurements": {"mass": {"label": "Mass", "min": 2, "max": 1}}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize({**document, **change})

    def test_invalid_settings_are_refused(self):
        for field, value in (("timeout_s", -1), ("repeat", 1.5), ("unknown", 1),
                             ("sensor_setpoints", {"-1": 10}),
                             ("sensor_setpoints", {"0": float("nan")}),
                             ("pressure_setpoints", {"0": 5})):
            with self.subTest(field=field, value=value):
                document = deepcopy(DOCUMENT)
                document["steps"][0][field] = value
                with self.assertRaises((ValueError, RuntimeError)):
                    normalize(document)

    def test_pressure_limits_are_not_a_protocol_field(self):
        document = deepcopy(DOCUMENT)
        self.assertNotIn("pressure_limits_mbar", normalize(document))
        document["pressure_limits_mbar"] = {"1": 500}
        with self.assertRaisesRegex(ValueError, "unknown protocol fields"):
            normalize(document)

    def test_detected_ranges_apply(self):
        channels = [{"index": 0, "detected": {"pressure_max_mbar": 2000, "sensor_max_ul_min": 1000}}]
        document = deepcopy(DOCUMENT)
        step = document["steps"][0]
        step.update(sensor_setpoints={}, pressure_setpoints={"0": 2000})
        validate_channels(normalize(document), channels)
        for mode, target in (("pressure_setpoints", 2001), ("sensor_setpoints", 1001)):
            step.update(sensor_setpoints={}, pressure_setpoints={})
            step[mode] = {"0": target}
            with self.assertRaisesRegex(ValueError, "detected range"):
                validate_channels(normalize(document), channels)
            step[mode] = {"4": 10}
            with self.assertRaisesRegex(ValueError, "not connected"):
                validate_channels(normalize(document), channels)
        for maximum in (None, float("nan"), float("inf")):
            step.update(sensor_setpoints={}, pressure_setpoints={"0": 10})
            channels[0]["detected"]["pressure_max_mbar"] = maximum
            with self.assertRaisesRegex(ValueError, "detected range"):
                validate_channels(normalize(document), channels)

    def test_unbounded_volume_is_refused(self):
        document = deepcopy(DOCUMENT)
        document["steps"][0].update(
            trigger_type="volume", trigger_params={"sensor_index": 0, "target_volume_ul": 10},
        )
        with self.assertRaisesRegex(ValueError, "timeout"):
            normalize(document)

    def test_group_repeat_expansion_is_bounded(self):
        document = deepcopy(DOCUMENT)
        step = document["steps"][0]
        step["group"] = "batch"
        document["steps"] = [deepcopy(step) for _ in range(20)]
        document["steps"][0]["repeat"] = 100
        with self.assertRaisesRegex(ValueError, "1000"):
            normalize(document)

    def test_invalid_saved_file_does_not_hide_other_protocols(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet()
            admet.create_project(f"{tmp}/p.admetp")
            saved = admet.do("save_protocol", {"protocol": DOCUMENT})
            bad = deepcopy(DOCUMENT)
            bad["steps"][0]["trigger_type"] = "typo"
            Path(saved["path"]).with_name("bad.json").write_text(json.dumps(bad))
            entries = admet.do("list_protocols", {})["protocols"]
            self.assertEqual(len(entries), 2)
            self.assertTrue(next(entry for entry in entries if entry["name"] == "bad")["error"])

    def test_timeout_fails_run_and_finalizes_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet()
            admet.create_project(f"{tmp}/p.admetp")
            try:
                admet.do("connect_fluidics", {"simulated": True})
                admet.do("apply_corrections", {})
                document = deepcopy(DOCUMENT)
                document["steps"][0].update(
                    sensor_setpoints={"0": 0}, trigger_type="volume",
                    trigger_params={"sensor_index": 0, "target_volume_ul": 100}, timeout_s=0.05,
                )
                plan = admet.plan_protocol(operation_id="run_json_protocol", settings={"protocol": document})
                admet.control_protocol(action="execute", plan_id=plan["plan_id"])
                admet.control_protocol(action="confirm", timeout_s=1)
                deadline = time.monotonic() + 3
                while admet._executing_plan_id and time.monotonic() < deadline:
                    time.sleep(0.01)
                finished = admet.planned_protocols()["plans"][0]
                self.assertEqual(finished["state"], "failed")
                self.assertIn("timed out", finished["error"])
                observed = admet.do("observe", {})
                self.assertFalse(observed["recording"]["active"])
                self.assertTrue(all(c["requested_flow_ul_min"] in (None, 0)
                                    for c in observed["channels"]))
            finally:
                admet.do("disconnect_fluidics", {})

    def test_plan_is_non_actuating(self):
        admet = Admet()
        with patch.object(admet, "engine_action", side_effect=AssertionError("hardware")):
            plan = admet.plan_protocol(operation_id="run_json_protocol", settings={"protocol": DOCUMENT})
        self.assertEqual(plan["steps"][0]["flow_setpoints_ul_min"], {"0": 10})
        self.assertNotIn("armed_safety_limits", plan)

    def test_simulated_plan_executes_and_zeros(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet()
            admet.create_project(f"{tmp}/test.admetp")
            try:
                admet.do("connect_fluidics", {"simulated": True})
                admet.do("apply_corrections", {})
                plan = admet.plan_protocol(operation_id="run_json_protocol", settings={"protocol": DOCUMENT})
                admet.control_protocol(action="execute", plan_id=plan["plan_id"], timeout_s=1)
                admet.control_protocol(action="confirm", timeout_s=1)
                admet.wait_for_protocol(timeout_s=2)
                deadline = time.monotonic() + 3
                while admet.planned_protocols()["plans"][0]["state"] == "executing":
                    if time.monotonic() >= deadline:
                        self.fail("plan did not complete")
                    time.sleep(0.01)
                self.assertEqual(
                    admet.planned_protocols()["plans"][0]["state"], "completed",
                )
                observation = admet.do("observe", {})
                self.assertTrue(all(c["requested_flow_ul_min"] in (None, 0)
                                    for c in observation["channels"]))
                completed = admet.planned_protocols()["plans"][0]
                run_dir = Path(tmp) / "test.admetp" / "records" / "protocols" / completed["run_id"]
                summary = json.loads((run_dir / "summary.json").read_text())
                self.assertEqual(summary["state"], "completed")
                self.assertTrue(Path(summary["artifacts"]["fluidics_csv"]).is_file())
                self.assertTrue((run_dir / "events.jsonl").read_text())
                self.assertEqual(json.loads((run_dir / "protocol.json").read_text()),
                                 normalize(DOCUMENT))
            finally:
                admet.do("disconnect_fluidics", {})

    def test_save_open_plan_file_survives_new_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = f"{tmp}/test.admetp"
            first = Admet()
            first.create_project(project)
            saved = first.do("save_protocol", {"protocol": DOCUMENT})
            with self.assertRaises(FileExistsError):
                first.do("save_protocol", {"protocol": DOCUMENT})
            second = Admet(project=project)
            self.assertEqual(second.do("list_protocols", {})["protocols"][0]["name"],
                             DOCUMENT["name"])
            reopened = second.do("list_protocols", {"name": DOCUMENT["name"]})
            self.assertEqual(reopened, saved)
            with patch.object(second, "engine_action", side_effect=AssertionError("setter")):
                plan = second.plan_protocol_file(path=saved["path"])
            path = Path(project) / "plans" / f"{plan['plan_id']}.json"
            self.assertFalse(path.exists())
            second.cancel_protocol_plan(plan_id=plan["plan_id"])
            self.assertFalse(path.exists())
            with self.assertRaises(ValueError):
                second.do("list_protocols", {"name": "../escape"})
