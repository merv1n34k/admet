"""Operations and the pipelines built from them.

An operation is a primitive plus the conditions under which using it is not a
mistake. These tests are about the conditions: that they refuse, that they say
why, and that a pipeline cannot slip past one by going through the back.
"""

import tempfile
import unittest
from pathlib import Path

from admet.core.service import Admet
from admet.workflows.operations import BY_ID, OPERATIONS, REQUIREMENTS, Refused, operation
from admet.workflows.pipelines import PIPELINES, pipeline, step


class DeclarationTests(unittest.TestCase):
    def test_every_operation_can_be_carried_out(self):
        for op in OPERATIONS:
            with self.subTest(operation=op.id):
                self.assertTrue(callable(op.run))

    def test_every_requirement_an_operation_names_is_one_that_exists(self):
        # A requirement nobody implements would silently never refuse anything.
        for op in OPERATIONS:
            for name in op.requires:
                with self.subTest(operation=op.id, requires=name):
                    self.assertIn(name, REQUIREMENTS)

    def test_a_protocol_operation_needs_hardware_corrections_and_an_idle_rig(self):
        for name in ("prime", "characterise", "gravimetry", "dropseq"):
            with self.subTest(operation=name):
                requires = set(operation(name).requires)
                self.assertIn("fluidics", requires)
                self.assertIn("corrections", requires)
                self.assertIn("idle", requires)

    def test_anything_that_writes_needs_a_project(self):
        for name in ("dropseq", "start_recording", "stop_recording"):
            with self.subTest(operation=name):
                self.assertIn("project", operation(name).requires)

    def test_recording_needs_both_instruments(self):
        # A recording is video and fluidics together; with no camera it would
        # quietly be half of one.
        self.assertEqual(
            set(operation("start_recording").requires), {"project", "fluidics", "camera"}
        )

    def test_an_unknown_operation_names_the_ones_there_are(self):
        with self.assertRaises(LookupError) as caught:
            operation("teleport")

        self.assertIn("prime", str(caught.exception))


class GuardTests(unittest.TestCase):
    COLD = {
        "project": False,
        "fluidics": False,
        "camera": False,
        "corrections": False,
        "running": False,
    }

    def test_a_guard_refuses_and_says_what_to_do(self):
        with self.assertRaises(Refused) as caught:
            operation("prime").check(self.COLD)

        self.assertIn("run connect first", str(caught.exception))

    def test_the_first_unmet_requirement_is_the_one_reported(self):
        with self.assertRaises(Refused) as caught:
            operation("dropseq").check(self.COLD)

        self.assertIn("no project is open", str(caught.exception))

    def test_a_met_requirement_passes(self):
        ready = {**self.COLD, "fluidics": True, "corrections": True}

        operation("prime").check(ready)

    def test_a_protocol_already_running_stops_another_starting(self):
        busy = {**self.COLD, "fluidics": True, "corrections": True, "running": True}

        with self.assertRaises(Refused) as caught:
            operation("prime").check(busy)

        self.assertIn("already running", str(caught.exception))

    def test_answering_a_step_needs_something_to_answer(self):
        with self.assertRaises(Refused):
            operation("confirm").check(self.COLD)


class PipelineDeclarationTests(unittest.TestCase):
    def test_every_stage_names_an_operation_that_exists(self):
        settings = {
            "simulated": True,
            "prime_oil_volume_ul": 1.0,
            "prime_aqueous_volume_ul": 1.0,
            "tick_s": 0.1,
        }
        for line in PIPELINES:
            bound = {param.name: settings.get(param.name, param.default) for param in line.params}
            for stage in line.stages(bound):
                with self.subTest(pipeline=line.id, stage=stage.operation):
                    self.assertIn(stage.operation, BY_ID)

    def test_a_pipeline_is_written_as_operations_in_order(self):
        stages = pipeline("setup").stages(
            {
                "simulated": True,
                "prime_oil_volume_ul": 40.0,
                "prime_aqueous_volume_ul": 5.0,
                "tick_s": 0.2,
            }
        )

        self.assertEqual(
            [stage.operation for stage in stages], ["connect", "apply_corrections", "prime"]
        )

    def test_a_stage_carries_the_settings_it_was_written_with(self):
        made = step("prime", prime_oil_volume_ul=12.0)

        self.assertEqual(made.settings["prime_oil_volume_ul"], 12.0)

    def test_an_unknown_pipeline_names_the_ones_there_are(self):
        with self.assertRaises(LookupError) as caught:
            pipeline("nonsense")

        self.assertIn("setup", str(caught.exception))


