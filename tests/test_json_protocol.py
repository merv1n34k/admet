from copy import deepcopy
import tempfile
import json
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from admet.mcp.server import AdmetServer
from admet.workflows.json_protocol import normalize


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
                             ("sensor_setpoints", {"1": 10}),
                             ("pressure_setpoints", {"0": 5})):
            with self.subTest(field=field, value=value):
                document = deepcopy(DOCUMENT)
                document["steps"][0][field] = value
                with self.assertRaises((ValueError, RuntimeError)):
                    normalize(document)

    def test_unbounded_volume_is_refused(self):
        document = deepcopy(DOCUMENT)
        document["steps"][0].update(
            trigger_type="volume", trigger_params={"sensor_index": 0, "target_volume_ul": 10},
        )
        with self.assertRaisesRegex(ValueError, "timeout"):
            normalize(document)

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
                run_dir = Path(tmp) / "test.admetp" / "records" / "protocols" / plan["plan_id"]
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
            self.assertEqual(json.loads(path.read_text())["digest"], plan["digest"])
            second.call("cancel_protocol_plan", {"plan_id": plan["plan_id"]})
            self.assertEqual(json.loads(path.read_text())["state"], "cancelled")
            with self.assertRaises(ValueError):
                second.call("list_protocols", {"name": "../escape"})
