from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from admet.mcp.server import AdmetServer
from admet.workflows.json_protocol import normalize, resolve


def parameter_protocol():
    return {"name": "parameter_test", "parameters": {"oil_base_flow": ["Oil flow rate, µL/min", 5]},
            "steps": [{"sensor_setpoints": {"1": "oil_base_flow * 1.5"}, "trigger_type": "time",
                       "trigger_params": {"duration_s": "(oil_base_flow + 5) / 2"},
                       "timeout_s": "oil_base_flow + 10"}]}


def typed_parameter_protocol():
    document = parameter_protocol()
    document["parameters"].update({
        "oil": {"type": "text", "label": "Oil name", "default": "dSurf"},
        "filtered": {"type": "boolean", "label": "Filtered", "default": True},
        "finish": {"type": "choice", "label": "Completion", "default": "zero", "options": ["zero", "hold"]},
        "offset": {"type": "number", "label": "Offset", "default": -1, "min": -2, "max": 2},
    })
    document["steps"][0].update(name="{oil}, filtered={filtered}", on_complete="{finish}",
                                confirm_message="Review {oil}")
    return document


class ParameterProtocolTests(unittest.TestCase):
    def test_typed_parameters_resolve_without_changing_source_or_casting_booleans(self):
        document = typed_parameter_protocol()
        before = deepcopy(document)
        normalized = normalize(document)
        self.assertEqual(document, before)
        self.assertEqual(normalize(normalized), normalized)
        self.assertIs(normalized["parameter_values"]["filtered"], True)
        self.assertEqual(resolve(normalized)["steps"][0]["name"], "dSurf, filtered=true")
        normalized["parameter_values"].update(oil="custom {offset}", filtered=False, finish="hold")
        resolved = resolve(normalized)
        self.assertEqual(resolved["steps"][0]["name"], "custom {offset}, filtered=false")
        self.assertEqual(resolved["steps"][0]["on_complete"], "hold")
        for name in ("filtered", "oil", "finish"):
            invalid = deepcopy(normalized)
            invalid["steps"][0]["sensor_setpoints"]["1"] = name + " * 2"
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "not numeric"):
                normalize(invalid)

    def test_typed_parameter_schema_defaults_and_overrides_are_strict(self):
        for field, value in (("oil", 3), ("filtered", 1), ("filtered", "false"),
                             ("finish", "unknown"), ("offset", True), ("offset", 3)):
            document = typed_parameter_protocol()
            document["parameter_values"] = {field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                normalize(document)
        for declaration in (
            {"type": "text", "label": "Bad", "default": None},
            {"type": "number", "label": "Bad", "default": 0, "min": 2, "max": 1},
            {"type": "number", "label": "Bad", "default": float("inf")},
            {"type": "boolean", "label": "Bad", "default": "true"},
            {"type": "choice", "label": "Bad", "default": "x", "options": []},
            {"type": "choice", "label": "Bad", "default": "x", "options": ["x", "x"]},
            {"type": "choice", "label": "Bad", "default": "x", "options": ["x", {}]},
            {"type": "choice", "label": "Bad", "default": "x", "options": ["y"]},
            {"type": "text", "label": "Bad", "default": "x", "options": ["x"]},
            {"type": "choice", "label": "Bad", "default": 1, "options": [True]},
        ):
            document = typed_parameter_protocol()
            document["parameters"]["extra"] = declaration
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                normalize(document)

    def test_numeric_choices_and_label_substitution_keep_types(self):
        document = parameter_protocol()
        document["parameters"]["oil_base_flow"] = {
            "type": "choice", "label": "Flow", "default": 10.0, "options": [5, 10, 15],
        }
        normalized = normalize(document)
        self.assertIs(type(normalized["parameter_values"]["oil_base_flow"]), int)
        self.assertEqual(resolve(normalized)["steps"][0]["sensor_setpoints"]["1"], 15)

    def test_resolve_preserves_source_and_validates_overrides(self):
        document = parameter_protocol()
        before = deepcopy(document)
        normalized = normalize(document)
        self.assertEqual(document, before)
        self.assertEqual(normalize(normalized), normalized)
        self.assertEqual(normalized["steps"][0]["sensor_setpoints"]["1"], "oil_base_flow * 1.5")
        self.assertEqual(resolve(normalized)["steps"][0]["sensor_setpoints"]["1"], 7.5)
        normalized["parameter_values"]["oil_base_flow"] = 10
        self.assertEqual(resolve(normalized)["steps"][0]["sensor_setpoints"]["1"], 15)

    def test_unsafe_unknown_and_invalid_arithmetic_refused(self):
        for expression in ("__import__('os')", "oil_base_flow.real", "oil_base_flow[0]", "unknown * 2",
                           "1 / 0", "2 ** 100000", "True", "1e999", "oil_base_flow - 20", "[5]",
                           "5 if oil_base_flow else 3", "min(1, 2)", "* 5"):
            document = typed_parameter_protocol()
            document["steps"][0]["sensor_setpoints"]["1"] = expression
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                normalize(document)

    def test_invalid_parameter_schema_and_numeric_targets_refused(self):
        for declarations, values in (({"x": ["label", float("nan")]}, {}),
                                     ({"oil_base_flow": ["label", 5]}, {"unknown": 2}),
                                     ({"oil_base_flow": ["label", 5]}, {"oil_base_flow": -1}),
                                     ({"oil_base_flow": ["label", True]}, {}),
                                     ({"oil_base_flow": "5"}, {}),
                                     ({"bad.key": ["label", 5]}, {})):
            document = parameter_protocol()
            document.update(parameters=declarations, parameter_values=values)
            with self.subTest(declarations=declarations, values=values), self.assertRaises(ValueError):
                normalize(document)

    def test_save_reopen_plan_is_immutable_and_planning_has_no_engine_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = AdmetServer(simulated=True, project=f"{tmp}/p.admetp", create=True)
            document = typed_parameter_protocol()
            document["parameter_values"] = normalize(document)["parameter_values"]
            document["parameter_values"]["oil_base_flow"] = 10
            server.call("save_protocol", {"protocol": document})
            loaded = server.call("list_protocols", {"name": document["name"]})["protocol"]
            self.assertEqual(loaded["parameters"], document["parameters"])
            self.assertEqual(loaded["parameter_values"], document["parameter_values"])
            with patch.object(server.admet, "engine_action", side_effect=AssertionError("hardware call")):
                plan = server.call("plan_protocol", {
                    "operation_id": "run_json_protocol", "settings": {"protocol": loaded}})
            loaded["parameter_values"]["oil_base_flow"] = 100
            self.assertEqual(plan["steps"][0]["flow_setpoints_ul_min"], {"1": 15})
            self.assertEqual(plan["normalized_settings"]["protocol"]["parameter_values"]["oil_base_flow"], 10)
            self.assertIs(plan["normalized_settings"]["protocol"]["parameter_values"]["filtered"], True)
            self.assertEqual(plan["steps"][0]["name"], "dSurf, filtered=true")
