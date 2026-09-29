from copy import deepcopy
import tempfile
import json
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from admet.mcp.server import AdmetServer
from admet.workflows.json_protocol import normalize, template_documents, validate_channels


DOCUMENT = {
    "name": "short_run",
    "pressure_limits_mbar": {"0": 1000},
    "steps": [{
        "sensor_setpoints": {"0": 10},
        "trigger_type": "time", "trigger_params": {"duration_s": 0.1},
        "confirm_message": "Start short run?",
    }],
}


class JsonProtocolTests(unittest.TestCase):
    def test_dropseq_template_preserves_existing_recipe_with_bounded_execution(self):
        from admet.workflows.operations import operation
        from admet.workflows.protocols import build_dropseq_protocol

        settings = {p.name: p.default for p in operation("run_dropseq").params}
        recipe = build_dropseq_protocol(settings)
        document = template_documents()["dropseq"]
        self.assertEqual(document["pressure_limits_mbar"], {})
        self.assertEqual(len(document["steps"]), len(recipe))
        for step, declared in zip(document["steps"], recipe, strict=True):
            self.assertEqual(step["sensor_setpoints"], {str(k): v for k, v in declared.sensor_setpoints.items()})
            self.assertEqual(step["trigger_type"], declared.trigger_type)
            self.assertEqual(step["trigger_params"], declared.trigger_params)
            self.assertEqual(step["on_complete"], "zero")
            self.assertTrue(step["confirm_message"])
        self.assertEqual(document["steps"][0]["timeout_s"], 120)
        server = AdmetServer(simulated=True)
        with patch.object(server.admet, "engine_action", side_effect=AssertionError("planning actuated")):
            plan = server.call("plan_protocol", {
                "operation_id": "run_json_protocol", "settings": {"protocol": document},
            })
        self.assertEqual(plan["expected_duration_s"], 30)
        self.assertEqual(plan["steps"][0]["flow_setpoints_ul_min"], {"0": 300, "1": 40, "2": 40})
        self.assertEqual(plan["armed_safety_limits"]["pressure_mbar"], {})

    def test_normalization_is_detached_and_rejects_unknown_fields(self):
        document = deepcopy(DOCUMENT)
        result = normalize(document)
        self.assertEqual(result["steps"][0]["on_complete"], "zero")
        self.assertNotIn("on_complete", document["steps"][0])
        document["extra"] = "unused"
        with self.assertRaisesRegex(ValueError, "unknown"):
            normalize(document)

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

    def test_limits_are_optional_and_explicit_limits_still_apply(self):
        document = deepcopy(DOCUMENT)
        document.pop("pressure_limits_mbar")
        self.assertEqual(normalize(document)["pressure_limits_mbar"], {})
        document["pressure_limits_mbar"] = {"1": 500}
        self.assertEqual(normalize(document)["pressure_limits_mbar"], {"1": 500})
        document["steps"][0].update(sensor_setpoints={}, pressure_setpoints={"1": 500})
        with self.assertRaisesRegex(ValueError, "below its pressure limit"):
            normalize(document)

    def test_detected_ranges_apply_without_software_trips(self):
        channels = [{"index": 0, "detected": {"pressure_max_mbar": 2000, "sensor_max_ul_min": 1000}}]
        document = deepcopy(DOCUMENT)
        document.pop("pressure_limits_mbar")
        step = document["steps"][0]
        step.update(sensor_setpoints={}, pressure_setpoints={"0": 2000})
        validate_channels(normalize(document), channels)
        document["pressure_limits_mbar"] = {"0": 2000}
        step["pressure_setpoints"] = {"0": 1999}
        validate_channels(normalize(document), channels)
        document["pressure_limits_mbar"] = {}
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
            server = AdmetServer(simulated=True, project=f"{tmp}/p.admetp", create=True)
            saved = server.call("save_protocol", {"protocol": DOCUMENT})
            bad = deepcopy(DOCUMENT)
            bad["steps"][0]["trigger_type"] = "typo"
            Path(saved["path"]).with_name("bad.json").write_text(json.dumps(bad))
            entries = server.call("list_protocols", {})["protocols"]
            self.assertEqual(len(entries), 2)
            self.assertTrue(next(entry for entry in entries if entry["name"] == "bad")["error"])

    def test_timeout_fails_run_and_finalizes_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = AdmetServer(simulated=True, project=f"{tmp}/p.admetp", create=True)
            try:
                server.call("connect_fluidics", {})
                server.call("apply_corrections", {})
                document = deepcopy(DOCUMENT)
                document["steps"][0].update(
                    sensor_setpoints={"0": 0}, trigger_type="volume",
                    trigger_params={"sensor_index": 0, "target_volume_ul": 100}, timeout_s=0.05,
                )
                plan = server.call("plan_protocol", {
                    "operation_id": "run_json_protocol", "settings": {"protocol": document},
                })
                server.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"]})
                server.call("control_protocol", {"action": "confirm", "timeout_s": 1})
                deadline = time.monotonic() + 3
                while server.admet._executing_plan_id and time.monotonic() < deadline:
                    time.sleep(0.01)
                finished = server.call("planned_protocols", {})["plans"][0]
                self.assertEqual(finished["state"], "failed")
                self.assertIn("timed out", finished["error"])
                observed = server.call("observe", {})
                self.assertFalse(observed["recording"]["active"])
                self.assertTrue(all(c["requested_flow_ul_min"] in (None, 0)
                                    for c in observed["channels"]))
            finally:
                server.call("disconnect_fluidics", {})

    def test_plan_is_non_actuating_and_direct_mcp_start_is_refused(self):
        server = AdmetServer(simulated=True)
        with patch.object(server.admet, "engine_action", side_effect=AssertionError("hardware")):
            plan = server.call("plan_protocol", {
                "operation_id": "run_json_protocol", "settings": {"protocol": DOCUMENT},
            })
        self.assertEqual(plan["steps"][0]["flow_setpoints_ul_min"], {"0": 10})
        self.assertEqual(plan["armed_safety_limits"]["pressure_mbar"], {"0": 1000})
        with self.assertRaisesRegex(RuntimeError, "plan_protocol"):
            server.call("run_json_protocol", {"protocol": DOCUMENT})

    def test_simulated_plan_executes_and_zeros(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = AdmetServer(simulated=True, project=f"{tmp}/test.admetp", create=True)
            try:
                server.call("connect_fluidics", {})
                server.call("apply_corrections", {})
                plan = server.call("plan_protocol", {
                    "operation_id": "run_json_protocol", "settings": {"protocol": DOCUMENT},
                })
                server.call("control_protocol", {
                    "action": "execute", "plan_id": plan["plan_id"], "timeout_s": 1,
                })
                server.call("control_protocol", {"action": "confirm", "timeout_s": 1})
                server.admet.wait_for_protocol(timeout_s=2)
                deadline = time.monotonic() + 3
                while server.call("planned_protocols", {})["plans"][0]["state"] == "executing":
                    if time.monotonic() >= deadline:
                        self.fail("plan did not complete")
                    time.sleep(0.01)
                self.assertEqual(
                    server.call("planned_protocols", {})["plans"][0]["state"], "completed",
                )
                observation = server.call("observe", {})
                self.assertTrue(all(c["requested_flow_ul_min"] in (None, 0)
                                    for c in observation["channels"]))
                completed = server.call("planned_protocols", {})["plans"][0]
                run_dir = Path(tmp) / "test.admetp" / "records" / "protocols" / completed["run_id"]
                summary = json.loads((run_dir / "summary.json").read_text())
                self.assertEqual(summary["state"], "completed")
                self.assertTrue(Path(summary["artifacts"]["fluidics_csv"]).is_file())
                self.assertTrue((run_dir / "events.jsonl").read_text())
                self.assertEqual(json.loads((run_dir / "protocol.json").read_text()),
                                 normalize(DOCUMENT))
            finally:
                server.call("disconnect_fluidics", {})

    def test_save_open_plan_file_survives_new_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = f"{tmp}/test.admetp"
            first = AdmetServer(simulated=True, project=project, create=True)
            saved = first.call("save_protocol", {"protocol": DOCUMENT})
            with self.assertRaises(FileExistsError):
                first.call("save_protocol", {"protocol": DOCUMENT})
            second = AdmetServer(simulated=True, project=project)
            self.assertEqual(second.call("list_protocols", {})["protocols"][0]["name"],
                             DOCUMENT["name"])
            reopened = second.call("list_protocols", {"name": DOCUMENT["name"]})
            self.assertEqual(reopened, saved)
            with patch.object(second.admet, "engine_action", side_effect=AssertionError("setter")):
                plan = second.call("plan_protocol_file", {"path": saved["path"]})
            path = Path(project) / "plans" / f"{plan['plan_id']}.json"
            self.assertFalse(path.exists())
            second.call("cancel_protocol_plan", {"plan_id": plan["plan_id"]})
            self.assertFalse(path.exists())
            with self.assertRaises(ValueError):
                second.call("list_protocols", {"name": "../escape"})
