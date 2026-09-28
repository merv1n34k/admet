from copy import deepcopy
import tempfile
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
                self.assertEqual(
                    server.call("planned_protocols", {})["plans"][0]["state"], "completed",
                )
                observation = server.call("observe", {})
                self.assertTrue(all(c["requested_flow_ul_min"] in (None, 0)
                                    for c in observation["channels"]))
            finally:
                server.call("disconnect_fluidics", {})
