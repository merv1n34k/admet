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
    def test_controller_uses_context_hooks(self):
        hooks = FakeHooks()
        controller = WindowController()

        decision = controller.render_current_stage(hooks.prepare)

        self.assertIsNotNone(decision)
        self.assertEqual(hooks.calls, ["prepare", "mount", "sync", "finish"])
        self.assertEqual(hooks.finished, decision)


class FakeHooks:
    def __init__(self):
        self.calls = []
        self.finished: RenderDecision | None = None

    def prepare(self) -> WindowStageContext:
        self.calls.append("prepare")
        return WindowStageContext(
            "stage",
            ("signature",),
            mount=self.mount,
            sync=self.sync,
            finish=self.finish,
            remount_shared=self.remount_shared,
        )

    def mount(self) -> None:
        self.calls.append("mount")

    def sync(self) -> None:
        self.calls.append("sync")

    def finish(self, decision: RenderDecision) -> None:
        self.calls.append("finish")
        self.finished = decision

    def remount_shared(self) -> None:
        self.calls.append("shared")


if __name__ == "__main__":
    unittest.main()
