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

    def test_what_is_offered_is_the_guarded_operations(self):
        self.assertIn("run_priming", self.names)
        self.assertIn("create_project", self.names)
        self.assertIn("run_steps", self.names)

    def test_no_unguarded_engine_action_is_offered(self):
        # A guard is the whole point of an operation, and over a wire to a model
        # an unguarded primitive is the wrong default. They stay reachable from
        # the Python binding, which is not this.
        self.assertFalse({name for name in self.names if name.startswith("acquisition_")})
        self.assertNotIn("set_channel_pressure", self.names)
        self.assertNotIn("calibrate_channels", self.names)

    def test_every_tool_is_an_operation_or_describe(self):
        from admet.workflows.operations import BY_ID

        planning = {
            "plan_protocol", "planned_protocols", "control_protocol",
            "cancel_protocol_plan",
        }
        hidden_controls = {
            "pause_protocol", "resume_protocol", "stop_protocol",
            "confirm_protocol", "skip_protocol",
        }
        self.assertEqual(self.names - {"describe"} - planning, set(BY_ID) - hidden_controls)
        self.assertFalse(self.names & hidden_controls)

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

    def test_manual_flow_exposes_a_bounded_control_lease(self):
        tools = {tool["name"]: tool for tool in self.server.tools()}

        lease = tools["set_channel_flow"]["inputSchema"]["properties"]["control_lease_s"]

        self.assertEqual(lease["default"], 10.0)
        self.assertEqual(lease["minimum"], 1.0)
        self.assertEqual(lease["maximum"], 60.0)

    def test_protocol_control_has_actions_and_a_bounded_cursor_schema(self):
        tools = {tool["name"]: tool for tool in self.server.tools()}
        schema = tools["control_protocol"]["inputSchema"]

        self.assertEqual(schema["required"], ["action"])
        self.assertIn("confirm", schema["properties"]["action"]["enum"])
        self.assertIn("execute", schema["properties"]["action"]["enum"])
        self.assertEqual(schema["properties"]["after_sequence"]["minimum"], 0)
        self.assertEqual(schema["properties"]["timeout_s"]["minimum"], 0.1)
        self.assertEqual(schema["properties"]["timeout_s"]["maximum"], 60.0)



class GuardTests(unittest.TestCase):
    def setUp(self):
        self.server = AdmetServer(simulated=True)

    def test_an_operation_out_of_order_is_refused_with_the_reason(self):
        text, is_error = _call(self.server, "run_priming")

        self.assertTrue(is_error)
        self.assertIn("use plan_protocol", text)

    def test_a_protocol_is_refused_until_corrections_are_applied(self):
        _call(self.server, "connect_fluidics")

        text, is_error = _call(self.server, "run_characterisation")

        self.assertTrue(is_error)
        self.assertIn("use plan_protocol", text)

    def test_direct_protocol_start_remains_refused_once_guards_are_met(self):
        _call(self.server, "connect_fluidics")
        _call(self.server, "apply_corrections")

        text, is_error = _call(self.server, "run_priming", {"prime_oil_volume_ul": 2.0, "tick_s": 0.1})

        self.assertTrue(is_error, text)
        self.assertIn("control_protocol", text)
        _call(self.server, "disconnect_fluidics")

    def test_a_setting_the_operation_does_not_have_is_refused(self):
        text, is_error = _call(self.server, "connect_fluidics", {"nonsense": 1})

        self.assertTrue(is_error)
        self.assertIn("has no setting", text)


class PlanningBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.server = AdmetServer(simulated=True)
        _call(self.server, "connect_fluidics")
        _call(self.server, "apply_corrections")
        self.addCleanup(self.server.admet.do, "disconnect_fluidics")

    @staticmethod
    def settings(duration_s=0.05):
        return {
            "steps": [{
                "name": "safe simulated pulse",
                "sensor_setpoints": {"0": 1.0},
                "trigger_type": "time",
                "trigger_params": {"duration_s": duration_s},
                "on_complete": "zero",
            }],
            "tick_s": 0.01,
        }

    def test_planning_does_not_call_an_engine_action_or_start_anything(self):
        with patch.object(self.server.admet, "engine_action") as action:
            text, is_error = _call(self.server, "plan_protocol", {
                "operation_id": "run_steps", "settings": self.settings(),
            })

        self.assertFalse(is_error, text)
        action.assert_not_called()
        plan = json.loads(text)
        self.assertEqual(plan["state"], "planned")
        self.assertEqual(plan["steps"][0]["flow_setpoints_ul_min"], {"0": 1.0})

    def test_volume_step_reports_nominal_duration_from_its_requested_flow(self):
        plan = self.server.admet.plan_protocol("run_steps", {
            "steps": [{
                "name": "meter oil",
                "sensor_setpoints": {"0": 100.0},
                "trigger_type": "volume",
                "trigger_params": {"sensor_index": 0, "target_volume_ul": 50.0},
                "on_complete": "zero",
            }],
        })

        self.assertEqual(plan["steps"][0]["expected_duration_s"], 30.0)
        self.assertEqual(plan["expected_duration_s"], 30.0)
        self.assertEqual(self.server.admet.state()["running"], False)
        self.assertFalse(self.server.admet.engine("acquisition").recording_active)

    def test_confirmation_wait_is_excluded_from_calculated_active_duration(self):
        plan = self.server.admet.plan_protocol("run_steps", {
            "steps": [
                {
                    "name": "check mapping",
                    "trigger_type": "confirmation",
                    "trigger_params": {"message": "Proceed?"},
                    "on_complete": "zero",
                },
                {
                    "name": "meter oil",
                    "sensor_setpoints": {"0": 100.0},
                    "trigger_type": "volume",
                    "trigger_params": {"sensor_index": 0, "target_volume_ul": 50.0},
                    "on_complete": "zero",
                },
            ],
        })

        self.assertEqual(plan["steps"][0]["expected_duration_s"], 0.0)
        self.assertEqual(plan["expected_duration_s"], 30.0)

    def test_multiple_plans_for_the_same_operation_remain_available(self):
        first = self.server.admet.plan_protocol("run_steps", self.settings())
        second = self.server.admet.plan_protocol("run_steps", self.settings())

        plans = self.server.admet.planned_protocols()["plans"]
        self.assertEqual([plan["plan_id"] for plan in plans], [first["plan_id"], second["plan_id"]])
        self.assertEqual([plan["state"] for plan in plans], ["planned", "planned"])

    def test_execute_accepts_only_the_plan_id_and_cannot_run_twice(self):
        plan = json.loads(_call(self.server, "plan_protocol", {
            "operation_id": "run_steps", "settings": self.settings(),
        })[0])

        result, is_error = _call(self.server, "control_protocol", {
            "action": "execute", "plan_id": plan["plan_id"],
        })
        self.assertFalse(is_error, result)
        self.assertEqual(json.loads(result)["yield"]["reason"], "step_completed")
        self.server.admet.wait_for_protocol(timeout_s=2.0, poll_s=0.01)
        self.assertEqual(
            self.server.admet.planned_protocols(plan["plan_id"])["plans"][0]["state"],
            "completed",
        )
        refused, is_error = _call(
            self.server, "control_protocol", {"action": "execute", "plan_id": plan["plan_id"]}
        )
        self.assertTrue(is_error)
        self.assertIn("completed", refused)

    def test_wait_yields_gate_step_outcomes_and_protocol_completion(self):
        plan = self.server.admet.plan_protocol("run_steps", {
            "steps": [
                {
                    "name": "operator gate",
                    "trigger_type": "confirmation",
                    "trigger_params": {"message": "Proceed?"},
                    "on_complete": "zero",
                },
                {
                    "name": "brief pulse",
                    "sensor_setpoints": {"0": 1.0},
                    "trigger_type": "time",
                    "trigger_params": {"duration_s": 0.02},
                    "on_complete": "zero",
                },
            ],
            "tick_s": 0.005,
        })
        gate = self.server.admet.control_protocol(
            action="execute", plan_id=plan["plan_id"], timeout_s=1.0
        )["yield"]

        self.assertEqual(gate["reason"], "confirmation_required")
        first_step = self.server.admet.control_protocol(
            action="confirm",
            after_sequence=gate["next_sequence"], timeout_s=1.0
        )["yield"]
        second_step = self.server.admet.control_protocol(
            action="wait",
            after_sequence=first_step["next_sequence"], timeout_s=1.0
        )["yield"]
        completed = self.server.admet.control_protocol(
            action="wait",
            after_sequence=second_step["next_sequence"], timeout_s=1.0
        )["yield"]

        self.assertEqual(first_step["reason"], "step_completed")
        self.assertEqual(second_step["reason"], "step_completed")
        self.assertEqual(completed["reason"], "protocol_completed")
        cursors = [gate, first_step, second_step, completed]
        self.assertEqual(
            [item["next_sequence"] for item in cursors],
            sorted(item["next_sequence"] for item in cursors),
        )

    def test_execute_times_out_without_returning_progress_as_a_milestone(self):
        plan = self.server.admet.plan_protocol("run_steps", self.settings(duration_s=2.0))
        result = self.server.admet.control_protocol(
            action="execute", plan_id=plan["plan_id"], timeout_s=0.1
        )["yield"]

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["reason"], "timeout")
        self.assertGreater(result["next_sequence"], 0)
        self.server.admet.do("stop_protocol")

    def test_wait_rejects_an_unbounded_timeout(self):
        with self.assertRaisesRegex(ValueError, "between 0.1 and 60"):
            self.server.admet.control_protocol(action="wait", timeout_s=61.0)

    def test_cancelled_and_stale_plans_are_refused(self):
        first = self.server.admet.plan_protocol("run_steps", self.settings())
        self.server.admet.cancel_protocol_plan(first["plan_id"])
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.server.admet.execute_protocol_plan(first["plan_id"])

        stale = self.server.admet.plan_protocol("run_steps", self.settings())
        self.server.admet.mark("correction_settings", {"changed": True})
        with self.assertRaisesRegex(RuntimeError, "stale"):
            self.server.admet.execute_protocol_plan(stale["plan_id"])

    def test_direct_python_binding_remains_the_expert_escape_hatch(self):
        result = self.server.admet.do("run_steps", self.settings())

        self.assertTrue(result["started"])
        self.server.admet.wait_for_protocol(timeout_s=2.0, poll_s=0.01)

    def test_every_protocol_producing_mcp_tool_refuses_direct_start(self):
        from admet.workflows.operations import OPERATIONS

        for operation in OPERATIONS:
            if not operation.starts_protocol:
                continue
            with self.subTest(operation=operation.id):
                text, is_error = _call(self.server, operation.id)
                self.assertTrue(is_error)
                self.assertIn("plan_protocol", text)



class SimulationTests(unittest.TestCase):
    def test_connecting_is_forced_simulated(self):
        server = AdmetServer(simulated=True)

        text, _is_error = _call(server, "connect_fluidics")

        self.assertTrue(json.loads(text)["simulated"])
        _call(server, "disconnect_fluidics")

    def test_asking_for_real_hardware_is_refused(self):
        server = AdmetServer(simulated=True)

        with self.assertRaises(SimulationRefused):
            server._simulated("connect_fluidics", {"simulated": False})

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

        self.assertEqual(server._simulated("connect_fluidics", {"simulated": False}), {"simulated": False})


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
        self.assertGreater(len(replies[1]["result"]["tools"]), 20)
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
