"""The MCP surface: operations and pipelines, their guards, and what simulated mode refuses."""

import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from admet.mcp.server import AdmetServer, SimulationRefused, handle, serve


def _call(server, name, arguments=None, request_id=1):
    response = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )["result"]
    return response["content"][0]["text"], response["isError"]


class SurfaceTests(unittest.TestCase):
    def setUp(self):
        self.server = AdmetServer(simulated=True)
        self.names = {tool["name"] for tool in self.server.tools()}

    def test_what_is_offered_is_the_workflow_layer(self):
        self.assertIn("run_priming", self.names)
        self.assertIn("pipeline_setup", self.names)
        self.assertIn("create_project", self.names)

    def test_all_three_levels_are_offered(self):
        # Low: every device action, engine-prefixed, so a model can configure the
        # camera and the channels in full. Med: a protocol written as steps.
        # High: the pipelines.
        self.assertIn("acquisition_set_camera_settings", self.names)
        self.assertIn("acquisition_set_channel_pressure", self.names)
        self.assertIn("run_steps", self.names)
        self.assertIn("pipeline_setup", self.names)

    def test_a_name_says_which_level_it_belongs_to(self):
        # Engine-prefixed is the unguarded low level; bare is the guarded
        # operation. Both exist for the same call and must not be confused.
        self.assertIn("acquisition_connect_fluidics", self.names)
        self.assertIn("connect_fluidics", self.names)

    def test_a_tool_says_what_it_needs_first(self):
        tools = {tool["name"]: tool for tool in self.server.tools()}

        self.assertIn("Needs: fluidics, corrections, idle", tools["run_priming"]["description"])
        self.assertNotIn("Needs:", tools["connect_fluidics"]["description"])

    def test_a_tool_says_which_half_of_the_system_it_belongs_to(self):
        tools = {tool["name"]: tool for tool in self.server.tools()}

        self.assertTrue(tools["run_priming"]["description"].startswith("[control]"))
        self.assertTrue(tools["run_analysis"]["description"].startswith("[analyze]"))
        self.assertTrue(tools["open_project"]["description"].startswith("[general]"))

    def test_a_tool_carries_the_parameters_its_operation_declares(self):
        tools = {tool["name"]: tool for tool in self.server.tools()}

        schema = tools["run_priming"]["inputSchema"]

        self.assertEqual(
            set(schema["properties"]),
            {"prime_oil_volume_ul", "prime_aqueous_volume_ul", "tick_s"},
        )
        self.assertEqual(schema["properties"]["prime_oil_volume_ul"]["type"], "number")
        self.assertFalse(schema["additionalProperties"])

    def test_every_pipeline_can_be_planned_as_well_as_run(self):
        for name in ("setup", "checks", "shutdown"):
            with self.subTest(pipeline=name):
                self.assertIn(f"pipeline_{name}", self.names)
                self.assertIn(f"plan_{name}", self.names)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.server = AdmetServer(simulated=True)

    def test_an_operation_out_of_order_is_refused_with_the_reason(self):
        text, is_error = _call(self.server, "run_priming")

        self.assertTrue(is_error)
        self.assertIn("the fluidics are not connected", text)

    def test_a_protocol_is_refused_until_corrections_are_applied(self):
        _call(self.server, "connect_fluidics")

        text, is_error = _call(self.server, "run_characterisation")

        self.assertTrue(is_error)
        self.assertIn("correction factors have not been applied", text)

    def test_the_guard_lifts_once_the_condition_is_met(self):
        _call(self.server, "connect_fluidics")
        _call(self.server, "apply_corrections")

        text, is_error = _call(self.server, "run_priming", {"prime_oil_volume_ul": 2.0, "tick_s": 0.1})

        self.assertFalse(is_error, text)
        _call(self.server, "stop_protocol")
        _call(self.server, "disconnect_fluidics")

    def test_a_setting_the_operation_does_not_have_is_refused(self):
        text, is_error = _call(self.server, "connect_fluidics", {"nonsense": 1})

        self.assertTrue(is_error)
        self.assertIn("has no setting", text)

    def test_planning_a_pipeline_runs_nothing(self):
        text, is_error = _call(self.server, "plan_setup")

        self.assertFalse(is_error)
        stages = json.loads(text)["stages"]
        self.assertEqual([stage["operation"] for stage in stages], ["connect_fluidics", "apply_corrections", "run_priming"])
        self.assertFalse(self.server.admet.state()["fluidics"])


