import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_qt_backend import definition

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HAS_QT = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(HAS_QT, "install the control extra for Qt acceptance tests")
class DesktopWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from admet.ui.backend import DesktopBackend
        from admet.ui.control import ControlWindow

        self.tmp = tempfile.TemporaryDirectory()
        self.backend = DesktopBackend(simulated=True)
        self.backend.create_project(Path(self.tmp.name) / "gui.admetp")
        self.window = ControlWindow(self.backend)
        self.window.timer.stop()
        self.panel = self.window._protocol_editor(self.window.workflow.stages[4])
        self.window.show()

    def tearDown(self):
        self.drain()
        self.backend.shutdown()
        self.window._shutdown_complete = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def drain(self, condition=None):
        deadline = time.monotonic() + 5
        condition = condition or (lambda: not self.window.tasks.busy and not self.window.emergency_tasks.busy)
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.005)
        self.fail("Qt worker did not yield")

    def test_restored_device_stages_and_nonblocking_connections(self):
        for index in range(len(self.window.workflow.stages)):
            self.window._select_stage(index)
            self.app.processEvents()
        self.window._run("connect_fluidics")
        self.drain()
        self.assertTrue(self.backend.service.state()["fluidics"])
        self.window._run("apply_corrections")
        self.drain()
        self.assertTrue(self.backend.service.state()["corrections"])
        self.assertIn("TEST SIMULATION", self.window.windowTitle())

    def test_gui_json_save_plan_review_confirm_completion(self):
        self.backend.call("connect_fluidics")
        self.backend.call("apply_corrections")
        self.window._select_stage(4)
        self.panel.set_document(definition())
        self.panel.save()
        self.drain()
        self.assertEqual(len(self.backend.call("list_protocols")["protocols"]), 1)
        with patch.object(self.backend.engine, "run", side_effect=AssertionError("planning actuated")):
            self.panel.build_plan()
            self.drain()
        self.assertEqual(self.panel.table.rowCount(), 3)
        self.assertEqual(self.panel.table.item(0, 3).text(), "10 µL/min")
        self.assertEqual(self.panel.table.item(1, 0).text(), "1")
        self.assertIn("time: 0.15 s", self.panel.table.item(0, 4).text())
        self.assertIn("500", self.panel.summary.text())
        plan_id = self.panel.plan["plan_id"]
        self.panel.execute()
        self.drain()
        self.window._poll_pipeline_events()
        self.assertIn("Confirm physical", self.window._pending_pipeline_confirmation())
        self.panel.control("confirm")
        self.drain()
        self.drain(lambda: self.backend.call("planned_protocols", {"plan_id": plan_id})["plans"][0]["state"] == "completed")
        self.panel.update_plan(self.backend.call("planned_protocols")["plans"])
        self.window._poll_pipeline_events()
        self.assertEqual(self.panel.plan["state"], "completed")
        self.assertFalse(self.panel.executable)
        self.assertIn("completed", "\n".join(self.window.log_entries))
        self.assertTrue((Path(self.backend.workdir) / "records" / "protocols" / plan_id / "summary.json").exists())

    def test_editor_changes_disable_execution_of_old_preview(self):
        self.panel.set_document(definition())
        self.panel.build_plan()
        self.drain()
        self.assertTrue(self.panel.executable)
        self.panel.editor.insertPlainText(" ")
        self.assertFalse(self.panel.executable)

    def test_custom_stages_stay_between_fixed_priming_and_wash(self):
        from PySide6.QtWidgets import QTabWidget

        stage = self.window._add_protocol_stage("Second experiment")
        ids = [s.id for s in self.window.workflow.stages]
        self.assertLess(ids.index("priming"), ids.index(stage.id))
        self.assertLess(ids.index(stage.id), ids.index("wash"))
        self.assertEqual(len(self.window.toc_rows), len(ids))
        self.assertFalse(self.window.findChildren(QTabWidget))
        editor = self.window._protocol_editor(stage)
        editor.set_document(definition())
        self.window._select_stage(0)
        self.window._select_stage(ids.index(stage.id))
        self.assertIs(self.window._protocol_editor(stage), editor)
        self.assertEqual(editor.document()["name"], "desktop_test")
        self.assertTrue(editor.isVisible())
        labels = [spec[0] for spec in self.window._action_button_specs(stage)]
        self.assertEqual(labels, ["Plan", "Execute", "Confirm", "Skip", "Pause", "Abort", "Continue", "E-STOP"])

    def test_fixed_protocols_build_bounded_json_using_original_steps(self):
        for stage_id, names in (
            ("priming", ["Prime Oil L", "Prime Cells M", "Prime Beads M"]),
            ("wash", ["Wash flow phase", "Wash pressure phase", "Confirm wash complete"]),
        ):
            index = next(i for i, s in enumerate(self.window.workflow.stages) if s.id == stage_id)
            self.window._select_stage(index)
            editor = self.window._protocol_editor(self.window.workflow.stages[index])
            editor.build_plan()
            self.drain()
            self.assertIsNotNone(editor.plan)
            self.assertEqual([s["name"] for s in editor.plan["steps"]], names)
            self.assertEqual(editor.plan["operation_id"], "run_json_protocol")
            self.assertTrue(all(s["timeout_s"] > 0 for s in editor.plan["steps"]))
        self.window._set_value("wash_pressure_mbar", 1750)
        self.assertFalse(editor.executable)

    def test_protocol_selector_and_preview_are_between_actions_and_graphs(self):
        self.window._select_stage(4)
        page = self.window.current_stage_page
        self.app.processEvents()
        self.assertLess(page.action_box_panel.y(), page.action_panel.y())
        self.assertLess(page.action_panel.y(), page.main_panel.y())
        self.assertTrue(self.panel.library.isVisible())
        self.assertTrue(self.panel.isVisible())
        self.backend.call("save_protocol", {"protocol": definition()})
        self.window._refresh_protocol_library()
        self.drain()
        self.panel.library.setCurrentIndex(1)
        self.panel.open_saved()
        self.drain()
        self.assertEqual(self.panel.document()["name"], "desktop_test")
        self.panel.build_plan()
        self.drain()
        self.assertTrue(self.panel.table.isVisible())
        self.window._select_stage(0)
        self.window._select_stage(4)
        self.app.processEvents()
        self.assertTrue(self.panel.library.isVisible())
        self.assertTrue(self.panel.table.isVisible())

    def test_saved_custom_toc_order_survives_project_reopen(self):
        self.window._select_stage(4)
        self.panel.set_document(definition())
        self.panel.save()
        self.drain()
        stage = self.window._add_protocol_stage("Follow-up")
        second = self.window._protocol_editor(stage)
        document = definition()
        document["name"] = "follow_up"
        second.set_document(document)
        second.save()
        self.drain()
        path = Path(self.backend.workdir)
        self.window._load_project_path(path)
        middle = [s for s in self.window.workflow.stages if "json_protocol" in s.features
                  and not s.settings_options.get("builtin")]
        self.assertEqual([s.id for s in middle], ["experiment_1", stage.id])
        self.assertEqual([self.window._protocol_editor(s).document()["name"] for s in middle],
                         ["desktop_test", "follow_up"])

    def test_event_loop_and_emergency_remain_responsive_during_command(self):
        from PySide6.QtCore import QTimer

        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start(5)

        def blocked():
            with self.backend.lock:
                entered.set()
                release.wait(3)

        self.window.tasks.submit(blocked, lambda _: None, self.fail)
        self.assertTrue(entered.wait(1))
        self.window._emergency_stop()
        self.drain(lambda: not self.window.emergency_tasks.busy)
        self.assertTrue(self.backend.engine.safety_state()["tripped"])
        self.drain(lambda: len(ticks) >= 3)
        release.set()
        self.drain()
        timer.stop()

    def test_missing_measurements_are_not_zero(self):
        from types import SimpleNamespace
        from admet.ui.control import FluidicsMonitorTable

        table = FluidicsMonitorTable()
        table.update_from_snapshot(SimpleNamespace(
            pressures=[None], flows=[None], flow_stats=[None], volumes_ul=[None], stability=[None],
        ))
        self.assertEqual(table.table.item(0, 3).text(), "—")
        self.assertEqual(table.table.item(0, 8).text(), "—")
        self.assertIn("—", table.table.item(0, 1).text())

    def test_window_close_waits_for_zero_and_disconnect(self):
        self.backend.call("connect_fluidics")
        self.window.close()
        self.assertTrue(self.backend.service.state()["fluidics"])
        self.assertIn("Close ADMET?", self.window.notification.text_label.text())
        self.window.notification._confirm()
        self.drain(lambda: self.window._shutdown_complete)
        self.assertFalse(self.backend.service.state()["fluidics"])
        self.assertTrue(self.backend.engine.safety_state()["tripped"])

    def test_execute_uses_only_the_native_protocol_confirmation(self):
        self.backend.call("connect_fluidics")
        self.backend.call("apply_corrections")
        self.window._select_stage(4)
        self.panel.set_document(definition())
        self.panel.build_plan()
        self.drain()
        with patch.object(self.window, "_confirm", side_effect=AssertionError("extra confirmation")):
            self.panel.execute()
            self.drain()
        self.window._poll_pipeline_events()
        self.assertIn("Confirm physical", self.window._pending_pipeline_confirmation())
        self.assertIsNone(self.window.notification._on_confirm)
        observed = self.backend.call("observe")
        self.assertTrue(all(c["requested_flow_ul_min"] in (None, 0) for c in observed["channels"]))
        self.panel.control("abort")
        self.drain()

    def test_desktop_launcher_has_no_mode_flags(self):
        import contextlib
        import io
        from admet.ui.app import main

        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertNotIn("--simulated", output.getvalue())
        self.assertNotIn("--live", output.getvalue())

    def test_reset_and_overwrite_use_inline_confirmation(self):
        self.backend.emergency_stop()
        self.window._run("reset_safety")
        self.assertTrue(self.backend.engine.safety_state()["tripped"])
        self.window.notification.close()
        self.assertTrue(self.backend.engine.safety_state()["tripped"])
        self.window._run("reset_safety")
        self.window.notification._confirm()
        self.drain()
        self.assertFalse(self.backend.engine.safety_state()["tripped"])
        self.window._select_stage(4)
        self.panel.set_document(definition())
        self.panel.save()
        self.drain()
        replacement = definition(2)
        self.panel.set_document(replacement)
        self.panel.save()
        self.drain()
        self.assertIn("Replace saved definition", self.window.notification.text_label.text())
        stored = self.backend.call("list_protocols", {"name": "desktop_test"})["protocol"]
        self.assertEqual(stored["steps"][0]["trigger_params"]["duration_s"], 0.15)
        self.window.notification._confirm()
        self.drain()
        stored = self.backend.call("list_protocols", {"name": "desktop_test"})["protocol"]
        self.assertEqual(stored["steps"][0]["trigger_params"]["duration_s"], 2)

    def test_only_one_desktop_can_own_the_session(self):
        from admet.ui.app import desktop_lock

        lock = desktop_lock(self.tmp.name)
        try:
            with self.assertRaisesRegex(RuntimeError, "Another ADMET Qt"):
                desktop_lock(self.tmp.name)
        finally:
            lock.unlock()
        replacement = desktop_lock(self.tmp.name)
        replacement.unlock()
