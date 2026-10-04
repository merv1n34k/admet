"""The command line exposes only desktop launch and API discovery."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from admet.app import build_parser, main


def _run(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(argv)
    return exit_code, stdout.getvalue(), stderr.getvalue()


def _commands() -> dict:
    return build_parser()._subparsers._group_actions[0].choices  # type: ignore[attr-defined]


class SurfaceTests(unittest.TestCase):
    def test_the_command_line_is_describe_and_control(self):
        self.assertEqual(set(_commands()), {"control", "describe"})

    def test_control_launches_desktop_and_forwards_only_project(self):
        for args, forwarded in (
            (["control"], []),
            (["control", "--project", "test.admetp"], ["--project", "test.admetp"]),
        ):
            with self.subTest(args=args), patch("admet.ui.app.main", return_value=0) as desktop:
                self.assertEqual(main(args), 0)
                desktop.assert_called_once_with(forwarded)

    def test_control_help_does_not_launch_desktop(self):
        with patch("admet.ui.app.main", side_effect=AssertionError("desktop launched")):
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(["control", "--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_control_rejects_server_options(self):
        for option in (["--runtime", "runtime"], ["--create-project"]):
            with self.subTest(option=option), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main([*option, "control"])
                self.assertEqual(caught.exception.code, 2)

    def test_describe_shows_the_operations_and_the_engines(self):
        _exit_code, out, _err = _run(["describe"])

        described = json.loads(out)
        self.assertTrue(described["operations"])
        self.assertIn("acquisition", {engine["id"] for engine in described["engines"]})

    def test_describe_reads_one_engine_without_running_anything(self):
        exit_code, out, _err = _run(["describe", "acquisition"])

        described = json.loads(out)
        self.assertEqual(exit_code, 0)
        self.assertEqual(described["layer"], "engine")
        self.assertIn("connect_fluidics", described["actions"][0]["used_by"])

    def test_describe_reads_one_operation(self):
        exit_code, out, _err = _run(["describe", "run_priming"])

        described = json.loads(out)
        self.assertEqual(exit_code, 0)
        self.assertEqual(described["kind"], "start")
        self.assertIn("fluidics", [entry["name"] for entry in described["requires"]])

    def test_describing_something_that_is_not_there_names_what_is(self):
        exit_code, _out, err = _run(["describe", "nonsense"])

        self.assertEqual(exit_code, 1)
        self.assertIn("run_priming", json.loads(err)["message"])


if __name__ == "__main__":
    unittest.main()
