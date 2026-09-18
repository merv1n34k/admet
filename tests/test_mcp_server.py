"""The MCP surface: what a program sees, and what simulated mode refuses."""

import io
import json
import os
import unittest
from unittest.mock import patch

from admet.mcp.server import AdmetServer, SimulationRefused, handle, serve
from admet.mcp.tools import tools_for


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.server = AdmetServer(simulated=True)

    def test_every_action_an_engine_declares_becomes_a_tool(self):
        # The tool list is generated from the engine, so the two cannot drift.
        engine = self.server.api("acquisition").engine

        names = {tool["name"] for tool in tools_for(engine)}

        self.assertEqual(len(names), len(engine.actions))
        self.assertIn("acquisition_run_protocol", names)
        self.assertIn("acquisition_connect_fluidics", names)

    def test_a_tool_carries_the_parameters_its_action_accepts(self):
        tools = {tool["name"]: tool for tool in self.server.tools()}

        schema = tools["acquisition_set_channel_flow"]["inputSchema"]

        self.assertEqual(set(schema["properties"]), {"channel_index", "channel_flow_ul_min"})
        self.assertEqual(schema["properties"]["channel_flow_ul_min"]["type"], "number")
        self.assertEqual(schema["properties"]["channel_index"]["type"], "integer")
        self.assertFalse(schema["additionalProperties"])

    def test_a_choice_becomes_the_choices(self):
        tools = {tool["name"]: tool for tool in self.server.tools()}

        pipeline = tools["acquisition_run_protocol"]["inputSchema"]["properties"]["pipeline_name"]

        self.assertIn("Wash", pipeline["enum"])
        self.assertIn("Priming", pipeline["enum"])

    def test_a_tool_says_when_it_reaches_the_instrument(self):
        tools = {tool["name"]: tool for tool in self.server.tools()}

        self.assertIn("Reaches the instrument", tools["acquisition_run_protocol"]["description"])
        self.assertNotIn("Reaches the instrument", tools["acquisition_camera_status"]["description"])


class SimulationTests(unittest.TestCase):
    def test_connecting_is_forced_simulated(self):
        server = AdmetServer(simulated=True)

        settings = server._apply_simulation("connect_fluidics", {"start_polling": True})

        self.assertIs(settings["simulated"], True)

    def test_asking_for_real_hardware_is_refused(self):
        server = AdmetServer(simulated=True)

        with self.assertRaises(SimulationRefused) as caught:
            server._apply_simulation("connect_fluidics", {"simulated": False})

        self.assertIn("simulated", str(caught.exception))

    def test_running_a_protocol_is_allowed_because_the_rig_is_simulated(self):
        # What makes the session safe is the connection. Refusing the actions that
        # follow would leave simulated mode unable to simulate anything.
        server = AdmetServer(simulated=True)

        settings = server._apply_simulation("run_protocol", {"pipeline_name": "Wash"})

        self.assertEqual(settings, {"pipeline_name": "Wash"})

    def test_the_camera_is_refused_unless_its_emulator_is_switched_on(self):
        server = AdmetServer(simulated=True)
        previous = os.environ.pop("PYLON_CAMEMU", None)
        try:
            with self.assertRaises(SimulationRefused) as caught:
                server._apply_simulation("connect_camera", {})
            self.assertIn("PYLON_CAMEMU", str(caught.exception))
        finally:
            if previous is not None:
                os.environ["PYLON_CAMEMU"] = previous

    def test_live_mode_changes_nothing(self):
        server = AdmetServer(simulated=False)

        settings = server._apply_simulation("connect_fluidics", {"simulated": False})

        self.assertIs(settings["simulated"], False)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = AdmetServer(simulated=True)

    def test_initialize_answers_with_the_protocol_version(self):
        response = handle(self.server, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})

        self.assertEqual(response["result"]["serverInfo"]["name"], "admet")
        self.assertIn("tools", response["result"]["capabilities"])

    def test_a_notification_gets_no_reply(self):
        # Replying to a notification is a protocol violation, not a courtesy.
        self.assertIsNone(
            handle(self.server, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_an_unknown_method_is_an_error_with_a_code(self):
        response = handle(self.server, {"jsonrpc": "2.0", "id": 2, "method": "nonsense"})

        self.assertEqual(response["error"]["code"], -32601)

    def test_an_unknown_tool_is_reported_to_the_caller_not_raised(self):
        response = handle(
            self.server,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope_nope"}},
        )

        self.assertTrue(response["result"]["isError"])
        self.assertIn("no tool named", response["result"]["content"][0]["text"])

    def test_a_failing_action_answers_with_the_reason(self):
        response = handle(
            self.server,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "acquisition_no_such_action", "arguments": {}},
            },
        )

        self.assertTrue(response["result"]["isError"])

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
                    "params": {
                        "name": "acquisition_connect_fluidics",
                        "arguments": {"start_polling": False},
                    },
                },
            )
        )
        stdout = io.StringIO()

        with patch("sys.stderr", io.StringIO()):
            exit_code = serve(simulated=True, stdin=io.StringIO(requests), stdout=stdout)

        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(exit_code, 0)
        # Three replies for four messages: the notification is not answered.
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])
        self.assertGreater(len(replies[1]["result"]["tools"]), 20)
        connected = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertTrue(connected["metadata"]["connected"])
        self.assertTrue(connected["metadata"]["simulated"])

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
