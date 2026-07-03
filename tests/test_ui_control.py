import unittest

from admet.ui.theme import box_padding, button_qss, spacing, stylesheet, text_qss


class ControlThemeTests(unittest.TestCase):
    def test_theme_helpers_are_local_and_pure(self):
        self.assertEqual(spacing("control"), 4)
        self.assertEqual(box_padding("none"), (0, 0, 0, 0))
        self.assertIn("font-weight: 600", text_qss("primary", bold=True))
        self.assertIn("QPushButton", button_qss("danger"))
        self.assertIn("QMainWindow", stylesheet())


if __name__ == "__main__":
    unittest.main()