class SimulationTests(unittest.TestCase):
    def test_connecting_is_forced_simulated(self):
        server = AdmetServer(simulated=True)

        text, _is_error = _call(server, "connect_fluidics")

        self.assertTrue(json.loads(text)["simulated"])
        _call(server, "disconnect_fluidics")

    def test_asking_for_real_hardware_is_refused(self):
        server = AdmetServer(simulated=True)

        with self.assertRaises(SimulationRefused):
            server._simulated("connect", {"simulated": False})

    def test_an_operation_without_a_simulated_switch_passes_through(self):
        # What makes the session safe is the connection, so the operations after
        # it need no flag of their own.
        server = AdmetServer(simulated=True)

        self.assertEqual(server._simulated("run_priming", {"tick_s": 0.1}), {"tick_s": 0.1})

    def test_the_camera_is_refused_unless_its_emulator_is_switched_on(self):
        server = AdmetServer(simulated=True)
        previous = os.environ.pop("PYLON_CAMEMU", None)
        try:
            with self.assertRaises(SimulationRefused) as caught:
                server._simulated("connect_camera", {})
            self.assertIn("PYLON_CAMEMU", str(caught.exception))
        finally:
            if previous is not None:
                os.environ["PYLON_CAMEMU"] = previous

    def test_live_mode_changes_nothing(self):
        server = AdmetServer(simulated=False)

        self.assertEqual(server._simulated("connect", {"simulated": False}), {"simulated": False})


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = AdmetServer(simulated=True)

    def test_initialize_answers_with_the_protocol_version(self):
        response = handle(self.server, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})

        self.assertEqual(response["result"]["serverInfo"]["name"], "admet")
        self.assertIn("tools", response["result"]["capabilities"])

    def test_a_notification_gets_no_reply(self):
        self.assertIsNone(
            handle(self.server, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_an_unknown_method_is_an_error_with_a_code(self):
        response = handle(self.server, {"jsonrpc": "2.0", "id": 2, "method": "nonsense"})

        self.assertEqual(response["error"]["code"], -32601)

    def test_an_unknown_tool_names_what_there_is(self):
        text, is_error = _call(self.server, "no_such_operation")

        self.assertTrue(is_error)
        self.assertIn("unknown operation", text)

    def test_a_client_opens_a_project_before_it_writes_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            text, is_error = _call(
                self.server, "create_project", {"path": f"{tmp}/rig.admetp"}
            )

            self.assertFalse(is_error)
            payload = json.loads(text)
            self.assertTrue(payload["open"])
            self.assertEqual(payload["project_id"], "rig")

    def test_recording_without_a_project_is_refused(self):
        text, is_error = _call(self.server, "start_recording")

        self.assertTrue(is_error)
        self.assertIn("no project is open", text)

    def test_a_session_runs_from_stdin_to_stdout(self):
        requests = "\n".join(
            json.dumps(request)
            for request in (
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "connect_fluidics", "arguments": {}},
                },
            )
        )
        stdout = io.StringIO()

        with patch("sys.stderr", io.StringIO()):
            exit_code = serve(simulated=True, stdin=io.StringIO(requests), stdout=stdout)

        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(exit_code, 0)
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])
        self.assertGreater(len(replies[1]["result"]["tools"]), 50)
        connected = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertTrue(connected["connected"])
        self.assertTrue(connected["simulated"])

    def test_a_malformed_line_does_not_stop_the_server(self):
        stdout = io.StringIO()
        stream = io.StringIO(
            "{ not json\n" + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n"
        )

        with patch("sys.stderr", io.StringIO()):
            serve(simulated=True, stdin=stream, stdout=stdout)

        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["id"], 9)


if __name__ == "__main__":
    unittest.main()
