from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from admet.ui.backend import DesktopBackend
from admet.workflows.json_protocol import normalize
from tests.test_qt_backend import definition


def measured_protocol():
    document = definition(0.05)
    document["measurements"] = {
        "before_mg": {"label": "Vessel before (mg)", "step": 1},
        "after_mg": {"label": "Vessel after (mg)", "step": 1},
        "density": {"label": "Oil density (g/mL)"},
    }
    return document


class RunMeasurementsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = DesktopBackend(simulated=True)
        self.addCleanup(self.backend.shutdown)
        self.backend.create_project(Path(self.tmp.name) / "measurements.admetp")

    def test_schema_rejects_values_defaults_and_invalid_step_bindings(self):
        for bad in ([], {"bad.key": {"label": "Mass"}}, {"mass": {"label": "Mass", "default": 0}},
                    {"mass": {"label": "Mass", "step": 0}}, {"mass": {"label": "Mass", "step": 2}},
                    {"mass": {"label": "Mass", "step": True}}):
            doc = measured_protocol()
            doc["measurements"] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize(doc)
        self.assertEqual(normalize(measured_protocol())["measurements"], measured_protocol()["measurements"])

    def test_historical_plan_named_run_retains_null_measurements(self):
        store = self.backend.service.protocol_store()
        plan_id = "plan_" + "a" * 32
        plan = {"plan_id": plan_id, "normalized_settings": {"protocol": measured_protocol()}}
        directory = store.begin(plan)
        self.assertEqual(directory.name, plan_id)
        values = store.measurements(plan_id)
        self.assertEqual(values["run_id"], plan_id)
        self.assertTrue(all(value is None for value in values["values"].values()))
        self.assertEqual(store.measurements(plan_id, {"before_mg": 1})["values"]["before_mg"], 1)
        with self.assertRaisesRegex(ValueError, "invalid run ID"):
            store.measurements("plan_../../elsewhere")

    def test_execution_initializes_null_values_and_rerun_is_independent(self):
        self.backend.call("connect_fluidics")
        self.backend.call("apply_corrections")
        ids = []
        for _ in range(2):
            plan = self.backend.preview("experiment_1", measured_protocol())
            self.assertNotIn("run_id", plan)
            result = self.backend.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"],
                                                            "timeout_s": 1})
            run_id = result["run_id"]
            ids.append(run_id)
            data = self.backend.measurements(run_id)
            self.assertTrue(all(v is None for v in data["values"].values()))
            before = deepcopy(self.backend.service.planned_protocols())
            with patch.object(self.backend.engine, "run", side_effect=AssertionError("measurement actuated")):
                saved = self.backend.measurements(run_id, {"before_mg": 150.25})
                self.assertEqual(saved["revision"], 1)
                self.assertIsNone(saved["values"]["after_mg"])
            self.assertEqual(self.backend.service.planned_protocols(), before)
            self.backend.call("control_protocol", {"action": "abort", "timeout_s": 1})
        self.assertNotEqual(*ids)
        self.assertEqual(self.backend.measurements(ids[0])["values"]["before_mg"], 150.25)
        self.assertEqual(len(self.backend.measurement_runs()), 2)
        self.backend.shutdown()
        self.backend.open_project(self.backend.workdir)
        self.assertEqual(self.backend.measurements(ids[0])["values"]["before_mg"], 150.25)
        for changes in ({"unknown": 1}, {"density": True}, {"density": float("nan")}, {"density": "1.6"}):
            with self.assertRaises(ValueError):
                self.backend.measurements(ids[0], changes)
        self.backend.measurements(ids[0], {"before_mg": None})
        self.assertIsNone(self.backend.measurements(ids[0])["values"]["before_mg"])
        with self.assertRaises(ValueError):
            self.backend.measurements("../../other")
