"""The command line: discovery, starting the controller, and nothing else.

Running an experiment from the terminal was removed deliberately. It happens
through MCP or the Python binding, so these tests are about what the CLI no
longer offers as much as what it does.
"""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from admet.app import build_parser, main


def _run(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = main(argv)
    return exit_code, stdout.getvalue(), stderr.getvalue()


def _commands() -> dict:
    return build_parser()._subparsers._group_actions[0].choices  # type: ignore[attr-defined]


class SurfaceTests(unittest.TestCase):
    def test_the_command_line_is_discovery_and_the_controller(self):
        self.assertEqual(set(_commands()), {"describe", "serve"})

    def test_no_command_runs_an_experiment(self):
        # do, call, run, plan, operations and status are gone. One way to run
        # something is better than three that drift.
        commands = set(_commands())

        for gone in ("do", "call", "run", "plan", "operations", "pipelines", "status"):
            with self.subTest(command=gone):
                self.assertNotIn(gone, commands)

    def test_describe_shows_the_operations_and_the_engines(self):
        _exit_code, out, _err = _run(["describe"])

        described = json.loads(out)
        self.assertTrue(described["operations"])
        self.assertIn("acquisition", {engine["id"] for engine in described["engines"]})

    def test_describe_no_longer_offers_a_pipeline_layer(self):
        _exit_code, out, _err = _run(["describe"])

        self.assertNotIn("pipelines", json.loads(out))

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


class ServeTests(unittest.TestCase):
    """Real hardware is opt-in, and saying nothing is not a choice."""

    def test_serving_without_saying_which_hardware_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            with redirect_stderr(io.StringIO()):
                build_parser().parse_args(["serve"])

        self.assertEqual(caught.exception.code, 2)

    def test_simulated_and_live_cannot_both_be_asked_for(self):
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                build_parser().parse_args(["serve", "--simulated", "--live"])

    def test_either_one_on_its_own_is_accepted(self):
        simulated = build_parser().parse_args(["serve", "--simulated"])
        live = build_parser().parse_args(["serve", "--live"])

        self.assertTrue(simulated.simulated)
        self.assertFalse(simulated.live)
        self.assertTrue(live.live)
        self.assertFalse(live.simulated)

    def test_a_runtime_directory_can_be_named(self):
        args = build_parser().parse_args(["--runtime", "/tmp/rt", "serve", "--simulated"])

        self.assertEqual(args.runtime, "/tmp/rt")


if __name__ == "__main__":
    unittest.main()
