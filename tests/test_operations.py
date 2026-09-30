"""Operations and the pipelines built from them.

An operation is a primitive plus the conditions under which using it is not a
mistake. These tests are about the conditions: that they refuse, that they say
why, and that a pipeline cannot slip past one by going through the back.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from admet.core.service import Admet
from admet.core.engine import KINDS, READ, START
from admet.workflows.operations import (
    ANALYZE,
    CONTROL,
    GENERAL,
    OPERATIONS,
    REQUIREMENTS,
    TARGETS,
    Refused,
    operation,
)


class DeclarationTests(unittest.TestCase):
    def test_retired_operations_are_absent_and_refused_without_actuation(self):
        admet = Admet()
        retired = {"run_characterisation", "run_gravimetry", "run_dropseq", "validate_oil_capacity"}
        listed = {entry["id"] for entry in admet.describe()["operations"]}
        self.assertFalse(retired & listed)
        self.assertTrue({"run_priming", "run_wash", "run_steps", "run_json_protocol",
                         "save_protocol", "list_protocols", "plan_protocol_file"} <= listed)
        with patch.object(admet, "engine_action", side_effect=AssertionError("actuated")), \
                patch.object(admet, "state", side_effect=AssertionError("read hardware")):
            for name in retired:
                for method in (admet.do, admet.plan_protocol, admet.describe):
                    with self.subTest(operation=name, method=method.__name__), self.assertRaises(LookupError):
                        method(name)

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
        for name in ("run_priming", "run_steps", "run_json_protocol"):
            with self.subTest(operation=name):
                requires = set(operation(name).requires)
                self.assertIn("fluidics", requires)
                self.assertIn("corrections", requires)
                self.assertIn("idle", requires)

    def test_anything_that_writes_needs_a_project(self):
        for name in ("run_json_protocol", "start_recording", "stop_recording"):
            with self.subTest(operation=name):
                self.assertIn("project", operation(name).requires)

    def test_recording_does_not_need_a_camera(self):
        # A fluidics-only run measures fluidics. Requiring a camera would make a
        # rig without one unable to record what it measured -- and a video that
        # was never written is not registered, so nothing claims a missing file.
        self.assertEqual(set(operation("start_recording").requires), {"project", "fluidics"})

    def test_an_unknown_operation_names_the_ones_there_are(self):
        with self.assertRaises(LookupError) as caught:
            operation("teleport")

        self.assertIn("run_priming", str(caught.exception))


class TargetTests(unittest.TestCase):
    """Which half of the system an operation belongs to."""

    def test_every_operation_says_which_half_it_belongs_to(self):
        for op in OPERATIONS:
            with self.subTest(operation=op.id):
                self.assertIn(op.target, TARGETS)

    def test_the_instrument_and_the_analysis_are_told_apart(self):
        self.assertEqual(operation("run_priming").target, CONTROL)
        self.assertEqual(operation("start_recording").target, CONTROL)
        self.assertEqual(operation("run_analysis").target, ANALYZE)
        self.assertEqual(operation("add_source").target, ANALYZE)

    def test_the_session_belongs_to_neither(self):
        # A project is where the instrument writes and where the analysis reads,
        # so opening one is not an instrument operation.
        for name in ("create_project", "open_project", "read_project", "list_projects"):
            with self.subTest(operation=name):
                self.assertEqual(operation(name).target, GENERAL)

    def test_nothing_general_needs_the_instrument(self):
        # If a general operation required fluidics it would not be general.
        for op in OPERATIONS:
            if op.target == GENERAL:
                with self.subTest(operation=op.id):
                    self.assertNotIn("fluidics", op.requires)
                    self.assertNotIn("camera", op.requires)

    def test_the_analysis_half_never_touches_the_instrument(self):
        for op in OPERATIONS:
            if op.target == ANALYZE:
                with self.subTest(operation=op.id):
                    self.assertEqual(op.uses, ())
                    self.assertFalse(op.starts_protocol)

    def test_one_half_can_be_asked_for_on_its_own(self):
        from admet.core.service import Admet

        listed = Admet().operations(ANALYZE)

        self.assertEqual({op["id"] for op in listed}, {"add_source", "list_sources", "run_analysis"})


class OperationContractTests(unittest.TestCase):
    def test_every_operation_says_what_calling_it_does(self):
        for op in OPERATIONS:
            with self.subTest(operation=op.id):
                self.assertIn(op.kind, KINDS)

    def test_the_operations_that_leave_something_running_are_the_protocols(self):
        # This is the distinction a caller cannot otherwise see: `start` returns
        # while the rig is still moving.
        starting = {op.id for op in OPERATIONS if op.kind == START}

        self.assertEqual(starting, {op.id for op in OPERATIONS if op.starts_protocol})

    def test_a_read_never_changes_anything(self):
        for op in OPERATIONS:
            if op.kind == READ:
                with self.subTest(operation=op.id):
                    self.assertFalse(op.starts_protocol)

    def test_every_engine_action_says_what_calling_it_does(self):
        admet = Admet()
        for engine_id in admet.engine_ids():
            for action in admet.engine(engine_id).actions:
                with self.subTest(engine=engine_id, action=action.id):
                    self.assertIn(action.kind, KINDS)


class StepTests(unittest.TestCase):
    """The middle level: a protocol the caller wrote, not one this build ships."""

    def test_the_step_level_is_an_operation_like_any_other(self):
        op = operation("run_steps")

        self.assertEqual(op.kind, START)
        self.assertEqual(set(op.requires), {"fluidics", "corrections", "idle", "safe"})
        self.assertIn("steps", op.raw)

    def test_describe_says_what_each_trigger_takes(self):
        # Otherwise the only way to learn it is to be refused.
        described = Admet().describe("run_steps")

        trigger = described["structured_params"]["steps"]["items"]["properties"]
        self.assertIn("target_volume_ul", trigger["trigger_params"]["description"])
        self.assertIn("duration_s", trigger["trigger_params"]["description"])

    def test_the_declared_triggers_are_the_ones_that_exist(self):
        from admet.engines.acquisition.triggers import TRIGGER_TYPES, create_trigger

        declared = operation("run_steps").raw["steps"]["items"]["properties"]

        self.assertEqual(tuple(declared["trigger_type"]["enum"]), TRIGGER_TYPES)
        for name in TRIGGER_TYPES:
            with self.subTest(trigger=name):
                with self.assertRaises(TypeError):  # exists, but needs its settings
                    create_trigger(name, {})

    def test_a_trigger_setting_that_does_not_exist_is_refused_by_name(self):
        admet = Admet()
        admet.do("connect_fluidics", {"simulated": True})
        admet.do("apply_corrections")
        try:
            with self.assertRaises(Refused) as caught:
                admet.do("run_steps", {"steps": [
                    {"name": "x", "trigger_type": "volume",
                     "trigger_params": {"volume_ul": 2.0}}]})
            self.assertIn("it takes: sensor_index, target_volume_ul", str(caught.exception))
        finally:
            admet.do("disconnect_fluidics")

    def test_a_trigger_missing_what_it_needs_is_refused(self):
        admet = Admet()
        admet.do("connect_fluidics", {"simulated": True})
        admet.do("apply_corrections")
        try:
            with self.assertRaises(Refused) as caught:
                admet.do("run_steps", {"steps": [{"name": "x", "trigger_type": "time"}]})
            self.assertIn("needs duration_s", str(caught.exception))
        finally:
            admet.do("disconnect_fluidics")

    def test_steps_the_caller_wrote_really_run(self):
        admet = Admet()
        admet.do("connect_fluidics", {"simulated": True})
        admet.do("apply_corrections")
        try:
            report = admet.do("run_steps", {"steps": [
                {"name": "wet the line", "sensor_setpoints": {"0": 5.0},
                 "trigger_type": "volume",
                 "trigger_params": {"sensor_index": 0, "target_volume_ul": 2.0}},
                {"name": "settle", "sensor_setpoints": {"0": 2.0},
                 "trigger_type": "time", "trigger_params": {"duration_s": 1.0},
                 "on_complete": "zero"},
            ], "tick_s": 0.1})

            self.assertEqual(report["steps"], 2)
            self.assertEqual(report["protocol"], "custom")
            self.assertTrue(admet.state()["running"])
        finally:
            admet.do("stop_protocol")
            admet.do("disconnect_fluidics")

    def test_the_step_level_is_guarded_like_the_rest(self):
        with self.assertRaises(Refused) as caught:
            Admet().do("run_steps", {"steps": [{"name": "x", "trigger_type": "time"}]})

        self.assertIn("not connected", str(caught.exception))


class GuardTests(unittest.TestCase):
    COLD = {
        "project": False,
        "fluidics": False,
        "camera": False,
        "corrections": False,
        "running": False,
        "tripped": False,
    }

    def test_a_guard_refuses_and_says_what_to_do(self):
        with self.assertRaises(Refused) as caught:
            operation("run_priming").check(self.COLD)

        self.assertIn("run connect_fluidics first", str(caught.exception))

    def test_the_first_unmet_requirement_is_the_one_reported(self):
        with self.assertRaises(Refused) as caught:
            operation("run_json_protocol").check(self.COLD)

        self.assertIn("no project is open", str(caught.exception))

    def test_a_met_requirement_passes(self):
        ready = {**self.COLD, "fluidics": True, "corrections": True}

        operation("run_priming").check(ready)

    def test_a_protocol_already_running_stops_another_starting(self):
        busy = {**self.COLD, "fluidics": True, "corrections": True, "running": True}

        with self.assertRaises(Refused) as caught:
            operation("run_priming").check(busy)

        self.assertIn("already running", str(caught.exception))

    def test_answering_a_step_needs_something_to_answer(self):
        with self.assertRaises(Refused):
            operation("confirm_protocol").check(self.COLD)




class ProjectAwareTests(unittest.TestCase):
    def test_a_run_that_records_is_refused_without_a_session(self):
        admet = Admet()
        admet.do("connect_fluidics", {"simulated": True})
        admet.do("apply_corrections")
        try:
            with self.assertRaises(Refused) as caught:
                admet.do("run_json_protocol", {"tick_s": 0.1})
            self.assertIn("no project is open", str(caught.exception))
        finally:
            admet.do("disconnect_fluidics")

    def test_with_a_project_open_the_same_run_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            admet = Admet()
            admet.create_project(Path(tmp) / "rig.admetp")
            admet.do("connect_fluidics", {"simulated": True})
            admet.do("apply_corrections")
            try:
                from admet.workflows.json_protocol import template_documents

                admet.do("run_json_protocol", {"protocol": template_documents()["dropseq"], "tick_s": 0.1})
                self.assertTrue(admet.state()["running"])
            finally:
                admet.do("stop_protocol")
                admet.do("disconnect_fluidics")


if __name__ == "__main__":
    unittest.main()


class DescribeTests(unittest.TestCase):
    """Reading the layers against each other."""

    def test_every_engine_action_an_operation_claims_to_drive_exists(self):
        # The link between the layers is declared, so it can go stale. This is
        # what catches it.
        admet = Admet()
        actions = {action.id for action in admet.engine("acquisition").actions}

        for op in OPERATIONS:
            for name in op.uses:
                with self.subTest(operation=op.id, action=name):
                    self.assertIn(name, actions)

    def test_every_protocol_an_operation_claims_to_run_exists(self):
        from admet.workflows.protocols import PROTOCOLS

        for op in OPERATIONS:
            if op.protocol:
                with self.subTest(operation=op.id):
                    self.assertIn(op.protocol, PROTOCOLS)

    def test_an_operation_that_starts_a_shipped_protocol_names_which(self):
        builds_its_own = {"run_steps", "run_json_protocol"}
        for op in OPERATIONS:
            if op.id in builds_its_own:
                continue
            with self.subTest(operation=op.id):
                self.assertEqual(bool(op.protocol), op.starts_protocol)

    def test_describing_nothing_shows_the_three_layers(self):
        described = Admet().describe()

        self.assertEqual(len(described["operations"]), len(OPERATIONS))
        self.assertIn("acquisition", {engine["id"] for engine in described["engines"]})

    def test_describing_an_operation_says_why_each_guard_is_there(self):
        described = Admet().describe("run_priming")

        self.assertEqual(described["layer"], "operation")
        self.assertEqual(described["target"], "control")
        self.assertEqual(described["protocol"], "Priming")
        reasons = {entry["name"]: entry["why"] for entry in described["requires"]}
        self.assertIn("run connect_fluidics first", reasons["fluidics"])


    def test_describing_an_engine_says_which_operations_drive_each_action(self):
        described = Admet().describe("acquisition")

        actions = {action["id"]: action for action in described["actions"]}
        self.assertEqual(described["layer"], "engine")
        self.assertEqual(actions["connect_fluidics"]["used_by"], ["connect_fluidics"])

    def test_describing_something_that_is_not_there_says_what_is(self):
        with self.assertRaises(LookupError) as caught:
            Admet().describe("nonsense")

        self.assertIn("run_priming", str(caught.exception))
