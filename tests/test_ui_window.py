from __future__ import annotations

import unittest

from admet.ui.window import log_state, settings_panel_state
from admet.workflows import SettingsSpec, Stage


class WindowContractTests(unittest.TestCase):
    def test_settings_panel_state_extracts_kind_source_and_stage(self) -> None:
        state = settings_panel_state(
            Stage(
                "video",
                "Video",
                settings_panel=SettingsSpec(kind="matrix", options={"source": "opencv_targets"}),
            )
        )

        self.assertEqual(state.kind, "matrix")
        self.assertEqual(state.source, "opencv_targets")
        self.assertEqual(state.stage_id, "video")

    def test_settings_panel_state_defaults_missing_panel_to_params(self) -> None:
        state = settings_panel_state(Stage("view", "View", settings_panel=None))

        self.assertEqual(state.kind, "params")
        self.assertEqual(state.stage_id, "view")

    def test_settings_panel_state_preserves_explicit_none_panel(self) -> None:
        state = settings_panel_state(Stage("view", "View", settings_panel=SettingsSpec(kind="none")))

        self.assertEqual(state.kind, "none")
        self.assertEqual(state.stage_id, "view")

    def test_log_state_limits_lines_and_keeps_empty_text(self) -> None:
        state = log_state(["one", "two", "three"], limit=2, empty_text="Empty")

        self.assertEqual(state.lines, ("two", "three"))
        self.assertEqual(state.empty_text, "Empty")


if __name__ == "__main__":
    unittest.main()
