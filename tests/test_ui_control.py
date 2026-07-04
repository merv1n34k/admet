import unittest

from admet.ui.theme import box_padding, button_qss, spacing, stylesheet, text_qss
from admet.workflows.control import ControlRuntime, control_button_specs, create_control_workflow


class ControlThemeTests(unittest.TestCase):
    def test_theme_helpers_are_local_and_pure(self):
        self.assertEqual(spacing("control"), 4)
        self.assertEqual(box_padding("none"), (0, 0, 0, 0))
        self.assertIn("font-weight: 600", text_qss("primary", bold=True))
        self.assertIn("QPushButton", button_qss("danger"))
        self.assertIn("QMainWindow", stylesheet())


class ControlWorkflowCommandTests(unittest.TestCase):
    def test_scene_commands_are_owned_by_workflow_runtime(self):
        stage = create_control_workflow().stages[0]
        blocked = {spec.label: spec for spec in control_button_specs(stage, ControlRuntime())}
        ready = {
            spec.label: spec
            for spec in control_button_specs(
                stage,
                ControlRuntime(project_ready=True, camera_connected=True, camera_live=True),
            )
        }

        self.assertFalse(blocked["Refresh"].enabled)
        self.assertTrue(ready["Refresh"].enabled)
        self.assertFalse(ready["Connect"].enabled)
        self.assertTrue(ready["Disconnect"].enabled)
        self.assertTrue(ready["Live"].checked)

    def test_fluigent_connect_requires_simulation_or_detected_instrument(self):
        stage = create_control_workflow().stages[1]
        blocked = {spec.label: spec for spec in control_button_specs(stage, ControlRuntime())}
        simulated = {spec.label: spec for spec in control_button_specs(stage, ControlRuntime(simulated=True))}

        self.assertFalse(blocked["Connect"].enabled)
        self.assertTrue(simulated["Connect"].enabled)


if __name__ == "__main__":
    unittest.main()
