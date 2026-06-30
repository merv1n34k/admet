import unittest

from admet.core.engine import (
    ActionSpec,
    EngineContext,
    EngineRegistry,
    EngineResult,
    LazyEngineSpec,
    validate_action_settings,
)
from admet.core.schema import Param, ParamKind, ParamOption, ParamSchema, ResultRecord, ResultSet
from admet.core.workflow import Stage, StageControl, StageStatus, Workflow, WorkflowRunner
from admet.ui.analyze.renderer import _record_to_row, _single_record_field_rows
from admet.workflows import create_control_workflow


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
        with self.assertRaises(TypeError):
            Param("limit", "Limit", ParamKind.INTEGER, default=1).validate(True)
        with self.assertRaises(TypeError):
            Param("threshold", "Threshold", ParamKind.FLOAT, default=1.0).validate(False)


class ActionSpecTests(unittest.TestCase):
    def test_action_settings_validate_catalog_contract(self):
        schema = ParamSchema(
            (
                Param("required_path", "Required Path", ParamKind.PATH, default=None, required=True),
                Param("limit", "Limit", ParamKind.INTEGER, default=1, minimum=1),
                Param("enabled", "Enabled", ParamKind.BOOLEAN, default=True),
            )
        )
        actions = (ActionSpec("ping", "Ping", "diagnostics", params=("limit",)),)

        self.assertEqual(validate_action_settings(schema, actions, "ping", {"limit": 2}), {"limit": 2})
        with self.assertRaises(ValueError):
            validate_action_settings(schema, actions, "missing", {"limit": 1})
        with self.assertRaises(KeyError):
            validate_action_settings(schema, actions, "ping", {"limit": 1, "extra": True})
        with self.assertRaises(KeyError):
            validate_action_settings(schema, actions, "ping", {"limit": 1, "enabled": False})


class WorkflowTests(unittest.TestCase):
    def test_control_workflow_declares_runtime_contract(self):
        workflow = create_control_workflow()
        stage_ids = [stage.id for stage in workflow.stages]
        runs = next(stage for stage in workflow.stages if stage.id == "runs")
        actions = {control.action for control in runs.controls}
        corrections = next(stage for stage in workflow.stages if stage.id == "corrections")
        correction_names = {param.name for param in corrections.settings.params}

        self.assertEqual(stage_ids, ["scene", "fluigent", "corrections", "priming", "runs", "wash"])
        self.assertNotIn("start_recording", actions)
        self.assertNotIn("stop_recording", actions)
        self.assertNotIn("run_protocol", actions)
        expected_names = {
            f"{prefix}_{suffix}"
            for prefix in ("oil_l", "cells_m", "beads_m")
            for suffix in ("calibration", "scale", "offset", "quadratic")
        }
        self.assertEqual(correction_names, expected_names)

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
        runner = WorkflowRunner(workflow, LocalAnalysisEngine())

        state, result = runner.run_current(
            workflow.initial_state(),
            {"sample_id": "s1", "threshold": 2.0},
        )

        self.assertIsNotNone(result)
        self.assertEqual(state.statuses["analyze"], StageStatus.COMPLETE)
        self.assertEqual(state.data["analyze"].result_set.records[0].sample_id, "s1")

    def test_runner_refuses_paused_workflow(self):
        workflow = Workflow("demo", "Demo", (Stage("analyze", "Analyze", action="analyze"),))
        runner = WorkflowRunner(workflow, LocalAnalysisEngine())
        state = workflow.pause(workflow.initial_state())

        with self.assertRaises(RuntimeError):
            runner.run_current(state, {"sample_id": "s1", "threshold": 1.0})

    def test_runner_validates_stage_action_against_catalog(self):
        engine = PartialActionEngine()
        workflow = Workflow("demo", "Demo", (Stage("ping", "Ping", action="missing"),))
        runner = WorkflowRunner(workflow, engine)

        with self.assertRaises(ValueError):
            runner.run_current(workflow.initial_state(), {"limit": 2})

        self.assertEqual(engine.calls, [])

    def test_runner_passes_only_declared_action_settings(self):
        engine = PartialActionEngine()
        workflow = Workflow("demo", "Demo", (Stage("ping", "Ping", action="ping"),))
        runner = WorkflowRunner(workflow, engine)

        _, result = runner.run_current(workflow.initial_state(), {"limit": 2})

        self.assertEqual(engine.calls, [("ping", {"limit": 2})])
        self.assertEqual(result.result_set.metadata["settings"], {"limit": 2})


