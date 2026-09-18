import unittest

from admet.core.engine import (
    ActionSpec,
    EngineRegistry,
    LazyEngineSpec,
    Param,
    ParamKind,
    ParamOption,
    ParamSchema,
    validate_action_settings,
)
from admet.core.run import RunJob, RunResult


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
        from admet.engines.acquisition import create_engine as create_acquisition_engine
        from admet.engines.cellpose import create_engine as create_cellpose_engine
        from admet.engines.opencv import create_engine as create_opencv_engine

        engines = (
            create_opencv_engine(),
            create_cellpose_engine(),
            create_acquisition_engine(),
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

    def run(self, job: RunJob) -> RunResult:
        normalized = validate_action_settings(self.settings, self.actions, job.action, job.settings)
        self.calls.append((job.action, dict(normalized)))
        return RunResult(
            job_id=job.id,
            engine=self.id,
            action=job.action,
            metadata={"settings": dict(normalized)},
        )


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

    def run(self, job: RunJob) -> RunResult:
        normalized = validate_action_settings(self.settings, self.actions, job.action, job.settings)
        return RunResult(
            job_id=job.id,
            engine=self.id,
            action=job.action,
            metadata={
                "sample_id": normalized["sample_id"],
                "threshold": normalized["threshold"],
            },
        )


if __name__ == "__main__":
    unittest.main()
