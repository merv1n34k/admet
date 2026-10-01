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
        from PySide6.QtCore import QSettings
        from admet.ui.backend import DesktopBackend
        from admet.ui.control import ControlWindow

        self.tmp = tempfile.TemporaryDirectory()
        self.settings_path = str(Path(self.tmp.name) / "qt-settings.ini")
        settings_patch = patch("admet.ui.control.QSettings", return_value=QSettings(self.settings_path, QSettings.Format.IniFormat))
        settings_factory = settings_patch.start()
        settings_factory.Status = QSettings.Status
        self.addCleanup(settings_patch.stop)
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

    def test_project_menu_counts_runs_and_remembers_external_project_after_restart(self):
        from PySide6.QtCore import QSettings
        from admet.core.project import ProjectStore
        from admet.ui.backend import DesktopBackend
        from admet.ui.control import ControlWindow

        root = Path(self.tmp.name).resolve()
        default = root / "projects"
        default.mkdir()
        self.window.discovery_root = default
        external = ProjectStore.create(root / "elsewhere.admetp", "external")
        summary = external.path / "records/protocols/run_test/summary.json"
        summary.parent.mkdir(parents=True)
        summary.write_text('{"state":"completed"}')
        external.upsert_file_path(summary, role="protocol_run")
        external.upsert_file_path(external.path / "records/flow.csv", role="control_fluidics_csv")
        external.save()
        self.window._load_project_path(external.path)
        menu = self.window._build_project_menu()
        action = next(action for action in menu.actions() if action.text().startswith("external ·"))
        self.assertIn("1 runs", action.text())
        self.assertNotIn("files", action.text())
        self.assertIn("last opened:", action.text())
        self.assertNotIn("—", action.text())
        self.assertEqual(action.toolTip(), str(external.path))
        self.assertTrue(action.isChecked())
        settings = QSettings(self.settings_path, QSettings.Format.IniFormat)
        recent = settings.value("recent_projects")
        self.assertEqual(recent[str(external.path)], self.window.recent_projects[str(external.path)])
        missing = str(root / "removed.admetp")
        recent[missing] = "2026-01-01T12:00:00+00:00"
        recent[str(default)] = "2026-01-01T12:00:00+00:00"
        settings.setValue("recent_projects", recent)
        settings.sync()
        other_backend = DesktopBackend(simulated=True)
        with patch("admet.ui.control.projects_root", return_value=default):
            reopened = ControlWindow(other_backend)
        reopened.timer.stop()
        try:
            self.assertIn(external.path, [ref.path for ref in reopened.project_refs])
            self.assertNotIn(missing, reopened.recent_projects)
            self.assertNotIn(str(default), reopened.recent_projects)
            settings.sync()
            self.assertNotIn(missing, settings.value("recent_projects"))
            self.assertTrue(external.path.is_dir())
        finally:
            other_backend.shutdown()
            reopened._shutdown_complete = True
            reopened.close()
            reopened.deleteLater()
            self.app.processEvents()

    def test_failed_project_open_does_not_change_history(self):
        before = dict(self.window.recent_projects)
        self.window._load_project_path(Path(self.tmp.name) / "missing.admetp")
        self.assertEqual(self.window.recent_projects, before)

    def test_new_project_is_added_to_recent_projects(self):
        path = Path(self.tmp.name).resolve() / "new.admetp"
        with patch("admet.ui.control.QFileDialog.getSaveFileName", return_value=(str(path), "")):
            self.window._new_project()
        self.assertIn(str(path), self.window.recent_projects)

    def test_typed_parameters_use_native_editors_and_lock_with_execution(self):
        from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit
        from tests.test_protocol_parameters import typed_parameter_protocol
        from admet.ui.control import NumericParamEdit
        from admet.workflows.json_protocol import resolve

        self.panel.set_document(typed_parameter_protocol())
        editors = self.panel.parameter_editors
        for name, kind in (("oil", QLineEdit), ("offset", NumericParamEdit),
                           ("filtered", QCheckBox), ("finish", QComboBox)):
            self.assertIsInstance(editors[name], kind)
        editors["oil"].setText("custom mix")
        editors["filtered"].setChecked(False)
        editors["finish"].setCurrentIndex(1)
        editors["offset"].setText("-0.5")
        editors["offset"].pending = True
        self.panel.build_plan()
        self.drain()
        document = self.panel.document()
        self.assertEqual(document["parameter_values"]["offset"], -0.5)
        self.assertIs(document["parameter_values"]["filtered"], False)
        self.assertEqual(resolve(document)["steps"][0]["name"], "custom mix, filtered=false")
        self.assertEqual(self.panel.plan["steps"][0]["on_complete"], "hold")
        editors["oil"].textEdited.emit("other")
        self.assertTrue(self.panel.dirty)
        self.panel.lock_definition(True)
        self.assertTrue(all(not e.isEnabled() for e in editors.values()))
        self.panel.lock_definition(False)
        self.assertTrue(all(e.isEnabled() for e in editors.values()))

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

    def test_setup_mode_allows_camera_free_continue_and_invalidates_preview(self):
        from PySide6.QtWidgets import QComboBox
        from admet.ui.workflow_view import guard_enabled

        self.panel.set_document(definition())
        self.panel.build_plan()
        self.drain()
        self.window._select_stage(0)
        scene = self.window.workflow.stages[0]
        guard = next(a.guard for a in scene.actions if a.completes)
        self.assertTrue(guard_enabled(guard, self.window._guard_value))
        mode = self.window._param_editors["acquisition_mode"]
        self.assertIsInstance(mode, QComboBox)
        self.assertIsNone(self.window.action_box_panel.findChild(QComboBox))
        self.assertTrue(mode.isEnabled())
        mode.setCurrentIndex(1)
        self.assertEqual(self.backend.acquisition_mode, "camera_fluidics")
        self.assertFalse(guard_enabled(guard, self.window._guard_value))
        self.assertTrue(self.panel.dirty)
        mode.setCurrentIndex(0)
        self.assertTrue(guard_enabled(guard, self.window._guard_value))

    def test_fluidics_mode_hides_preview_and_keeps_graphs_across_stages(self):
        import numpy as np

        self.window._select_stage(0)
        mode = self.window._param_editors["acquisition_mode"]
        self.assertFalse(self.window.preview.isVisible())
        self.assertTrue(self.window.plot_panel.isVisible())
        mode.setCurrentIndex(1)
        self.assertTrue(self.window.preview.isVisible())
        self.window._select_stage(self.experiment_index)
        self.app.processEvents()
        self.assertTrue(self.window.preview.isVisible())
        self.window._select_stage(0)
        self.window._param_editors["acquisition_mode"].setCurrentIndex(0)
        self.assertFalse(self.window.preview.isVisible())
        self.window._select_stage(self.experiment_index)
        self.app.processEvents()
        self.assertFalse(self.window.preview.isVisible())
        self.assertTrue(self.window.plot_panel.isVisible())
        with patch.object(self.backend.engine, "acknowledge_camera_frame") as acknowledge:
            self.window._show_camera_frame(np.zeros((12, 16), dtype=np.uint8))
        acknowledge.assert_called_once()
        self.assertFalse(self.window._camera_ack_pending)

    def test_numeric_settings_commit_after_typing_and_invalidate_preview(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        index = next(i for i, stage in enumerate(self.window.workflow.stages) if stage.id == "priming")
        self.window._select_stage(index)
        panel = self.window._protocol_editor(self.window.workflow.stages[index])
        editor = self.window._param_editors["prime_oil_volume_ul"]
        panel.dirty = False
        editor.setFocus()
        editor.selectAll()
        with patch.object(self.window, "_set_value", wraps=self.window._set_value) as commit:
            QTest.keyClicks(editor, "1850.5")
            self.assertEqual(self.window.values[editor.param.name], 40.0)
            self.assertTrue(panel.dirty)
            commit.assert_not_called()
            self.window._sync_param_editor(editor.param.name, editor)
            self.assertEqual(editor.text(), "1850.5")
            QTest.keyClick(editor, Qt.Key.Key_Return)
            commit.assert_called_once_with(editor.param.name, 1850.5)
            self.assertEqual(self.window.values[editor.param.name], 1850.5)
            editor.clearFocus()
            self.assertEqual(commit.call_count, 1)

    def test_invalid_numeric_entry_is_retained_and_blocks_planning(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        index = next(i for i, stage in enumerate(self.window.workflow.stages) if stage.id == "priming")
        self.window._select_stage(index)
        panel = self.window._protocol_editor(self.window.workflow.stages[index])
        editor = self.window._param_editors["prime_oil_volume_ul"]
        with patch.object(self.backend, "call", side_effect=AssertionError("invalid draft used")):
            for text in ("0", "nan", "inf", "-1", "abc"):
                with self.subTest(text=text):
                    editor.setFocus()
                    editor.selectAll()
                    QTest.keyClick(editor, Qt.Key.Key_Backspace)
                    QTest.keyClicks(editor, text)
                    QTest.keyClick(editor, Qt.Key.Key_Return)
                    self.assertTrue(editor.pending)
                    self.assertEqual(self.window.values[editor.param.name], 40.0)
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
        name = "prime_oil_volume_ul"
        editor = self.window._param_editors[name]
        editor.selectAll()
        QTest.keyClicks(editor, "nan")
        self.window.current_stage_page.mounted_signature = None
        self.window._render_current_stage()
        self.assertEqual(self.window._param_editors[name].text(), "nan")
        self.assertTrue(self.window._param_editors[name].pending)
        self.assertEqual(self.window.values[name], 40.0)

    def test_plan_commits_valid_numeric_draft_without_actuation(self):
        from PySide6.QtTest import QTest

        index = next(i for i, stage in enumerate(self.window.workflow.stages) if stage.id == "priming")
        self.window._select_stage(index)
        panel = self.window._protocol_editor(self.window.workflow.stages[index])
        editor = self.window._param_editors["prime_oil_volume_ul"]
        editor.selectAll()
        QTest.keyClicks(editor, "1750")
        with patch.object(self.backend.engine, "run", side_effect=AssertionError("planning actuated")):
            panel.build_plan()
            self.drain()
        self.assertEqual(self.window.values[editor.param.name], 1750)
        self.assertIsNotNone(panel.plan)
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

    def test_liquid_summary_refreshes_in_place_and_survives_stage_switches(self):
        from PySide6.QtCore import Qt
        from admet.ui.tables import SummaryLabel

        index = next(i for i, stage in enumerate(self.window.workflow.stages) if stage.id == "corrections")
        self.window._select_stage(index)
        page = self.window.current_stage_page
        summary = page.liquid_summary
        self.assertIsInstance(summary, SummaryLabel)
        self.assertEqual(summary.textFormat(), Qt.TextFormat.PlainText)
        self.assertTrue(summary.wordWrap())
        self.assertIn("Cells M - Water (M): H2O table, scale 1", summary.text())
        with patch.object(self.backend.engine, "apply_corrections", side_effect=AssertionError("actuation")):
            selector = self.window._param_editors["cells_m_profile"]
            selector.setCurrentIndex(selector.findData("oil_m"))
            self.app.processEvents()
            self.assertIs(page.liquid_summary, summary)
            self.assertIn("Cells M - Oil (M): IPA table, scale 2.25", summary.text())
            self.assertNotIn("Cells M - Water (M)", summary.text())
            self.window._set_value("cells_m_scale", 3.0)
            self.assertIn("Cells M - Oil (M): IPA table, scale 3,", summary.text())
            self.window._select_stage(self.experiment_index)
            self.window._select_stage(index)
            self.assertIs(page.liquid_summary, summary)
            self.assertIn("Cells M - Oil (M): IPA table, scale 3,", summary.text())


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
            self.assertEqual(set(panel.sections), {"flow", "layout", "consumption"})
            self.assertFalse(stage.pipeline)
            self.assertNotIn("Execute", [s[0] for s in self.window._action_button_specs(stage)])
            length, _bore = panel._segment_inputs["Oil L"][0]
            length.setValue(37)
            panel.cells_flow.setValue(11)
            panel.beads_flow.setValue(22)
            panel.run_time.setValue(123)
            panel.load_snapshot({"flow_checks": [{"channel": "Oil L", "samples": []}],
                                 "dispense_checks": [{"channel": "Oil L", "weights_g": []}]})
            self.window._save_project()
        saved = self.backend.session.metadata["qt_checkup"]
        self.assertNotIn("flow_checks", saved)
        self.assertNotIn("dispense_checks", saved)
        self.assertEqual(saved["conditions"]["layout"]["channels"][1]["flow_ul_min"], 11)
        self.assertEqual(saved["conditions"]["layout"]["channels"][2]["flow_ul_min"], 22)
        self.window._load_project_path(Path(self.backend.workdir))
        self.window._select_stage(ids.index("preflight"))
        panel = self.window._ensure_preflight()
        self.assertEqual(panel._segment_inputs["Oil L"][0][0].value(), 37)
        self.assertEqual(panel.cells_flow.value(), 11)
        self.assertEqual(panel.beads_flow.value(), 22)
        self.assertEqual(panel.run_time.value(), 123)
        self.assertEqual(panel.pressure_limit.value(), 2500)

    def test_templates_are_editable_json_not_fixed_stages(self):
        self.window._select_stage(self.experiment_index)
        self.assertEqual(
            [self.panel.library.itemText(i) for i in range(self.panel.library.count())],
            ["Select protocol…", "dead volume", "density", "dropseq", "flow stability scout", "gravimetry", "viscosity"],
        )
        for name in ("gravimetry", "dead_volume", "viscosity", "density", "flow_stability_scout", "dropseq"):
            self.panel.library.setCurrentIndex(self.panel.library.findData("@" + name))
            with patch.object(self.backend.engine, "run", side_effect=AssertionError("template actuated")):
                self.panel.build_plan()
                self.drain()
            self.assertEqual(self.panel.plan["operation_id"], "run_json_protocol")
            self.assertEqual(self.panel.document()["name"], self.panel.templates[name]["name"])
            self.assertTrue(any(step["confirmation"] for step in self.panel.plan["steps"]))
            self.assertTrue(all(s["on_complete"] == "zero" for s in self.panel.plan["steps"]))
        document = self.panel.document()
        document["name"] = "my_dropseq"
        document["steps"][0]["sensor_setpoints"]["0"] = 25
        self.panel.set_document(document)
        self.assertFalse(self.panel.executable)
        stored = self.panel.document()
        self.assertEqual(stored["steps"][0]["sensor_setpoints"]["0"], 25)
        self.assertEqual(self.panel.templates["dropseq"]["steps"][0]["sensor_setpoints"]["0"], 300)

    def test_calculation_references_require_explicit_choice_and_show_results(self):
        from tests.test_metrology import archive
        from admet.workflows.calculations import calculate_run

        calibration = archive(self.backend.workdir, "gravimetry")
        ref = calculate_run(calibration, "gravimetry")
        directory = archive(self.backend.workdir, "dead_volume")
        import json
        from admet.core.protocol_store import write_json
        path = directory / "measurements.json"
        data = json.loads(path.read_text())
        data["values"]["flow_multiplier"] = None
        write_json(path, data)
        index = next(i for i, s in enumerate(self.window.workflow.stages) if s.id == "calculations")
        self.window._select_stage(index)
        panel = self.window._calculations
        self.drain(lambda: not panel.tasks.busy)
        panel.runs.setCurrentIndex(panel.runs.findData(str(directory)))
        self.assertEqual(panel.calculation.currentData(), "dead_volume")
        box = panel.references["calibration"]
        self.assertIsNone(box.currentData())
        self.assertFalse(panel.calculate_button.isEnabled())
        reference_index = next(i for i in range(1, box.count())
                               if Path(box.itemData(i)).resolve() == Path(ref["path"]).resolve())
        box.setCurrentIndex(reference_index)
        self.assertTrue(panel.calculate_button.isEnabled(), panel.status.text())
        panel.calculate()
        self.drain(lambda: not panel.tasks.busy)
        self.assertIn("Effective displacement volumes by flow", panel.output.toPlainText())
        self.assertIn("TIMING UNCERTAINTY", panel.output.toPlainText())

    def test_density_template_and_persistent_calculations_section(self):
        from tests.test_calculations import archived_density

        archived_density(self.backend.workdir)
        self.window._select_stage(self.experiment_index)
        self.panel.library.setCurrentIndex(self.panel.library.findData("@density"))
        with patch.object(self.backend.engine, "run", side_effect=AssertionError("actuation")):
            self.panel.build_plan()
            self.drain()
        self.assertEqual(self.panel.plan["expected_duration_s"], 540)
        self.assertIn("5 cm ABOVE", self.panel.plan["steps"][1]["confirmation"])
        self.assertNotIn("temperature", self.panel.editor.toPlainText())
        self.assertEqual(self.panel.document()["calculations"][0]["type"], "oil_density")
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
        self.assertNotIn("qt_protocol_stages", self.backend.session.metadata)
        with patch.object(self.backend.engine, "run", side_effect=AssertionError("planning actuated")):
            self.panel.build_plan()
            self.drain()
        self.assertEqual(self.panel.table.rowCount(), 1)
        self.assertEqual(self.panel.table.item(0, 2).text(), "0\n1\n2")
        self.assertEqual(self.panel.table.item(0, 4).text(), "10 µL/min\n5 µL/min\n5 µL/min")
        self.assertIn("time: 0.15 s", self.panel.table.item(0, 5).text())
        self.assertEqual(self.window._protocol_status_label.text(),
                         "Experiment 1: 1 step · ETA 0.15 s · Oil: 0.025 µL · Cells: 0.0125 µL · Beads: 0.0125 µL")
        self.assertFalse(hasattr(self.panel, "summary"))
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

    def test_action_plan_summary_counts_expanded_parallel_steps(self):
        self.window._select_stage(self.experiment_index)
        document = definition(60)
        document["steps"][0]["repeat"] = 2
        self.panel.set_document(document)
        self.panel.build_plan()
        self.drain()
        self.assertEqual(self.window._protocol_status_label.text(),
                         "Experiment 1: 2 steps · ETA 2 min · Oil: 20 µL · Cells: 10 µL · Beads: 10 µL")
        self.panel.edited()
        self.assertEqual(self.window._protocol_status_label.text(), "Experiment 1: ready to plan")

    def test_plan_summary_handles_held_flows_and_unknown_estimates(self):
        from admet.ui.protocols import plan_summary

        document = definition(60)
        first = document["steps"][0]
        first["sensor_setpoints"] = {"0": 10}
        first["on_complete"] = "hold"
        document["steps"].append({"sensor_setpoints": {"1": 5}, "trigger_type": "time",
                                  "trigger_params": {"duration_s": 60}, "on_complete": "zero"})
        plan = self.backend.preview("summary", document)
        self.assertEqual(plan_summary(plan, "Experiment 1"),
                         "Experiment 1: 2 steps · ETA 2 min · Oil: 20 µL · Cells: 5 µL · Beads: 0 µL")
        plan["steps"][1]["confirmation"] = "Change collection tube"
        self.assertIn("Oil: —", plan_summary(plan, "Experiment 1"))
        plan["steps"][1]["confirmation"] = None
        plan["steps"][1]["trigger_type"] = "stability"
        self.assertIn("ETA — · Oil: — · Cells: — · Beads: 0 µL", plan_summary(plan, "Experiment 1"))
        plan["steps"][1]["trigger_type"] = "time"
        plan["steps"][1]["pressure_setpoints_mbar"] = {"1": 20}
        plan["steps"][1]["flow_setpoints_ul_min"] = {}
        self.assertIn("Oil: 20 µL · Cells: —", plan_summary(plan, "Experiment 1"))
        document = definition()
        document["steps"][0].update(trigger_type="volume", trigger_params={"sensor_index": 0, "target_volume_ul": 50},
                                     timeout_s=600)
        plan = self.backend.preview("summary", document)
        self.assertEqual(plan_summary(plan, "Experiment 1"),
                         "Experiment 1: 1 step · ETA 5 min · Oil: 50 µL · Cells: 25 µL · Beads: 25 µL")

    def test_editor_changes_disable_execution_of_old_preview(self):
        self.panel.set_document(definition())
        self.panel.build_plan()
        self.drain()
        self.assertTrue(self.panel.executable)
        self.panel.editor.insertPlainText(" ")
        self.assertFalse(self.panel.executable)

    def test_plan_gates_are_clear_and_instructions_are_folded(self):
        from admet.ui.tables import SummaryLabel

        self.window._select_stage(self.experiment_index)
        document = definition()
        document["steps"].append({
            "name": "Weigh Oil L", "trigger_type": "time", "trigger_params": {"duration_s": 0},
            "confirm_message": "Weigh the tube and record empty and full masses", "on_complete": "zero",
        })
        self.panel.set_document(document)
        self.panel.build_plan()
        self.drain()
        self.assertEqual(self.panel.table.rowCount(), 2)
        self.assertEqual(self.panel.table.item(1, 3).text(), "confirm")
        self.assertEqual(self.panel.table.item(1, 5).text(), "Operator: Weigh Oil L")
        self.assertEqual(self.panel.table.item(0, 7).text(), "Before")
        self.assertFalse(self.panel.details_box.isVisible())
        self.panel.table.setCurrentCell(1, 0)
        self.assertTrue(self.panel.details_box.isVisible())
        self.assertIsInstance(self.panel.details, SummaryLabel)
        self.assertIn("record empty and full masses", self.panel.details.text())
        self.assertIn("Timeout:", self.panel.details.text())
        self.assertEqual(self.panel.plan["steps"][1]["trigger_type"], "time")
        self.assertEqual(self.panel.plan["steps"][1]["trigger_params"]["duration_s"], 0)
        self.panel.table.setCurrentCell(0, 0)
        self.assertNotIn("record empty and full masses", self.panel.details.text())
        self.assertIn("Step 1", self.panel.details.text())
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
        self.assertIn(f"for {STABILITY_DURATION_S:g} s", self.panel.table.item(0, 5).text())

    def test_plan_table_fits_all_rows_and_reflows_without_nested_scroll(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QTableWidgetItem

        from admet.ui.protocols import PlanTable

        table = PlanTable()
        try:
            table.setHorizontalHeaderLabels([
                "STEP", "STATUS", "UNIT ID", "TYPE", "TARGET", "TRIGGER / ETA", "END", "CONFIRM",
            ])
            table.setRowCount(30)
            for row in range(30):
                for column, value in enumerate([
                    str(row + 1), "pending", "0", "pressure", "1800 mbar",
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
        self.panel.library.setCurrentIndex(self.panel.library.findData("@flow_stability_scout"))
        self.drain()
        self.assertEqual(self.panel.document()["name"], "flow_stability_scout")
        self.panel.build_plan()
        self.drain()
        self.assertTrue(self.panel.table.isVisible())
        self.window._select_stage(0)
        self.window._select_stage(self.experiment_index)
        self.app.processEvents()
        self.assertTrue(self.panel.library.isVisible())
        self.assertTrue(self.panel.table.isVisible())

    def test_custom_protocol_edits_and_toc_are_memory_only(self):
        self.window._select_stage(self.experiment_index)
        self.panel.set_document(definition())
        stage = self.window._add_protocol_stage("Follow-up")
        second = self.window._protocol_editor(stage)
        document = definition()
        document["name"] = "follow_up"
        second.set_document(document)
        self.window._save_project()
        self.assertNotIn("qt_protocol_stages", self.backend.session.metadata)
        path = Path(self.backend.workdir)
        self.window._load_project_path(path)
        middle = [s for s in self.window.workflow.stages if "json_protocol" in s.features
                  and not s.settings_options.get("builtin")]
        self.assertEqual([s.id for s in middle], ["experiment_1"])
        self.assertEqual(self.window._protocol_editor(middle[0]).editor.toPlainText(), "")
        self.assertFalse((path / "protocols").exists())

    def test_editor_changes_and_plan_do_not_write_project_files(self):
        from tests.test_protocol_parameters import parameter_protocol

        root = Path(self.backend.workdir)
        before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        with patch.object(self.backend.service.project, "save", side_effect=AssertionError("draft saved")):
            self.panel.set_document(parameter_protocol())
            self.panel.set_parameter("oil_base_flow", 10)
            self.panel.build_plan()
            self.drain()
            self.assertIsNotNone(self.panel.plan)
            self.panel.editor.insertPlainText(" ")
            self.window._autosave_measurements()
        after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_old_draft_metadata_is_not_restored(self):
        self.backend.service.project.update_metadata(qt_protocol_stages=[{
            "id": "old_step", "label": "Old draft", "json": '{"unfinished":',
        }])
        self.window._load_project_path(Path(self.backend.workdir))
        self.assertNotIn("old_step", {stage.id for stage in self.window.workflow.stages})

    def test_template_selector_and_poll_do_not_use_project_protocol_library(self):
        def call(operation, settings=None):
            self.assertNotIn(operation, {"save_protocol", "list_protocols"})
            return original(operation, settings)

        original = self.backend.call
        with patch.object(self.backend, "call", side_effect=call):
            self.panel.library.setCurrentIndex(self.panel.library.findData("@flow_stability_scout"))
            self.window._last_status_poll = 0
            self.window._poll()
            self.drain()
        self.assertTrue(all(self.panel.library.itemData(i).startswith("@")
                            for i in range(1, self.panel.library.count())))
        self.assertFalse((Path(self.backend.workdir) / "protocols").exists())

    def _failing_command(self, **kwargs):
        self.backend.call("connect_fluidics")
        with patch.object(self.window, "_notify") as notify, \
                patch.object(self.backend, "run", side_effect=RuntimeError("controller refused")):
            self.window._run("stop_channel", {"channel_index": 0}, **kwargs)
            self.drain()
        return [call.args[0] for call in notify.call_args_list]

    def test_a_failed_command_tells_the_operator(self):
        messages = self._failing_command()
        self.assertTrue(any("controller refused" in m for m in messages), messages)

    def test_a_failed_background_apply_stays_quiet(self):
        messages = self._failing_command(raise_errors=False)
        self.assertFalse(any("controller refused" in m for m in messages), messages)

    def test_a_status_refresh_in_flight_does_not_refuse_a_command(self):
        # The refresh used to share the command slot, so a click landing during
        # it was refused with "Another command is in progress".
        release = threading.Event()
        entered = threading.Event()
        original = self.backend.call

        def slow_call(name, *args, **kwargs):
            if name == "planned_protocols":
                entered.set()
                release.wait(3)
            return original(name, *args, **kwargs)

        with patch.object(self.backend, "call", side_effect=slow_call):
            self.window._last_status_poll = 0.0
            self.window._poll()
            self.assertTrue(entered.wait(2))
            self.assertFalse(self.window.tasks.busy)
            self.assertTrue(self.window.tasks.submit(lambda: "done", lambda _: None, self.fail))
            release.set()
            self.drain(lambda: not self.window.status_tasks.busy and not self.window.tasks.busy)
        self.assertFalse(any("Another command is in progress" in e for e in self.window.log_entries))

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
        self.assertTrue(all(c.mode == "off" for c in self.backend.engine.channel_manager.channels))
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

    def test_manual_channel_fields_apply_on_enter_and_unlock_on_release(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from admet.ui.control import ChannelControlPanel

        self.backend.call("connect_fluidics")
        self.backend.call("apply_corrections")
        channels = self.backend.engine.channel_manager
        panel = ChannelControlPanel(channels.channels, on_flow=self.window._set_channel_flow,
                                    on_pressure=self.window._set_channel_pressure, on_stop=self.window._stop_channel)
        self.addCleanup(panel.close)
        panel.show()
        row = panel._rows[0]
        channels.pipeline_set_setpoint(0, 20)
        panel.update_modes(channels.channels)
        self.assertFalse(row.flow.isEnabled())
        self.assertFalse(row.pressure.isEnabled())
        channels.pipeline_release_all()
        panel.update_modes(channels.channels)
        self.assertTrue(row.flow.isEnabled())
        self.assertTrue(row.pressure.isEnabled())
        for editor, text, field, expected in ((row.flow, "12.5", "active_setpoint", 12.5),
                                              (row.pressure, "27", "pressure_setpoint", 27)):
            editor.setFocus()
            editor.selectAll()
            QTest.keyClicks(editor, text)
            self.assertEqual(getattr(channels.channels[0], field), 0)
            QTest.keyClick(editor, Qt.Key.Key_Return)
            self.drain()
            self.assertEqual(getattr(channels.channels[0], field), expected)
        row.stop_button.click()
        self.drain()
        self.assertEqual(channels.channels[0].mode, "off")
        self.assertEqual(channels.channels[0].pressure_setpoint, 0)
        self.assertTrue(all(c.mode == "off" for c in channels.channels[1:]))

    def test_window_close_waits_for_zero_and_disconnect(self):
        from PySide6.QtWidgets import QMessageBox

        self.backend.call("connect_fluidics")
        self.window.close()
        self.assertTrue(self.backend.service.state()["fluidics"])
        self.assertIn("Close ADMET?", self.window._confirmation_dialog.text())
        self.window._confirmation_dialog.button(QMessageBox.StandardButton.Ok).click()
        self.drain(lambda: self.window._shutdown_complete)
        self.assertFalse(self.backend.service.state()["fluidics"])

    def test_escape_clears_plan_selection_and_details_without_changing_plan(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        self.window._select_stage(self.experiment_index)
        self.panel.set_document(definition())
        self.panel.build_plan()
        self.drain()
        digest = self.panel.plan["digest"]
        self.panel.table.setCurrentCell(0, 0)
        self.panel.table.setFocus()
        self.assertTrue(self.panel.table.selectedItems())
        self.assertTrue(self.panel.details_box.isVisible())
        QTest.keyClick(self.panel.table, Qt.Key.Key_Escape)
        self.assertEqual(self.panel.table.selectedItems(), [])
        self.assertEqual(self.panel.table.currentRow(), -1)
        self.assertFalse(self.panel.table.hasFocus())
        self.assertFalse(self.panel.details_box.isVisible())
        self.assertEqual(self.panel.plan["digest"], digest)

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
        with patch.object(self.window, "_notify", side_effect=AssertionError("duplicate gate card")):
            self.window._poll_pipeline_events()
        self.assertIn("Confirm physical", self.window._pending_pipeline_confirmation())
        self.assertIsNone(self.window._confirmation_dialog)
        observed = self.backend.call("observe")
        self.assertTrue(all(c["requested_flow_ul_min"] in (None, 0) for c in observed["channels"]))
        from PySide6.QtWidgets import QLabel

        self.assertIsNone(self.window.notification)
        label = self.window.findChild(QLabel, "ProtocolConfirmLabel")
        self.assertIsNotNone(label)
        self.assertIn("Confirm physical", label.text())
        self.panel.control("confirm")
        self.drain()
        self.window._poll_pipeline_events()
        self.assertEqual(self.window._pending_pipeline_confirmation(), "")

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

    def test_json_remains_visible_but_draft_and_preview_are_discarded_on_reopen(self):
        from PySide6.QtWidgets import QPushButton

        self.window._select_stage(self.experiment_index)
        self.panel.library.setCurrentIndex(self.panel.library.findData("@flow_stability_scout"))
        self.assertEqual(self.panel.document()["name"], "flow_stability_scout")
        self.panel.build_plan()
        self.drain()
        self.assertTrue(self.panel.editor.isVisible())
        self.assertTrue(self.panel.parameter_table.isVisible())
        self.assertTrue(self.panel.table.isVisible())
        self.assertFalse({"Open", "Import JSON", "Save JSON", "Edit JSON"} &
                         {b.text() for b in self.panel.findChildren(QPushButton)})
        plan_id = self.panel.plan["plan_id"]
        self.panel.editor.setPlainText('{"unfinished":')
        self.window._load_project_path(Path(self.backend.workdir))
        stage = next(s for s in self.window.workflow.stages if s.id == "experiment_1")
        restored = self.window._protocol_editor(stage)
        self.assertEqual(restored.editor.toPlainText(), "")
        self.assertFalse(restored.executable)
        self.assertIsNone(restored.plan)
        self.assertNotIn(plan_id, {p["plan_id"] for p in self.backend.call("planned_protocols")["plans"]})
        self.assertFalse((Path(self.backend.workdir) / "plans").exists())

    def test_measurements_enable_only_for_run_and_survive_reopen(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QComboBox, QLabel
        from tests.test_run_measurements import measured_protocol

        self.backend.call("connect_fluidics")
        self.backend.call("apply_corrections")
        self.panel.set_document(measured_protocol())
        measurements = self.panel.measurements
        self.assertFalse(measurements.findChildren(QComboBox))
        self.assertFalse(measurements.findChildren(QLabel))
        self.assertTrue(measurements.table.verticalHeader().isHidden())
        self.assertEqual(measurements.table.horizontalHeader().defaultAlignment(),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.assertEqual(measurements.table.item(0, 0).textAlignment(),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.assertFalse(measurements.cells["before_mg"].isEnabled())
        self.panel.build_plan()
        self.drain()
        self.panel.execute()
        self.drain()
        run_id = self.panel.plan["run_id"]
        cell = measurements.cells["before_mg"]
        self.assertTrue(cell.isEnabled())
        cell.setText("12.5")
        cell.editingFinished.emit()
        self.window._autosave_measurements()
        self.assertEqual(self.backend.measurements(run_id)["values"]["before_mg"], 12.5)
        self.panel.control("abort")
        self.drain()
        self.window._load_project_path(Path(self.backend.workdir))
        stage = next(s for s in self.window.workflow.stages if s.id == "experiment_1")
        restored = self.window._protocol_editor(stage).measurements
        self.assertIsNone(restored.run_id)
        self.assertTrue(restored.isHidden())
        saved = self.backend.measurements(run_id)
        self.assertEqual(saved["values"]["before_mg"], 12.5)
        self.assertIsNone(saved["values"]["after_mg"])
        calculation_index = next(i for i, s in enumerate(self.window.workflow.stages) if "calculations" in s.features)
        self.window._select_stage(calculation_index)
        calculations = self.window._calculations
        self.drain(lambda: not calculations.tasks.busy)
        self.assertIn(run_id, [Path(calculations.runs.itemData(i)).name for i in range(calculations.runs.count())])

    def test_measurements_reset_for_new_plan_and_stay_bound_to_each_run(self):
        from tests.test_run_measurements import measured_protocol

        self.backend.call("connect_fluidics")
        self.backend.call("apply_corrections")
        self.panel.set_document(measured_protocol())
        measurements = self.panel.measurements
        ids = []
        for value in (12.5, 25.0):
            self.panel.build_plan()
            self.drain()
            self.assertIsNone(measurements.run_id)
            self.assertFalse(measurements.cells["before_mg"].isEnabled())
            self.assertEqual(measurements.cells["before_mg"].text(), "")
            self.panel.execute()
            self.drain()
            ids.append(measurements.run_id)
            self.assertEqual(measurements.cells["before_mg"].text(), "")
            self.panel.control("abort")
            self.drain()
            cell = measurements.cells["before_mg"]
            self.assertTrue(cell.isEnabled())
            cell.setText(str(value))
            cell.editingFinished.emit()
            self.window._autosave_measurements()
        self.assertNotEqual(*ids)
        self.assertEqual(self.backend.measurements(ids[0])["values"]["before_mg"], 12.5)
        self.assertEqual(self.backend.measurements(ids[1])["values"]["before_mg"], 25.0)
        self.assertIsNone(self.backend.measurements(ids[1])["values"]["after_mg"])

    def test_run_rows_show_gates_outcomes_and_respect_manual_browsing(self):
        from admet.engines.acquisition.pipeline import PipelineEvent, PipelineState, StepOutcome
        from PySide6.QtWidgets import QCheckBox
        from tests.test_protocol_parameters import parameter_protocol

        self.window._select_stage(self.experiment_index)
        document = parameter_protocol()
        document["steps"][0]["repeat"] = 2
        self.panel.set_document(document)
        self.panel.build_plan()
        self.drain()
        self.window._pipeline_stage_id = self.panel.stage.id
        self.assertEqual(self.panel.table.horizontalHeaderItem(1).text(), "STATUS")
        self.assertEqual(self.panel.table.item(0, 0).text(), "1")
        self.assertEqual(self.panel.table.item(0, 1).text(), "pending")
        self.assertFalse(any("Follow" in box.text() for box in self.panel.findChildren(QCheckBox)))

        def event(sequence, row, **kwargs):
            self.panel.pipeline_event(PipelineEvent(
                state=PipelineState.RUNNING, current_step=row, total_steps=2,
                step_name="point", sequence=sequence, **kwargs))

        with patch.object(self.window.page_scroll, "ensureVisible") as follow:
            event(1, 0, confirmation_message="Position vessel")
            self.assertEqual(self.panel.table.item(0, 1).text(), "confirm")
            self.assertTrue(self.panel.editor.isReadOnly())
            self.assertFalse(self.panel.editor.isVisible())
            self.assertTrue(self.panel.parameter_table.isVisible())
            self.assertTrue(self.panel.table.isVisible())
            self.assertFalse(self.panel.library.isEnabled())
            self.assertFalse(self.panel.parameter_editors["oil_base_flow"].isEnabled())
            follow.assert_not_called()
            event(2, 0, progress=0.25)
            self.assertIn("25%", self.panel.table.item(0, 1).text())
            follow.assert_not_called()
            self.panel.table.cellClicked.emit(0, 0)
            event(3, 0, outcome=StepOutcome.SKIPPED)
            event(4, 1, progress=0.5)
            follow.assert_not_called()
            self.assertEqual(self.panel.table.item(0, 1).text(), "skipped")
            self.assertIn("50%", self.panel.table.item(1, 1).text())
            event(5, 1, outcome=StepOutcome.COMPLETED)
            self.panel.pipeline_event(PipelineEvent(
                state=PipelineState.COMPLETED, current_step=1, total_steps=2,
                sequence=6, outcome=StepOutcome.COMPLETED))
            self.assertEqual(self.panel.table.item(1, 1).text(), "completed")
            self.assertEqual(self.panel.table.item(0, 0).text(), "1")
            self.assertEqual(self.panel.table.item(1, 0).text(), "2")
            self.assertFalse(self.panel.editor.isReadOnly())
            self.assertFalse(self.panel.editor.isVisible())
            self.assertTrue(self.panel.parameter_editors["oil_base_flow"].isEnabled())
            self.panel.set_parameter("oil_base_flow", 6)
            self.assertTrue(self.panel.editor.isVisible())


    def test_action_progress_shows_percentages_without_repeating_status(self):
        from admet.engines.acquisition.pipeline import PipelineEvent, PipelineState

        self.window._select_stage(self.experiment_index)
        stage = self.panel.stage
        self.window._pipeline_stage_id = stage.id
        for state, confirmation, status, expected in (
            (PipelineState.RUNNING, "", "Running: Flow scout pass 1", "Step 25% / Total 31%"),
            (PipelineState.RUNNING, "Confirm setup", "Waiting: Flow scout pass 1", "Step 25% / Total 31%"),
            (PipelineState.PAUSED, "", "Paused: Flow scout pass 1", "Step 25% / Total 31%"),
            (PipelineState.PAUSED, "Confirm setup", "Paused: Flow scout pass 1", "Step 25% / Total 31%"),
            (PipelineState.STOPPING, "", "Stopping: Flow scout pass 1", "Step 25% / Total 31%"),
            (PipelineState.COMPLETED, "", "Protocol complete", "Step 100% / Total 100%"),
            (PipelineState.ERROR, "", "Protocol error", "Step 0% / Total 0%"),
        ):
            with self.subTest(state=state, confirmation=confirmation):
                self.window._latest_pipeline_event = PipelineEvent(
                    state=state, current_step=1, total_steps=4, progress=0.25,
                    step_name="Flow scout pass 1", confirmation_message=confirmation,
                )
                self.window._sync_action_box(stage)
                self.assertEqual(self.window._protocol_status_label.text(), status)
                self.assertEqual(self.window._protocol_progress_bar.format(), expected)
        self.window._pipeline_stage_id = "another_experiment"
        self.window._sync_action_box(stage)
        self.assertEqual(self.window._protocol_progress_bar.format(), "Not running")

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
