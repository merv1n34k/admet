import unittest

from admet.core.api import AdmetAPI
from admet.core.engine import ActionSpec, EngineContext, EngineResult
from admet.core.schema import Param, ParamKind, ParamSchema, ResultSet
from admet.core.session import new_session


class CapturingEngine:
    id = "capture"
    name = "Capture Engine"
    settings = ParamSchema((Param("value", "Value", ParamKind.INTEGER, default=1),))
    actions = (ActionSpec("run", "Run", "diagnostics", params=("value",)),)

    def __init__(self):
        self.contexts = []

    def run_action(
        self,
        action: str,
        settings: dict,
        context: EngineContext | None = None,
    ) -> EngineResult:
        self.contexts.append(context)
        return EngineResult(ResultSet(metadata={"action": action, "settings": settings}))


class AdmetAPITests(unittest.TestCase):
    def test_describe_exposes_engine_contract(self):
        api = AdmetAPI(CapturingEngine(), session=new_session("project-1", "analysis"))

        description = api.describe()

        self.assertEqual(description["engine"]["id"], "capture")
        self.assertEqual(description["settings"], ["value"])
        self.assertEqual(description["actions"][0]["id"], "run")
        self.assertEqual(description["session"], "project-1")

    def test_run_action_injects_session_and_workdir(self):
        engine = CapturingEngine()
        api = AdmetAPI(engine, session=new_session("project-1", "analysis"), workdir="/tmp/work")

        api.run_action("run", {"value": 2}, EngineContext(metadata={"source": "test"}))

        context = engine.contexts[0]
        self.assertEqual(context.session.project_id, "project-1")
        self.assertEqual(context.workdir, "/tmp/work")
        self.assertEqual(context.metadata, {"source": "test"})


if __name__ == "__main__":
    unittest.main()
