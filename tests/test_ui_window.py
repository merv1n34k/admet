import unittest

from admet.ui.window import RenderDecision, WindowController, WindowStageContext, WindowWiring


class WindowWiringTests(unittest.TestCase):
    def test_render_mounts_once_then_syncs(self):
        wiring = WindowWiring()
        calls = []

        first = wiring.render("stage", ("a",), mount=lambda: calls.append("mount"), sync=lambda: calls.append("sync"))
        second = wiring.render("stage", ("a",), mount=lambda: calls.append("mount"), sync=lambda: calls.append("sync"))

        self.assertTrue(first.mounted)
        self.assertFalse(second.mounted)
        self.assertEqual(calls, ["mount", "sync", "sync"])

    def test_render_remounts_shared_on_cached_stage_switch(self):
        wiring = WindowWiring()
        calls = []
        wiring.render("stage", ("a",), mount=lambda: None, sync=lambda: None)

        decision = wiring.render(
            "stage",
            ("a",),
            mount=lambda: calls.append("mount"),
            sync=lambda: calls.append("sync"),
            stage_changed=True,
            remount_shared=lambda: calls.append("shared"),
        )

        self.assertFalse(decision.mounted)
        self.assertTrue(decision.remounted_shared)
        self.assertEqual(calls, ["shared", "sync"])


class WindowControllerTests(unittest.TestCase):
    def test_controller_uses_adapter_hooks(self):
        adapter = FakeAdapter()
        controller = WindowController(adapter)

        decision = controller.render_current_stage()

        self.assertIsNotNone(decision)
        self.assertEqual(adapter.calls, ["prepare", "mount", "sync", "finish"])
        self.assertEqual(adapter.finished, decision)


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.finished: RenderDecision | None = None

    def prepare_window_stage(self) -> WindowStageContext:
        self.calls.append("prepare")
        return WindowStageContext("stage", ("signature",))

    def mount_window_stage(self) -> None:
        self.calls.append("mount")

    def sync_window_stage(self) -> None:
        self.calls.append("sync")

    def finish_window_stage(self, decision: RenderDecision) -> None:
        self.calls.append("finish")
        self.finished = decision

    def remount_shared_window_stage(self) -> None:
        self.calls.append("shared")


if __name__ == "__main__":
    unittest.main()
