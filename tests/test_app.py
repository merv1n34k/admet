"""The command line, which is the same surface a program drives over MCP."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from admet.app import build_parser, main


class DescribeTests(unittest.TestCase):
    def test_an_engine_says_what_it_can_do(self):
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = main(["describe", "opencv"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["engine"]["id"], "opencv")
        self.assertIn("analyze", [action["id"] for action in payload["actions"]])
        self.assertIn("video_path", payload["settings"])

    def test_the_acquisition_engine_describes_its_actions(self):
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            main(["describe", "acquisition"])

        actions = {action["id"] for action in json.loads(stdout.getvalue())["actions"]}
        self.assertIn("connect_fluidics", actions)
        self.assertIn("run_protocol", actions)

    def test_an_engine_nobody_has_is_refused_by_the_parser(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["describe", "nonsense"])


class RunTests(unittest.TestCase):
    def test_a_malformed_setting_is_refused_before_anything_runs(self):
        with patch("admet.app.create_engine_api") as create:
            exit_code = main(["run", "acquisition", "connect_fluidics", "--set", "simulated"])

        self.assertEqual(exit_code, 2)
        create.return_value.run.assert_not_called()

    def test_a_simulated_connection_reports_what_it_connected_to(self):
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = main(["run", "acquisition", "connect_fluidics", "--set", "simulated=true"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "complete")
        self.assertTrue(payload["metadata"]["connected"])
        self.assertTrue(payload["metadata"]["simulated"])

    def test_an_action_that_raises_reports_the_reason_and_fails(self):
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            exit_code = main(["run", "acquisition", "no_such_action"])

        self.assertEqual(exit_code, 1)
        self.assertIn("message", json.loads(stderr.getvalue()))

    def test_json_settings_carry_values_a_shell_would_mangle(self):
        args = build_parser().parse_args(
            [
                "run",
                "acquisition",
                "run_protocol",
                "--settings-json",
                '{"pipeline_name": "Wash", "tick_s": 0.2}',
            ]
        )

        self.assertEqual(json.loads(args.settings_json)["pipeline_name"], "Wash")


class ScalarTests(unittest.TestCase):
    def test_a_command_line_value_becomes_what_it_looks_like(self):
        from admet.app import _scalar

        self.assertIs(_scalar("true"), True)
        self.assertIs(_scalar("FALSE"), False)
        self.assertIsNone(_scalar("null"))
        self.assertEqual(_scalar("42"), 42)
        self.assertEqual(_scalar("2.5"), 2.5)
        self.assertEqual(_scalar("Wash"), "Wash")

    def test_nothing_cleverer_happens_to_a_string(self):
        # A value that merely contains digits stays the text it was.
        from admet.app import _scalar

        self.assertEqual(_scalar("set01_rep02"), "set01_rep02")
        self.assertEqual(_scalar("1,2,3"), "1,2,3")


if __name__ == "__main__":
    unittest.main()
