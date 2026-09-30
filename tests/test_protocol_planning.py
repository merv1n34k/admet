"""Planning and bounded protocol control through the direct Python binding."""

import unittest

from admet.core.service import Admet


class ProtocolPlanningTests(unittest.TestCase):
    def setUp(self):
        self.admet = Admet()
        self.admet.do("connect_fluidics", {"simulated": True})
        self.admet.do("apply_corrections")
        self.addCleanup(self.admet.do, "disconnect_fluidics")

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

    def test_volume_step_reports_nominal_duration_from_its_requested_flow(self):
        plan = self.admet.plan_protocol("run_steps", {
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
        self.assertEqual(self.admet.state()["running"], False)
        self.assertFalse(self.admet.engine("acquisition").recording_active)

    def test_confirmation_wait_is_excluded_from_calculated_active_duration(self):
        plan = self.admet.plan_protocol("run_steps", {
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
        first = self.admet.plan_protocol("run_steps", self.settings())
        second = self.admet.plan_protocol("run_steps", self.settings())

        plans = self.admet.planned_protocols()["plans"]
        self.assertEqual([plan["plan_id"] for plan in plans], [first["plan_id"], second["plan_id"]])
        self.assertEqual([plan["state"] for plan in plans], ["planned", "planned"])

    def test_completed_plan_cannot_run_twice(self):
        plan = self.admet.plan_protocol("run_steps", self.settings())
        result = self.admet.control_protocol(action="execute", plan_id=plan["plan_id"])
        self.assertEqual(result["yield"]["reason"], "step_completed")
        self.admet.wait_for_protocol(timeout_s=2, poll_s=0.01)
        self.assertEqual(self.admet.planned_protocols(plan["plan_id"])["plans"][0]["state"], "completed")
        with self.assertRaisesRegex(RuntimeError, "completed"):
            self.admet.execute_protocol_plan(plan["plan_id"])

    def test_wait_yields_gate_step_outcomes_and_protocol_completion(self):
        plan = self.admet.plan_protocol("run_steps", {
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
        gate = self.admet.control_protocol(
            action="execute", plan_id=plan["plan_id"], timeout_s=1.0
        )["yield"]

        self.assertEqual(gate["reason"], "confirmation_required")
        first_step = self.admet.control_protocol(
            action="confirm",
            after_sequence=gate["next_sequence"], timeout_s=1.0
        )["yield"]
        second_step = self.admet.control_protocol(
            action="wait",
            after_sequence=first_step["next_sequence"], timeout_s=1.0
        )["yield"]
        completed = self.admet.control_protocol(
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
        plan = self.admet.plan_protocol("run_steps", self.settings(duration_s=2.0))
        result = self.admet.control_protocol(
            action="execute", plan_id=plan["plan_id"], timeout_s=0.1
        )["yield"]

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["reason"], "timeout")
        self.assertGreater(result["next_sequence"], 0)
        self.admet.do("stop_protocol")

    def test_wait_rejects_an_unbounded_timeout(self):
        with self.assertRaisesRegex(ValueError, "between 0.1 and 60"):
            self.admet.control_protocol(action="wait", timeout_s=61.0)

    def test_cancelled_and_stale_plans_are_refused(self):
        first = self.admet.plan_protocol("run_steps", self.settings())
        self.admet.cancel_protocol_plan(first["plan_id"])
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.admet.execute_protocol_plan(first["plan_id"])

        stale = self.admet.plan_protocol("run_steps", self.settings())
        self.admet.mark("correction_settings", {"changed": True})
        with self.assertRaisesRegex(RuntimeError, "stale"):
            self.admet.execute_protocol_plan(stale["plan_id"])

    def test_direct_python_binding_remains_the_expert_escape_hatch(self):
        result = self.admet.do("run_steps", self.settings())

        self.assertTrue(result["started"])
        self.admet.wait_for_protocol(timeout_s=2.0, poll_s=0.01)
