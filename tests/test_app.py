"""The command line: the workflow layer, and nothing below it."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from admet.app import _scalar, build_parser, main


def _run(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(argv)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class SurfaceTests(unittest.TestCase):
    def test_operations_are_listed_with_what_they_need(self):
        exit_code, out, _err = _run(["operations"])

        operations = {op["id"]: op for op in json.loads(out)}
        self.assertEqual(exit_code, 0)
        self.assertEqual(operations["prime"]["requires"], ["fluidics", "corrections", "idle"])
        self.assertIn("prime_oil_volume_ul", [p["name"] for p in operations["prime"]["params"]])

    def test_pipelines_are_listed(self):
        _exit_code, out, _err = _run(["pipelines"])

        self.assertIn("setup", {line["id"] for line in json.loads(out)})

    def test_there_is_no_command_for_engine_actions(self):
        # They stay reachable from Python; the command line is the workflow layer.
        commands = build_parser()._subparsers._group_actions[0].choices  # type: ignore[attr-defined]

        self.assertNotIn("describe", commands)
        self.assertIn("do", commands)
        self.assertIn("run", commands)

    def test_a_plan_shows_the_stages_without_running_them(self):
        exit_code, out, _err = _run(["plan", "setup"])

        stages = json.loads(out)
        self.assertEqual(exit_code, 0)
        self.assertEqual([stage["operation"] for stage in stages], ["connect", "apply_corrections", "prime"])

    def test_status_reports_the_session_and_the_instrument(self):
        _exit_code, out, _err = _run(["status"])

        payload = json.loads(out)
        self.assertIn("fluidics", payload)
        self.assertFalse(payload["session"]["open"])


class GuardTests(unittest.TestCase):
    def test_an_operation_out_of_order_fails_with_the_reason(self):
        exit_code, _out, err = _run(["do", "prime"])

        self.assertEqual(exit_code, 1)
        self.assertIn("the fluidics are not connected", json.loads(err)["message"])

    def test_an_unknown_operation_names_what_there_is(self):
        exit_code, _out, err = _run(["do", "nonsense"])

        self.assertEqual(exit_code, 1)
        self.assertIn("unknown operation", json.loads(err)["message"])

    def test_a_connection_works_and_can_be_released(self):
        exit_code, out, _err = _run(["do", "connect", "--set", "simulated=true"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(out)["connected"])
        _run(["do", "disconnect"])


class ProjectTests(unittest.TestCase):
    def test_a_project_can_be_created_for_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, out, _err = _run(
                ["--project", f"{tmp}/rig.admetp", "--create-project", "status"]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(json.loads(out)["session"]["open"])


class ScalarTests(unittest.TestCase):
    def test_a_command_line_value_becomes_what_it_looks_like(self):
        self.assertIs(_scalar("true"), True)
        self.assertIs(_scalar("FALSE"), False)
        self.assertIsNone(_scalar("null"))
        self.assertEqual(_scalar("42"), 42)
        self.assertEqual(_scalar("2.5"), 2.5)
        self.assertEqual(_scalar("Wash"), "Wash")

    def test_nothing_cleverer_happens_to_a_string(self):
        self.assertEqual(_scalar("set01_rep02"), "set01_rep02")
        self.assertEqual(_scalar("1,2,3"), "1,2,3")


if __name__ == "__main__":
    unittest.main()
