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


class ThemeAssetTests(unittest.TestCase):
    def test_the_stylesheet_points_at_chevrons_that_exist(self):
        # Qt draws nothing where a styled combo box's arrow should be unless it is
        # given an image, so a missing asset is an invisible dropdown.
        from admet.ui.theme import ASSETS, asset

        for name in (
            "chevron-down.svg",
            "chevron-down-strong.svg",
            "chevron-down-muted.svg",
            "chevron-up.svg",
            "chevron-up-strong.svg",
            "chevron-up-muted.svg",
        ):
            with self.subTest(name=name):
                self.assertTrue((ASSETS / name).is_file())
                self.assertEqual(asset(name), (ASSETS / name).as_posix())

    def test_the_chevrons_are_drawn_in_the_palette_they_sit_in(self):
        # The colour is baked into the file, so it has to be checked against the
        # palette rather than trusted to stay in step with it.
        from admet.ui.theme import ASSETS, Theme

        expected = {
            "chevron-down.svg": Theme.TEXT_MUTED,
            "chevron-down-strong.svg": Theme.TEXT_WHITE,
            "chevron-down-muted.svg": Theme.TEXT_DISABLED,
            "chevron-up.svg": Theme.TEXT_MUTED,
            "chevron-up-strong.svg": Theme.TEXT_WHITE,
            "chevron-up-muted.svg": Theme.TEXT_DISABLED,
        }
        for name, colour in expected.items():
            with self.subTest(name=name):
                self.assertIn(f'stroke="{colour}"', (ASSETS / name).read_text(encoding="utf-8"))


class CloseWarningTests(unittest.TestCase):
    def test_nothing_connected_is_nothing_to_warn_about(self):
        from admet.ui.window import connected_devices

        self.assertEqual(connected_devices(), ())

    def test_each_live_thing_is_named(self):
        from admet.ui.window import connected_devices

        devices = connected_devices(
            camera=True, camera_live=True, fluidics=True, recording=True, protocol="runs"
        )

        self.assertEqual(
            devices,
            (
                "Camera (live preview)",
                "Fluigent pressure controller",
                "Recording in progress",
                "Protocol running (runs)",
            ),
        )

    def test_a_connected_camera_that_is_not_previewing_says_so(self):
        from admet.ui.window import connected_devices

        self.assertEqual(connected_devices(camera=True), ("Camera",))
        self.assertEqual(connected_devices(camera=True, camera_live=True), ("Camera (live preview)",))

    def test_a_camera_that_is_off_is_not_listed_by_its_preview_flag(self):
        # camera_live only qualifies a camera that is connected in the first place.
        from admet.ui.window import connected_devices

        self.assertEqual(connected_devices(camera=False, camera_live=True), ())
