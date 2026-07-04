import unittest

from admet.ui.theme import box_padding, button_qss, spacing, stylesheet, text_qss
from admet.ui.presenter import build_screen
from admet.workflows.control import create_control_workflow


class ControlThemeTests(unittest.TestCase):
    def test_theme_helpers_are_local_and_pure(self):
        self.assertEqual(spacing("control"), 4)
        self.assertEqual(box_padding("none"), (0, 0, 0, 0))
        self.assertIn("font-weight: 600", text_qss("primary", bold=True))
        self.assertIn("QPushButton", button_qss("danger"))
        self.assertIn("QMainWindow", stylesheet())


class ControlWorkflowPresenterTests(unittest.TestCase):
    def test_scene_surface_commands_come_from_workflow(self):
        workflow = create_control_workflow()
        screen = build_screen(workflow, workflow.initial_state())

        self.assertEqual(screen.workflow_id, "control")
        self.assertEqual(screen.surfaces[0].kind, "camera")
        self.assertEqual(
            [button.command for button in screen.surfaces[0].buttons],
            ["refresh_cameras", "connect_camera", "start_camera_live", "stop_camera_live"],
        )

    def test_fluigent_controls_come_from_workflow(self):
        workflow = create_control_workflow()
        state = workflow.complete_current(workflow.initial_state(), confirmed=True)
        screen = build_screen(workflow, state)

        self.assertEqual(
            [button.command for button in screen.buttons],
            ["back", "connect_fluidics", "disconnect_fluidics", "complete"],
        )


if __name__ == "__main__":
    unittest.main()