class EngineRegistryTests(unittest.TestCase):
    def test_lazy_unavailable_engine_is_reported(self):
        registry = EngineRegistry()
        registry.register_lazy(LazyEngineSpec("missing", "admet.nope"))

        self.assertEqual(registry.available_ids(), ())
        self.assertIn("missing", registry.unavailable())
        with self.assertRaises(LookupError):
            registry.create("missing")


class EngineContractTests(unittest.TestCase):
    def test_builtin_engines_declare_valid_action_catalogs(self):
        from admet.engines.analyze.cellpose.engine import create_engine as create_cellpose_engine
        from admet.engines.analyze.opencv.engine import create_engine as create_opencv_engine
        from admet.engines.control.engine import create_engine as create_control_engine

        engines = (
            create_opencv_engine(),
            create_cellpose_engine(),
            create_control_engine(),
        )
        for engine in engines:
            with self.subTest(engine=engine.id):
                self.assertTrue(engine.actions)
                setting_names = {param.name for param in engine.settings.params}
                action_ids = set()
                for action in engine.actions:
                    self.assertIsInstance(action, ActionSpec)
                    self.assertNotIn(action.id, action_ids)
                    self.assertLessEqual(set(action.params), setting_names)
                    action_ids.add(action.id)


class RendererHelperTests(unittest.TestCase):
    def test_record_rows_flatten_lists_and_dicts_for_nicegui_tables(self):
        row = _record_to_row(
            0,
            ResultRecord(
                sample_id="control",
                engine="fluidics",
                values={"cameras": [], "camera": {"pypylon_available": False}},
            ),
        )

        self.assertEqual(row["cameras"], "")
        self.assertEqual(row["camera"], '{"pypylon_available": false}')

    def test_single_record_rows_are_transposed_for_status_tables(self):
        rows = _single_record_field_rows(
            {
                "id": 0,
                "sample_id": "control",
                "engine": "fluidics",
                "action": "connect_camera",
                "camera_message": "No Basler cameras are currently enumerated; refresh after attaching one.",
            }
        )

        self.assertNotIn("Id", [row["field"] for row in rows])
        self.assertEqual(rows[0], {"field": "Sample Id", "value": "control"})
        self.assertEqual(rows[2], {"field": "Action", "value": "connect_camera"})
        self.assertEqual(rows[3]["field"], "Camera Message")


class PartialActionEngine:
    id = "partial"
    name = "Partial Action Engine"
    settings = ParamSchema(
        (
            Param("required_path", "Required Path", ParamKind.PATH, default=None, required=True),
            Param("limit", "Limit", ParamKind.INTEGER, default=1, minimum=1),
        )
    )
    actions = (ActionSpec("ping", "Ping", "diagnostics", params=("limit",)),)

    def __init__(self):
        self.calls = []

    def run_action(
        self,
        action: str,
        settings: dict,
        context: EngineContext | None = None,
    ) -> EngineResult:
        self.calls.append((action, dict(settings)))
        return EngineResult(ResultSet(metadata={"settings": dict(settings)}))


class LocalAnalysisEngine:
    id = "local"
    name = "Local Analysis Engine"
    settings = ParamSchema(
        (
            Param("sample_id", "Sample ID", ParamKind.TEXT, default="demo"),
            Param("threshold", "Threshold", ParamKind.FLOAT, default=1.0, minimum=0.0),
        )
    )
    actions = (ActionSpec("analyze", "Analyze", "analysis", params=("sample_id", "threshold")),)

    def run_action(
        self,
        action: str,
        settings: dict,
        context: EngineContext | None = None,
    ) -> EngineResult:
        normalized = validate_action_settings(self.settings, self.actions, action, settings)
        return EngineResult(
            ResultSet(
                records=(
                    ResultRecord(
                        sample_id=normalized["sample_id"],
                        engine=self.id,
                        values={"threshold": normalized["threshold"]},
                    ),
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
