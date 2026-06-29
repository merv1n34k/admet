import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from admet.app import build_parser, main


class AppCliTests(unittest.TestCase):
    def test_ui_targets_do_not_parse_engine_selection(self):
        parser = build_parser()

        args = parser.parse_args(["analyze"])

        self.assertEqual(args.target, "analyze")
        self.assertIsNone(args.engine)

    def test_engine_flag_is_headless_only(self):
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            main(["analyze", "--engine", "opencv"])

        self.assertEqual(caught.exception.code, 2)

    def test_headless_engine_mode_describes_api_contract(self):
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            main(["--engine", "opencv"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["engine"]["id"], "opencv")
        self.assertIn("analyze", [action["id"] for action in payload["actions"]])
        self.assertIn("video_path", payload["settings"])

    def test_control_target_launches_pyside_target(self):
        with patch("admet.app.run_control_ui") as run_control:
            main(["control"])

        run_control.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