class PipelineRunTests(unittest.TestCase):
    def test_a_pipeline_stops_where_a_guard_refuses(self):
        # checks needs fluidics, which nothing has connected.
        report = Admet().run_pipeline("checks", {"tick_s": 0.1}, wait_s=5)

        self.assertFalse(report["completed"])
        self.assertEqual(report["stopped_at"], 0)
        self.assertEqual(report["stages"][0]["outcome"], "refused")
        self.assertIn("not connected", report["stages"][0]["reason"])

    def test_a_pipeline_stops_where_a_protocol_wants_the_operator(self):
        # Nothing is auto-confirmed: a confirmation exists because somebody has
        # to look at the rig.
        admet = Admet()

        report = admet.run_pipeline(
            "setup",
            {
                "simulated": True,
                "prime_oil_volume_ul": 2.0,
                "prime_aqueous_volume_ul": 1.0,
                "tick_s": 0.1,
            },
            wait_s=15,
        )

        self.assertFalse(report["completed"])
        self.assertEqual(report["reason"], "waiting")
        self.assertEqual(report["stages"][0]["outcome"], "completed")
        self.assertEqual(report["stages"][1]["outcome"], "completed")
        self.assertEqual(report["stages"][2]["operation"], "prime")
        admet.do("stop")
        admet.do("disconnect")

    def test_the_stages_before_a_stop_really_happened(self):
        admet = Admet()

        admet.run_pipeline(
            "setup",
            {"simulated": True, "prime_oil_volume_ul": 2.0, "tick_s": 0.1},
            wait_s=15,
        )

        state = admet.state()
        self.assertTrue(state["fluidics"])
        self.assertTrue(state["corrections"])
        admet.do("stop")
        admet.do("disconnect")

    def test_a_run_can_be_picked_up_from_a_later_stage(self):
        admet = Admet()
        admet.do("connect", {"simulated": True})
        admet.do("apply_corrections")

        report = admet.run_pipeline(
            "setup",
            {"simulated": True, "prime_oil_volume_ul": 2.0, "tick_s": 0.1},
            from_stage=2,
            wait_s=15,
        )

        self.assertEqual(report["stages"][0]["outcome"], "skipped")
        self.assertEqual(report["stages"][1]["outcome"], "skipped")
        self.assertEqual(report["stages"][2]["operation"], "prime")
        admet.do("stop")
        admet.do("disconnect")


class ProjectAwareTests(unittest.TestCase):
    def test_a_run_that_records_is_refused_without_a_session(self):
        admet = Admet()
        admet.do("connect", {"simulated": True})
        admet.do("apply_corrections")
        try:
            with self.assertRaises(Refused) as caught:
                admet.do("dropseq", {"tick_s": 0.1})
            self.assertIn("no project is open", str(caught.exception))
        finally:
            admet.do("disconnect")

    def test_with_a_project_open_the_same_run_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet()
            admet.create_project(Path(tmp) / "rig.admetp")
            admet.do("connect", {"simulated": True})
            admet.do("apply_corrections")
            try:
                admet.do("dropseq", {"set_count": 1, "replicate_count": 1, "tick_s": 0.1})
                self.assertTrue(admet.state()["running"])
            finally:
                admet.do("stop")
                admet.do("disconnect")


if __name__ == "__main__":
    unittest.main()
