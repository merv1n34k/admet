import unittest

from admet.core.engine import EngineRegistry, LazyEngineSpec
from admet.core.schema import Param, ParamKind, ParamOption, ParamSchema
from admet.core.workflow import Stage, StageControl, StageStatus, Workflow, WorkflowRunner
from admet.engines.dummy import create_engine


class ParamSchemaTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        schema = ParamSchema(
            (
                Param("enabled", "Enabled", ParamKind.BOOLEAN, default=True),
                Param("threshold", "Threshold", ParamKind.FLOAT, default=1.0, minimum=0.0),
                Param(
                    "mode",
                    "Mode",
                    ParamKind.CHOICE,
                    default="a",
                    options=(ParamOption("a", "A"), ParamOption("b", "B")),
                ),
            )
        )

        self.assertEqual(
            schema.validate({"threshold": 2}),
            {"enabled": True, "threshold": 2, "mode": "a"},
        )

        with self.assertRaises(ValueError):
            schema.validate({"threshold": -1})
        with self.assertRaises(ValueError):
            schema.validate({"mode": "c"})


class WorkflowTests(unittest.TestCase):
    def test_advance_skip_and_rewind(self):
        workflow = Workflow(
            "demo",
            "Demo",
            (
                Stage("one", "One"),
                Stage("two", "Two", skippable=True),
                Stage("three", "Three"),
            ),
        )
        state = workflow.initial_state()
        self.assertEqual(workflow.current_stage(state).id, "one")
        self.assertEqual(state.statuses["one"], StageStatus.ACTIVE)

        state = workflow.complete_current(state)
        self.assertEqual(workflow.current_stage(state).id, "two")
        self.assertEqual(state.statuses["one"], StageStatus.COMPLETE)

        state = workflow.skip_current(state)
        self.assertEqual(workflow.current_stage(state).id, "three")
        self.assertEqual(state.statuses["two"], StageStatus.SKIPPED)

        state = workflow.rewind(state)
        self.assertEqual(workflow.current_stage(state).id, "two")
        self.assertEqual(state.statuses["two"], StageStatus.ACTIVE)

    def test_requires_unique_stage_ids(self):
        with self.assertRaises(ValueError):
            Workflow("bad", "Bad", (Stage("x", "X"), Stage("x", "X again")))

    def test_stage_metadata_supports_settings_and_controls(self):
        stage = Stage(
            "scene",
            "Scene",
            description="Camera setup",
            settings=ParamSchema((Param("camera_index", "Camera", ParamKind.INTEGER, default=0),)),
            controls=(StageControl("Refresh Cameras", "refresh_cameras"),),
        )

        self.assertEqual(stage.settings.defaults()["camera_index"], 0)
        self.assertEqual(stage.controls[0].action, "refresh_cameras")

    def test_confirmation_gate_blocks_unconfirmed_completion(self):
        workflow = Workflow("gated", "Gated", (Stage("run", "Run", confirmation_required=True),))
        state = workflow.initial_state()

        with self.assertRaises(PermissionError):
            workflow.complete_current(state)

        state = workflow.complete_current(state, confirmed=True)
        self.assertEqual(state.statuses["run"], StageStatus.COMPLETE)


class WorkflowRunnerTests(unittest.TestCase):
    def test_runner_executes_stage_action_and_stores_result(self):
        workflow = Workflow("demo", "Demo", (Stage("analyze", "Analyze", action="analyze"),))
        runner = WorkflowRunner(workflow, create_engine())

        state, result = runner.run_current(
            workflow.initial_state(),
            {"sample_id": "s1", "threshold": 2.0},
        )

        self.assertIsNotNone(result)
        self.assertEqual(state.statuses["analyze"], StageStatus.COMPLETE)
        self.assertEqual(state.data["analyze"].result_set.records[0].sample_id, "s1")

    def test_runner_refuses_paused_workflow(self):
        workflow = Workflow("demo", "Demo", (Stage("analyze", "Analyze", action="analyze"),))
        runner = WorkflowRunner(workflow, create_engine())
        state = workflow.pause(workflow.initial_state())

        with self.assertRaises(RuntimeError):
            runner.run_current(state, {"sample_id": "s1", "threshold": 1.0})


class EngineRegistryTests(unittest.TestCase):
    def test_lazy_unavailable_engine_is_reported(self):
        registry = EngineRegistry()
        registry.register_lazy(LazyEngineSpec("missing", "admet.nope"))

        self.assertEqual(registry.available_ids(), ())
        self.assertIn("missing", registry.unavailable())
        with self.assertRaises(LookupError):
            registry.create("missing")


if __name__ == "__main__":
    unittest.main()
