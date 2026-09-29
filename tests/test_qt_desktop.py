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
        self.experiment_index = next(i for i, s in enumerate(self.window.workflow.stages) if s.id == "experiment_1")
        self.panel = self.window._protocol_editor(self.window.workflow.stages[self.experiment_index])
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

    def test_json_parameters_use_existing_numeric_settings_table(self):
        from tests.test_protocol_parameters import parameter_protocol
        from admet.ui.control import NumericParamEdit
        from admet.workflows.json_protocol import resolve

        self.panel.set_document(parameter_protocol())
        table = self.panel.parameter_table
        self.assertEqual(table.horizontalHeaderItem(0).text(), "Parameter")
        self.assertEqual(table.item(0, 0).text(), "Oil flow rate, µL/min")
        widget = self.panel.parameter_editors["oil_base_flow"]
        self.assertIsInstance(widget, NumericParamEdit)
        widget.setText("10")
        widget.pending = True
        self.panel.build_plan()
        self.drain()
        self.assertEqual(self.panel.plan["steps"][0]["flow_setpoints_ul_min"], {"1": 15})
        widget.setText("20")
        widget.textEdited.emit("20")
        self.assertFalse(self.panel.executable)
        self.assertEqual(resolve(self.panel.document())["steps"][0]["sensor_setpoints"], {"1": 30})
        self.assertEqual(self.panel.plan["steps"][0]["flow_setpoints_ul_min"], {"1": 15})
        widget.setText("broken")
        widget.pending = True
        with self.assertRaisesRegex(ValueError, "invalid parameter"):
            self.panel.document()
        self.panel.set_document(definition())
        self.assertIsNone(self.panel.parameter_table)
        self.assertEqual(self.panel.parameter_editors, {})

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

    def test_numeric_settings_commit_after_typing_and_invalidate_preview(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        index = next(i for i, stage in enumerate(self.window.workflow.stages) if stage.id == "priming")
        self.window._select_stage(index)
        panel = self.window._protocol_editor(self.window.workflow.stages[index])
        editor = self.window._param_editors["desktop_pressure_limit_mbar"]
        panel.dirty = False
        editor.setFocus()
        editor.selectAll()
        with patch.object(self.window, "_set_value", wraps=self.window._set_value) as commit:
            QTest.keyClicks(editor, "1850.5")
            self.assertIsNone(self.window.values[editor.param.name])
            self.assertTrue(panel.dirty)
            commit.assert_not_called()
            self.window._sync_param_editor(editor.param.name, editor)
            self.assertEqual(editor.text(), "1850.5")
            QTest.keyClick(editor, Qt.Key.Key_Return)
            commit.assert_called_once_with(editor.param.name, 1850.5)
            self.assertEqual(self.window.values[editor.param.name], 1850.5)
            editor.clearFocus()
            self.assertEqual(commit.call_count, 1)

    def test_invalid_pressure_entry_is_retained_and_blocks_planning(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        index = next(i for i, stage in enumerate(self.window.workflow.stages) if stage.id == "priming")
        self.window._select_stage(index)
        panel = self.window._protocol_editor(self.window.workflow.stages[index])
        editor = self.window._param_editors["desktop_pressure_limit_mbar"]
        with patch.object(self.backend, "call", side_effect=AssertionError("invalid draft used")):
            for text in ("0", "nan", "inf", "-1", "abc"):
                with self.subTest(text=text):
                    editor.setFocus()
                    editor.selectAll()
                    QTest.keyClick(editor, Qt.Key.Key_Backspace)
                    QTest.keyClicks(editor, text)
                    QTest.keyClick(editor, Qt.Key.Key_Return)
                    self.assertTrue(editor.pending)
                    self.assertIsNone(self.window.values[editor.param.name])
                    self.window._sync_param_editor(editor.param.name, editor)
                    self.assertEqual(editor.text(), text)
                    panel.build_plan()
                    self.assertIsNone(panel.plan)
            editor.selectAll()
            QTest.keyClicks(editor, "1800")
            self.window.activateWindow()
            self.app.processEvents()
            editor.setFocus()
            self.app.processEvents()
            self.assertTrue(editor.hasFocus())
            editor.clearFocus()
            self.app.processEvents()
            self.assertEqual(self.window.values[editor.param.name], 1800)
            self.assertFalse(editor.pending)

    def test_numeric_integer_validation_and_deferred_auto_apply(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        editor = self.window._param_editors["camera_width"]
        previous = self.window.values["camera_width"]
        with patch.object(self.window, "_schedule_camera_apply") as apply:
            editor.setFocus()
            editor.selectAll()
            QTest.keyClicks(editor, "1234")
            apply.assert_not_called()
            self.assertEqual(self.window.values["camera_width"], previous)
            QTest.keyClick(editor, Qt.Key.Key_Return)
            apply.assert_called_once()
            self.assertEqual(self.window.values["camera_width"], 1234)
            self.assertIsInstance(self.window.values["camera_width"], int)
            editor.selectAll()
            QTest.keyClicks(editor, "1234.5")
            QTest.keyClick(editor, Qt.Key.Key_Return)
            self.assertTrue(editor.pending)
            apply.assert_called_once()
            with patch.object(self.window, "_camera_scene_ready", return_value=True):
                self.assertIsNone(self.window._prepare_action_payload("apply_camera_settings"))

    def test_numeric_draft_survives_parameter_remount(self):
        from PySide6.QtTest import QTest

        index = next(i for i, stage in enumerate(self.window.workflow.stages) if stage.id == "priming")
        self.window._select_stage(index)
        name = "desktop_pressure_limit_mbar"
        editor = self.window._param_editors[name]
        editor.selectAll()
        QTest.keyClicks(editor, "nan")
        self.window.current_stage_page.mounted_signature = None
        self.window._render_current_stage()
        self.assertEqual(self.window._param_editors[name].text(), "nan")
        self.assertTrue(self.window._param_editors[name].pending)
        self.assertIsNone(self.window.values[name])

    def test_optional_pressure_trip_accepts_controller_ceiling_and_clears_to_off(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        index = next(i for i, stage in enumerate(self.window.workflow.stages) if stage.id == "priming")
        self.window._select_stage(index)
        panel = self.window._protocol_editor(self.window.workflow.stages[index])
        editor = self.window._param_editors["desktop_pressure_limit_mbar"]
        self.assertEqual(editor.placeholderText(), "Off")
        QTest.keyClicks(editor, "2000")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        self.assertEqual(self.window.values[editor.param.name], 2000)
        panel.build_plan()
        self.drain()
        self.assertEqual(set(panel.plan["armed_safety_limits"]["pressure_mbar"].values()), {2000})
        editor.selectAll()
        QTest.keyClick(editor, Qt.Key.Key_Backspace)
        QTest.keyClick(editor, Qt.Key.Key_Return)
        self.assertIsNone(self.window.values[editor.param.name])
        panel.build_plan()
        self.drain()
        self.assertEqual(panel.plan["armed_safety_limits"]["pressure_mbar"], {})
        self.assertIn("pressure trips Off", panel.summary.text())

    def test_plan_commits_valid_numeric_draft_without_actuation(self):
        from PySide6.QtTest import QTest

        index = next(i for i, stage in enumerate(self.window.workflow.stages) if stage.id == "priming")
        self.window._select_stage(index)
        panel = self.window._protocol_editor(self.window.workflow.stages[index])
        editor = self.window._param_editors["desktop_pressure_limit_mbar"]
        editor.selectAll()
        QTest.keyClicks(editor, "1750")
        with patch.object(self.backend.engine, "run", side_effect=AssertionError("planning actuated")):
            panel.build_plan()
            self.drain()
        self.assertEqual(self.window.values[editor.param.name], 1750)
        self.assertEqual(set(panel.plan["armed_safety_limits"]["pressure_mbar"].values()), {1750})
        self.assertFalse(editor.pending)

    def test_correction_auto_apply_waits_for_numeric_draft(self):
        from PySide6.QtTest import QTest

        index = next(i for i, stage in enumerate(self.window.workflow.stages) if stage.id == "corrections")
        self.window._select_stage(index)
        self.window._toggle_action_params()
        editor = next(e for e in self.window._param_editors.values() if hasattr(e, "pending"))
        name = editor.param.name
        previous = self.window.values[name]
        editor.selectAll()
        QTest.keyClicks(editor, "2.5")
        with patch.object(self.window, "_fluigent_ready", return_value=True):
            self.assertIsNone(self.window._prepare_action_payload("apply_corrections"))
            self.assertEqual(self.window.values[name], previous)
            with patch.object(self.window, "_schedule_correction_apply") as apply:
                self.assertTrue(editor.commit())
                apply.assert_called_once()
            self.assertEqual(self.window._prepare_action_payload("apply_corrections")[name], 2.5)


    def test_fluigent_simulation_selector_is_visible_and_locked_when_connected(self):
        self.backend.simulated = False
        with patch.object(self.window, "_ensure_fluigent_availability"):
            self.window._select_stage(1)
            self.app.processEvents()
            selector = self.window._param_editors["simulated"]
            self.assertTrue(selector.isVisible())
            self.assertTrue(selector.isEnabled())
            selector.setCurrentIndex(selector.findData(True))
            self.window._run("connect_fluidics")
            self.drain()
            self.assertTrue(self.backend.engine.hardware.state.simulated)
            self.assertFalse(self.window._param_editors["simulated"].isEnabled())
            self.window._set_value("simulated", False)
            self.assertTrue(self.window.values["simulated"])

    def test_preflight_is_non_actuating_and_preserves_project_layout(self):
        ids = [s.id for s in self.window.workflow.stages]
        self.assertEqual(ids[3:6], ["priming", "preflight", "experiment_1"])
        self.assertLess(ids.index("calculations"), ids.index("wash"))
        self.assertNotIn("gravimetry", ids)
        self.assertNotIn("characterise", ids)
        with patch.object(self.backend.engine, "run", side_effect=AssertionError("checkup actuated")):
            self.window._select_stage(ids.index("preflight"))
            self.app.processEvents()
            panel = self.window._ensure_preflight()
            self.assertTrue(panel.scheme.isVisible())
            panel.pressure_limit.setValue(2500)
            self.assertEqual(panel.pressure_limit.value(), 2500)
            self.assertFalse(self.window.main_panel.isVisible())
            self.assertFalse(self.window.action_panel.isVisible())
            stage = self.window.workflow.stages[ids.index("preflight")]
            self.assertEqual(stage.settings_options["sections"], ("flow", "layout", "consumption"))
            self.assertIsNone(self.window._build_check_history(stage))
            self.assertFalse(stage.pipeline)
            self.assertNotIn("Execute", [s[0] for s in self.window._action_button_specs(stage)])
            length, _bore = panel._segment_inputs["Oil L"][0]
            length.setValue(37)
            panel.cells_flow.setValue(11)
            panel.beads_flow.setValue(22)
            panel.record_sweep_point(0, [None, 12], [None, 3])
            panel._gravimetric_rows[0]["empty"].setValue(1.25)
            self.window._save_project()
        saved = self.backend.session.metadata["qt_checkup"]
        self.assertIsNone(saved["flow_checks"][0]["samples"][0]["pressure_mbar"])
        self.assertEqual(saved["conditions"]["layout"]["channels"][1]["flow_ul_min"], 11)
        self.assertEqual(saved["conditions"]["layout"]["channels"][2]["flow_ul_min"], 22)
        self.window._load_project_path(Path(self.backend.workdir))
        self.window._select_stage(ids.index("preflight"))
        panel = self.window._ensure_preflight()
        self.assertEqual(panel._segment_inputs["Oil L"][0][0].value(), 37)
        self.assertEqual(panel._system_rows[0][1][0].value(), 12)
        self.assertEqual(panel._system_rows[0][0][0].text(), "—")
        self.assertEqual(panel._gravimetric_rows[0]["empty"].value(), 1.25)
        self.assertEqual(panel._gravimetric_rows[0]["full"].text(), "—")
        self.assertEqual(panel.flow_checks(), ())

    def test_templates_are_editable_json_not_fixed_stages(self):
        self.window._select_stage(self.experiment_index)
        for name in ("dropseq", "gravimetry", "pressure_flow_check"):
            self.panel.library.setCurrentIndex(self.panel.library.findData("@" + name))
            with patch.object(self.backend.engine, "run", side_effect=AssertionError("template actuated")):
                self.panel.open_saved()
                self.panel.build_plan()
                self.drain()
            self.assertEqual(self.panel.plan["operation_id"], "run_json_protocol")
            self.assertEqual(self.panel.document()["name"], name)
            self.assertTrue(self.panel.plan["steps"][0]["confirmation"])
            self.assertTrue(all(s["on_complete"] == "zero" for s in self.panel.plan["steps"]))
        document = self.panel.document()
        document["name"] = "my_flow_check"
        document["steps"][0]["sensor_setpoints"]["0"] = 25
        self.panel.set_document(document)
        self.assertFalse(self.panel.executable)
        self.panel.save()
        self.drain()
        stored = self.backend.call("list_protocols", {"name": "my_flow_check"})["protocol"]
        self.assertEqual(stored["steps"][0]["sensor_setpoints"]["0"], 25)
        self.assertEqual(self.panel.templates["pressure_flow_check"]["steps"][0]["sensor_setpoints"]["0"], 50)

    def test_density_template_and_persistent_calculations_section(self):
        from tests.test_calculations import archived_density

        archived_density(self.backend.workdir)
        self.window._select_stage(self.experiment_index)
        self.panel.library.setCurrentIndex(self.panel.library.findData("@density_dsurf"))
        with patch.object(self.backend.engine, "run", side_effect=AssertionError("actuation")):
            self.panel.open_saved()
            self.panel.build_plan()
            self.drain()
        self.assertEqual(self.panel.plan["expected_duration_s"], 420)
        self.assertIn("5 cm ABOVE", self.panel.plan["steps"][1]["confirmation"])
        self.assertNotIn("temperature", self.panel.editor.toPlainText())
        self.assertNotIn("analysis", self.panel.document())
        calculation_index = next(i for i, stage in enumerate(self.window.workflow.stages) if stage.id == "calculations")
        with patch.object(self.backend.engine, "run", side_effect=AssertionError("calculation actuated")):
            self.window._select_stage(calculation_index)
            panel = self.window._calculations
            self.drain(lambda: not panel.tasks.busy)
            self.assertEqual(panel.runs.count(), 1)
            panel.calculate()
            self.drain(lambda: not panel.tasks.busy)
            self.assertIn("1.2000 g/mL", panel.output.toPlainText())
            self.assertEqual(panel.history.count(), 1)
            panel.calculation.setCurrentIndex(panel.calculation.findData("recording_summary"))
            panel.calculate()
            self.drain(lambda: not panel.tasks.busy)
            self.assertEqual(panel.history.count(), 2)
            self.window._select_stage(self.experiment_index)
            self.window._select_stage(calculation_index)
            self.drain(lambda: not panel.tasks.busy)
            self.assertFalse(panel.isHidden())
            self.assertEqual(panel.history.count(), 2)
            self.window._reset_project_workflow()
            self.window._select_stage(calculation_index)
            self.drain(lambda: not panel.tasks.busy)
            self.assertEqual(panel.history.count(), 2)

    def test_gui_json_save_plan_review_confirm_completion(self):
        self.backend.call("connect_fluidics")
        self.backend.call("apply_corrections")
        self.window._select_stage(self.experiment_index)
        self.panel.set_document(definition())
        self.panel.save()
        self.drain()
        self.assertEqual(len(self.backend.call("list_protocols")["protocols"]), 1)
        with patch.object(self.backend.engine, "run", side_effect=AssertionError("planning actuated")):
            self.panel.build_plan()
            self.drain()
        self.assertEqual(self.panel.table.rowCount(), 1)
        self.assertEqual(self.panel.table.item(0, 1).text(), "0\n1\n2")
        self.assertEqual(self.panel.table.item(0, 3).text(), "10 µL/min\n5 µL/min\n5 µL/min")
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
        self.assertTrue((Path(self.backend.workdir) / "records" / "protocols" / self.panel.plan["run_id"] / "summary.json").exists())

    def test_editor_changes_disable_execution_of_old_preview(self):
        self.panel.set_document(definition())
        self.panel.build_plan()
        self.drain()
        self.assertTrue(self.panel.executable)
        self.panel.editor.insertPlainText(" ")
        self.assertFalse(self.panel.executable)

    def test_plan_gates_are_clear_and_instructions_are_folded(self):
        self.window._select_stage(self.experiment_index)
        self.panel.set_document(self.panel.templates["gravimetry"])
        self.panel.build_plan()
        self.drain()
        self.assertEqual(self.panel.table.rowCount(), 6)
        self.assertEqual(self.panel.table.item(1, 2).text(), "confirm")
        self.assertEqual(self.panel.table.item(1, 4).text(), "Operator: Weigh Oil L")
        self.assertEqual(self.panel.table.item(0, 6).text(), "Before")
        self.assertFalse(self.panel.details_box.isVisible())
        self.panel.table.setCurrentCell(1, 0)
        self.assertTrue(self.panel.details_box.isVisible())
        self.assertIn("record empty and full masses", self.panel.details.text())
        self.assertIn("Timeout:", self.panel.details.text())
        self.assertEqual(self.panel.plan["steps"][1]["trigger_type"], "time")
        self.assertEqual(self.panel.plan["steps"][1]["trigger_params"]["duration_s"], 0)
        self.panel.details_box.hide()
        self.assertFalse(self.panel.details_box.isVisible())

    def test_preview_accepts_default_stability_parameters(self):
        from admet.engines.acquisition.fluidics.config import STABILITY_DURATION_S

        document = definition()
        document["steps"][0].update(trigger_type="stability", trigger_params={"sensor_index": 0}, timeout_s=30)
        self.panel.set_document(document)
        self.panel.build_plan()
        self.drain()
        self.assertIsNotNone(self.panel.plan)
        self.assertIn(f"for {STABILITY_DURATION_S:g} s", self.panel.table.item(0, 4).text())

    def test_plan_table_fits_all_rows_and_reflows_without_nested_scroll(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QTableWidgetItem

        from admet.ui.protocols import PlanTable

        table = PlanTable()
        try:
            table.setHorizontalHeaderLabels([
                "STEP", "UNIT ID", "TYPE", "TARGET", "TRIGGER / ETA", "END", "CONFIRM",
            ])
            table.setRowCount(30)
            for row in range(30):
                for column, value in enumerate([
                    str(row + 1), "0", "pressure", "1800 mbar",
                    "volume ch 0: 50 µL\nETA 30 s / timeout 120 s", "zero",
                    "Confirm the physical channel mapping and collection tube before continuing.",
                ]):
                    table.setItem(row, column, QTableWidgetItem(value))
            table.resize(1100, 300)
            table.show()
            self.app.processEvents()
            wide_height = table.height()
            for width in (640, 900, 1100):
                table.resize(width, table.height())
                self.app.processEvents()
                self.assertLessEqual(table.horizontalHeader().length(), table.viewport().width())
                self.assertGreaterEqual(
                    table.viewport().height(), sum(table.rowHeight(r) for r in range(30)),
                )
                self.assertEqual(table.verticalScrollBar().maximum(), 0)
                self.assertEqual(table.horizontalScrollBar().maximum(), 0)
                self.assertEqual(table.textElideMode(), Qt.TextElideMode.ElideNone)
                if width == 640:
                    self.assertGreater(table.height(), wide_height)
            self.assertEqual(table.height(), wide_height)
        finally:
            table.close()
            table.deleteLater()

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
        self.window._select_stage(self.experiment_index)
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
        self.window._select_stage(self.experiment_index)
        self.app.processEvents()
        self.assertTrue(self.panel.library.isVisible())
        self.assertTrue(self.panel.table.isVisible())

    def test_saved_custom_toc_order_survives_project_reopen(self):
        self.window._select_stage(self.experiment_index)
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
        from PySide6.QtWidgets import QMessageBox

        self.backend.call("connect_fluidics")
        self.window.close()
        self.assertTrue(self.backend.service.state()["fluidics"])
        self.assertIn("Close ADMET?", self.window._confirmation_dialog.text())
        self.window._confirmation_dialog.button(QMessageBox.StandardButton.Ok).click()
        self.drain(lambda: self.window._shutdown_complete)
        self.assertFalse(self.backend.service.state()["fluidics"])
        self.assertTrue(self.backend.engine.safety_state()["tripped"])

    def test_execute_uses_only_the_native_protocol_confirmation(self):
        self.backend.call("connect_fluidics")
        self.backend.call("apply_corrections")
        self.window._select_stage(self.experiment_index)
        self.panel.set_document(definition())
        self.panel.build_plan()
        self.drain()
        with patch.object(self.window, "_confirm", side_effect=AssertionError("extra confirmation")):
            self.panel.execute()
            self.drain()
        self.window._poll_pipeline_events()
        self.assertIn("Confirm physical", self.window._pending_pipeline_confirmation())
        self.assertIsNone(self.window._confirmation_dialog)
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

    def test_reset_and_overwrite_use_popup_confirmation(self):
        from PySide6.QtWidgets import QMessageBox

        self.backend.emergency_stop()
        self.window._run("reset_safety")
        self.assertTrue(self.backend.engine.safety_state()["tripped"])
        self.window._confirmation_dialog.close()
        self.assertTrue(self.backend.engine.safety_state()["tripped"])
        self.window._run("reset_safety")
        self.window._confirmation_dialog.button(QMessageBox.StandardButton.Ok).click()
        self.drain()
        self.assertFalse(self.backend.engine.safety_state()["tripped"])
        self.window._select_stage(self.experiment_index)
        self.panel.set_document(definition())
        self.panel.save()
        self.drain()
        replacement = definition(2)
        self.panel.set_document(replacement)
        self.panel.save()
        self.drain()
        self.assertIn("Replace saved definition", self.window._confirmation_dialog.text())
        stored = self.backend.call("list_protocols", {"name": "desktop_test"})["protocol"]
        self.assertEqual(stored["steps"][0]["trigger_params"]["duration_s"], 0.15)
        self.window._confirmation_dialog.button(QMessageBox.StandardButton.Ok).click()
        self.drain()
        stored = self.backend.call("list_protocols", {"name": "desktop_test"})["protocol"]
        self.assertEqual(stored["steps"][0]["trigger_params"]["duration_s"], 2)

    def test_program_confirmation_is_focused_modal_and_single_use(self):
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QMessageBox

        calls = []
        ticks = []
        self.window._confirm("Reset?", lambda: calls.append("reset"))
        dialog = self.window._confirmation_dialog
        QTimer.singleShot(0, lambda: ticks.append(True))
        self.app.processEvents()
        self.assertIs(self.app.activeModalWidget(), dialog)
        self.assertEqual(dialog.windowModality(), Qt.WindowModality.WindowModal)
        self.assertIs(dialog.defaultButton(), dialog.button(QMessageBox.StandardButton.Cancel))
        self.assertTrue(dialog.button(QMessageBox.StandardButton.Cancel).hasFocus())
        self.assertTrue(ticks)
        self.window._notify("Telemetry updated")
        self.assertIs(self.window._confirmation_dialog, dialog)
        self.window._confirm("Different action", lambda: calls.append("wrong"))
        self.assertIs(self.window._confirmation_dialog, dialog)
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.drain(lambda: self.window._confirmation_dialog is None)
        self.assertIsNone(self.window._confirmation_dialog)
        self.assertEqual(calls, [])
        self.window._confirm("Reset?", lambda: calls.append("reset"))
        dialog = self.window._confirmation_dialog
        dialog.button(QMessageBox.StandardButton.Ok).click()
        dialog.finished.emit(QMessageBox.StandardButton.Ok)
        self.assertEqual(calls, ["reset"])

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
