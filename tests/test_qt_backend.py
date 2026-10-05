import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from admet.core.run import RunJob
from admet.ui.backend import DesktopBackend
from admet.workflows.json_protocol import loads, template_documents


def definition(duration=0.15):
    return {
        "name": "desktop_test",
        "steps": [{
            "sensor_setpoints": {"0": 10, "1": 5, "2": 5},
            "trigger_type": "time", "trigger_params": {"duration_s": duration},
            "confirm_message": "Confirm physical Oil L / Cells M1 / Beads M2 mapping",
            "on_complete": "zero",
        }],
    }


class DesktopBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = DesktopBackend(simulated=True)
        self.addCleanup(self.backend.shutdown)
        self.backend.create_project(Path(self.tmp.name) / "desktop.admetp")

    def connect(self):
        self.backend.call("connect_fluidics")
        self.backend.call("apply_corrections")

    def test_calibration_is_kept_in_the_project(self):
        saved = {"cells_m": {"profile": "water_m", "liquids": {"water_m": {
            "calibration": "H2O", "scale": 1.07, "offset": 0.0, "quadratic": 0.0, "updated_at": "2026-10-02T20:00:00"}}}}
        self.backend.save_calibration(saved)

        reopened = DesktopBackend(simulated=True)
        self.addCleanup(reopened.shutdown)
        reopened.open_project(Path(self.tmp.name) / "desktop.admetp")
        self.assertEqual(reopened.calibration, saved)

    def test_added_liquids_are_kept_in_the_project(self):
        entry = self.backend.add_liquid("dSurf", "M", "H2O", 1.0, 1.1)

        reopened = DesktopBackend(simulated=True)
        self.addCleanup(reopened.shutdown)
        reopened.open_project(Path(self.tmp.name) / "desktop.admetp")
        self.assertEqual(reopened.liquids, [entry])
        self.assertEqual(self.backend.add_liquid("dSurf", "M", "H2O", 1.0)["id"], "dsurf_m_2")

    def test_a_project_liquid_is_edited_in_place(self):
        entry = self.backend.add_liquid("New liquid", "M", "H2O", 1.0, 1.0)
        edited = self.backend.update_liquid(entry["id"], name="dSurf", density=1.02)

        self.assertEqual((edited["id"], edited["name"], edited["density"]), (entry["id"], "dSurf", 1.02))
        self.assertEqual(self.backend.liquids, [edited])
        with self.assertRaises(ValueError):
            self.backend.update_liquid(entry["id"], density=0)
        self.backend.save_calibration({"cells_m": {"profile": entry["id"], "liquids": {}}})
        with self.assertRaisesRegex(ValueError, "in use"):
            self.backend.update_liquid(entry["id"], unit="L")    # Cells M is an M channel

    def test_preflight_save_replaces_the_checkup_and_keeps_other_metadata(self):
        from dataclasses import replace

        project = self.backend.service.project
        project.session = replace(project.session, metadata={"qt_checkup": {"flow_checks": []}, "note": "retain"})
        checkup = {"conditions": {"flows_ul_min": {"oil": 20}}}
        self.backend.save_project(checkup=checkup)
        self.backend.open_project(project.path)
        saved = self.backend.session.metadata
        self.assertEqual(saved["note"], "retain")
        self.assertEqual(saved["qt_checkup"], checkup)

    def test_corrected_range_is_refreshed_before_planning_scout_base_15(self):
        from admet.workflows.json_protocol import template_documents

        self.backend.call("connect_fluidics")
        self.backend.call("apply_corrections", {"cells_m_calibration": "IPA", "cells_m_scale": 2.25})
        reported = self.backend.engine.sdk.get_sensor_channels_info()[1].smax
        cached = self.backend.engine.hardware.state.sensor_channels[1].smax
        self.assertEqual(cached, reported)
        self.assertGreaterEqual(cached, 135)
        doc = template_documents()["flow_stability_scout"]
        doc["parameter_values"]["oil_base_flow"] = 15
        with patch.object(self.backend.engine.sdk, "get_sensor_channels_info", side_effect=AssertionError("planning read SDK")):
            plan = self.backend.preview("scout", doc)
        self.assertEqual(max(s["flow_setpoints_ul_min"].get("1", 0) for s in plan["steps"]), 135)
        doc["parameter_values"]["oil_base_flow"] = reported
        with self.assertRaisesRegex(ValueError, "Step .*target .*exceeds detected range maximum"):
            self.backend.preview("scout", doc)

    def test_failed_range_refresh_does_not_leave_old_range_available(self):
        self.connect()
        with patch.object(self.backend.engine.sdk, "get_sensor_channels_info", side_effect=RuntimeError("SDK unavailable")):
            with self.assertRaisesRegex(RuntimeError, "SDK unavailable"):
                self.backend.call("apply_corrections")
        self.assertTrue(all(s.smax is None for s in self.backend.engine.hardware.state.sensor_channels))

    def plan(self, document=None):
        return self.backend.call("plan_protocol", {
            "operation_id": "run_json_protocol", "settings": {"protocol": document or definition()},
        })

    def wait_completed(self, plan_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            plan = self.backend.call("planned_protocols", {"plan_id": plan_id})["plans"][0]
            if plan["state"] == "completed":
                return plan
            time.sleep(0.01)
        self.fail(f"Protocol did not finish: {plan}")

    def test_standalone_json_plan_gate_execute_archive_and_reopen(self):
        self.connect()
        saved = self.backend.call("save_protocol", {"protocol": definition()})
        plan = self.backend.plan_file(saved["path"])
        observed = self.backend.call("observe")
        self.assertFalse(observed["recording"]["active"])
        self.assertEqual(observed["protocol"]["state"], "idle")
        self.assertEqual({c["mode"] for c in observed["channels"]}, {"off"})
        result = self.backend.call("control_protocol", {
            "action": "execute", "plan_id": plan["plan_id"], "timeout_s": 1,
        })
        self.assertEqual(result["yield"]["reason"], "confirmation_required")
        observed = self.backend.call("observe")
        self.assertTrue(all(c["requested_flow_ul_min"] in (None, 0) for c in observed["channels"]))
        self.backend.call("control_protocol", {"action": "confirm", "timeout_s": 1})
        completed = self.wait_completed(plan["plan_id"])
        self.assertEqual(plan["digest"], completed["digest"])
        observed = self.backend.call("observe")
        self.assertFalse(observed["recording"]["active"])
        self.assertTrue(all(c["requested_flow_ul_min"] in (None, 0) for c in observed["channels"]))
        events = self.backend.call("protocol_events")["events"]
        self.assertEqual([e["sequence"] for e in events], sorted({e["sequence"] for e in events}))
        path = Path(self.backend.workdir) / "records" / "protocols" / completed["run_id"]
        summary = json.loads((path / "summary.json").read_text())
        self.assertEqual(summary["state"], "completed")
        self.assertTrue((Path(self.backend.workdir) / summary["artifacts"]["fluidics_csv"]).is_file())
        with self.assertRaisesRegex(RuntimeError, "completed"):
            self.backend.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"]})
        self.backend.shutdown()
        reopened = DesktopBackend(simulated=True)
        self.addCleanup(reopened.shutdown)
        reopened.open_project(self.backend.workdir)
        self.assertEqual(reopened.call("list_protocols")["protocols"][0]["name"], "desktop_test")
        self.assertEqual(reopened.call("planned_protocols")["plans"], [])
        self.assertTrue(reopened.service.project.session.files)

    def test_typed_parameters_are_frozen_in_executed_run(self):
        from tests.test_protocol_parameters import typed_parameter_protocol
        from admet.workflows.json_protocol import normalize

        self.connect()
        document = normalize(typed_parameter_protocol())
        document["parameter_values"].update(oil="EvaGreen mix", filtered=False)
        document["steps"][0]["trigger_params"]["duration_s"] = 0.1
        plan = self.plan(document)
        document["parameter_values"].update(oil="different oil", filtered=True)
        self.backend.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"], "timeout_s": 1})
        self.backend.call("control_protocol", {"action": "confirm", "timeout_s": 1})
        completed = self.wait_completed(plan["plan_id"])
        directory = Path(self.backend.workdir) / "records" / "protocols" / completed["run_id"]
        archived = json.loads((directory / "protocol.json").read_text())
        self.assertEqual(archived["parameter_values"]["oil"], "EvaGreen mix")
        self.assertIs(archived["parameter_values"]["filtered"], False)
        self.assertEqual(archived["parameter_values"]["finish"], "zero")
        self.assertEqual(archived["parameter_values"]["offset"], -1)

    def test_dropseq_template_simulated_collection_gates_and_artifacts(self):
        self.connect()
        document = template_documents()["dropseq"]
        document["steps"][0]["trigger_params"]["target_volume_ul"] = 5
        document["steps"][0]["confirm_message"] = "Collect a 5 uL test aliquot?"
        saved = self.backend.call("save_protocol", {"protocol": document})
        plan = self.backend.plan_file(saved["path"])
        observed = self.backend.call("observe")
        self.assertFalse(observed["recording"]["active"])
        self.assertEqual({c["mode"] for c in observed["channels"]}, {"off"})
        started = self.backend.call("control_protocol", {
            "action": "execute", "plan_id": plan["plan_id"], "timeout_s": 1,
        })
        self.assertEqual(started["yield"]["reason"], "confirmation_required")
        self.backend.call("control_protocol", {"action": "confirm", "timeout_s": 0.1})
        deadline = time.monotonic() + 10
        running_targets_seen = False
        while time.monotonic() < deadline:
            observed = self.backend.call("observe")
            running_targets_seen |= [c["requested_flow_ul_min"] for c in observed["channels"]] == [300, 40, 40]
            if observed["protocol"]["confirmation_message"] == document["steps"][1]["confirm_message"]:
                break
            time.sleep(0.05)
        else:
            self.fail("simulated Drop-Seq did not reach the completion gate")
        self.assertTrue(running_targets_seen)
        self.assertTrue(all(c["requested_flow_ul_min"] in (None, 0) for c in observed["channels"]))
        self.assertTrue(observed["recording"]["active"])
        self.backend.call("control_protocol", {"action": "confirm", "timeout_s": 1})
        completed = self.wait_completed(plan["plan_id"])
        self.assertFalse(self.backend.call("observe")["recording"]["active"])
        directory = Path(self.backend.workdir) / "records" / "protocols" / completed["run_id"]
        summary = json.loads((directory / "summary.json").read_text())
        self.assertEqual(json.loads((directory / "protocol.json").read_text()), document)
        self.assertTrue((Path(self.backend.workdir) / summary["artifacts"]["fluidics_csv"]).is_file())
        events = self.backend.call("protocol_events", {"limit": 1000})["events"]
        completed = next(e for e in events if e["step_name"] == "Run set01_rep01" and e["outcome"] == "completed")
        self.assertGreaterEqual(float(completed["step_volumes"][0]), 5)

    def test_planning_never_invokes_engine_run(self):
        self.connect()
        with patch.object(self.backend.engine, "run", side_effect=AssertionError("hardware call")):
            plan = self.plan()
        self.assertEqual(plan["state"], "planned")

    def test_acquisition_mode_persists_and_invalidates_unexecuted_preview(self):
        self.assertEqual(self.backend.acquisition_mode, "fluidics_only")
        first = self.backend.preview("experiment_1", definition())
        self.assertFalse(first["camera_required"])
        self.backend.set_acquisition_mode("camera_fluidics")
        with self.assertRaises(LookupError):
            self.backend.call("planned_protocols", {"plan_id": first["plan_id"]})
        plan = self.backend.preview("experiment_1", definition())
        self.assertTrue(plan["recording"]["include_video"])
        self.assertIn("camera_live", plan["unmet_guards"])
        self.backend.open_project(self.backend.workdir)
        self.assertEqual(self.backend.acquisition_mode, "camera_fluidics")

    def test_combined_run_uses_existing_recorder_and_archives_video(self):
        import numpy as np
        from tests.test_control_engine import FakeCameraAcquisition, FakeVideoWriter

        self.connect()
        self.backend.engine._recordings._writer_factory = FakeVideoWriter
        camera = FakeCameraAcquisition()
        self.backend.engine._camera._acquisition = camera
        self.addCleanup(setattr, self.backend.engine._camera, "_acquisition", None)
        self.backend.engine._camera._on_frame(np.zeros((12, 16), dtype=np.uint8))
        self.backend.set_acquisition_mode("camera_fluidics")
        plan = self.backend.preview("experiment_1", definition())
        self.assertEqual(plan["unmet_guards"], [])
        with patch.object(camera, "is_alive", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "start Live"):
                self.backend.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"]})
        self.assertFalse(self.backend.engine.recording_active)
        self.backend.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"], "timeout_s": 1})
        self.assertTrue(camera.recording)
        with self.assertRaises(RuntimeError):
            self.backend.set_acquisition_mode("fluidics_only")
        self.backend.call("control_protocol", {"action": "confirm", "timeout_s": 1})
        completed = self.wait_completed(plan["plan_id"])
        summary = json.loads((Path(self.backend.workdir) / "records/protocols" / completed["run_id"] / "summary.json").read_text())
        self.assertTrue(summary["normalized_settings"]["include_video"])
        self.assertTrue((Path(self.backend.workdir) / summary["artifacts"]["video_path"]).is_file())
        self.assertTrue((Path(self.backend.workdir) / summary["artifacts"]["fluidics_csv"]).is_file())
        self.assertFalse(camera.recording)

    def test_camera_start_failure_never_starts_protocol(self):
        from tests.test_control_engine import FakeCameraAcquisition, FakeVideoWriter

        self.connect()
        camera = FakeCameraAcquisition()
        self.backend.engine._camera._acquisition = camera
        self.addCleanup(setattr, self.backend.engine._camera, "_acquisition", None)
        self.backend.engine._recordings._writer_factory = FakeVideoWriter
        self.backend.set_acquisition_mode("camera_fluidics")
        plan = self.backend.preview("experiment_1", definition())
        with patch.object(camera, "start_recording", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "Failed to start camera"):
                self.backend.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"]})
        self.assertFalse(self.backend.engine.recording_active)
        self.assertEqual(self.backend.engine.pipeline_state, "idle")
        self.assertTrue(all(c.mode == "off" for c in self.backend.engine.channel_manager.channels))

    def test_one_transient_preview_per_scope_and_run_id_only_on_execution(self):
        self.connect()
        first = self.backend.preview("experiment_1", definition())
        for _ in range(5):
            current = self.backend.preview("experiment_1", definition())
        self.assertEqual(len(self.backend.call("planned_protocols")["plans"]), 1)
        self.assertNotIn("run_id", current)
        self.assertFalse((Path(self.backend.workdir) / "plans").exists())
        self.assertFalse((Path(self.backend.workdir) / "records" / "protocols").exists())
        with self.assertRaises(LookupError):
            self.backend.call("control_protocol", {"action": "execute", "plan_id": first["plan_id"]})
        result = self.backend.call("control_protocol", {
            "action": "execute", "plan_id": current["plan_id"], "timeout_s": 1})
        self.assertTrue(result["run_id"].startswith("run_"))
        self.assertNotEqual(result["run_id"], current["plan_id"])
        self.backend.call("control_protocol", {"action": "confirm", "timeout_s": 1})
        completed = self.wait_completed(current["plan_id"])
        self.assertEqual(completed["run_id"], result["run_id"])
        self.assertTrue((Path(self.backend.workdir) / "records" / "protocols" / result["run_id"] / "summary.json").exists())

    def test_density_completion_closes_recording_without_running_calculations(self):
        from tests.test_fluid_density import recorded_density

        document, csv_path, events_path, origin = recorded_density(self.backend.workdir)
        plan = self.plan(document)
        service = self.backend.service
        stored = service._protocol_plans[plan["plan_id"]]
        stored["run_id"] = "run_" + "1" * 32
        store = service._plan_store(plan["plan_id"])
        directory = store.begin(stored)
        (directory / "events.jsonl").write_text(events_path.read_text())
        service._run_artifacts[plan["plan_id"]] = {
            "directory": directory, "recording": True,
            "artifacts": {"fluidics_csv": str(csv_path), "polling_origin_monotonic": origin},
        }
        stored["state"] = "completed"
        with patch.object(service, "do") as stop, patch(
            "admet.workflows.fluid_density.analyze_density_run", side_effect=AssertionError("automatic analysis"),
        ):
            service._finish_plan(stored)
        stop.assert_called_once_with("stop_recording")
        published = self.backend.call("planned_protocols", {"plan_id": plan["plan_id"]})["plans"][0]
        self.assertNotIn("analysis_result", published)
        summary = json.loads((directory / "summary.json").read_text())
        self.assertTrue(summary["artifacts"]["recording_closed"])
        self.assertEqual(summary["artifacts"]["polling_origin_monotonic"], origin)
        self.assertNotIn(plan["plan_id"], service._run_artifacts)

    def test_density_mocked_fast_run_uses_native_gates_and_rejects_short_recording(self):
        self.connect()
        channels = self.backend.engine.channel_manager
        channels.user_set_pressure(0, 13)
        channels.user_set_pressure(2, 17)
        plan = self.plan(template_documents()["density"])
        # Only the clock trigger is accelerated; device access remains simulated.
        from admet.engines.acquisition import triggers

        with patch.object(triggers.TimeTrigger, "check", return_value=True), patch.object(
            channels._sdk, "set_pressure", wraps=channels._sdk.set_pressure,
        ) as set_pressure:
            self.backend.call("control_protocol", {
                "action": "execute", "plan_id": plan["plan_id"], "timeout_s": 0.1,
            })
            gates, last_gate = [], None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                observed = self.backend.call("observe")
                protocol = observed["protocol"]
                if protocol["confirmation_message"] and protocol["step_index"] != last_gate:
                    last_gate = protocol["step_index"]
                    gates.append(last_gate)
                    self.assertEqual(observed["channels"][1]["requested_flow_ul_min"], 0)
                    self.assertEqual(observed["channels"][1]["mode"], "flow")
                    self.assertEqual([observed["channels"][i]["requested_pressure_mbar"] for i in (0, 2)], [13, 17])
                    self.backend.call("control_protocol", {"action": "confirm", "timeout_s": 0.1})
                completed = self.backend.call("planned_protocols", {"plan_id": plan["plan_id"]})["plans"][0]
                if completed["state"] == "completed":
                    break
                time.sleep(0.01)
            else:
                self.fail("accelerated density run did not finish")
            set_pressure.assert_not_called()
        self.assertEqual(len(gates), 6)
        self.assertNotIn("analysis_result", completed)
        from admet.workflows.calculations import calculate_run

        directory = Path(self.backend.workdir) / "records" / "protocols" / completed["run_id"]
        calculation = calculate_run(directory, "fluid_density")
        self.assertEqual(calculation["result"]["status"], "inconclusive")
        self.assertIsNone(calculation["result"]["density_g_ml"])
        observed = self.backend.call("observe")
        self.assertFalse(observed["recording"]["active"])
        self.assertEqual(observed["channels"][1]["requested_pressure_mbar"], 0)
        self.assertEqual([observed["channels"][i]["requested_pressure_mbar"] for i in (0, 2)], [13, 17])

    def test_stale_cancelled_and_direct_start_refused(self):
        self.connect()
        plan = self.plan()
        self.backend.call("apply_corrections", {"oil_l_scale": 2})
        with self.assertRaisesRegex(RuntimeError, "stale"):
            self.backend.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"]})
        plan = self.plan()
        self.backend.call("cancel_protocol_plan", {"plan_id": plan["plan_id"]})
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.backend.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"]})
        with self.assertRaisesRegex(RuntimeError, "review"):
            self.backend.call("run_json_protocol", {"protocol": definition()})

    def test_simulation_fence_and_camera_nulls(self):
        self.backend.call("connect_fluidics", {"simulated": False})
        self.assertTrue(self.backend.engine.hardware.state.simulated)
        for action in ("connect_camera", "list_cameras"):
            with self.assertRaisesRegex(RuntimeError, "disabled in simulation"):
                self.backend.run(RunJob("test", "acquisition", action))
        observed = self.backend.call("observe")
        self.assertFalse(observed["camera"]["connected"])
        self.assertIsNone(observed["camera"]["model"])
        self.assertIsNone(observed["camera"]["measured_frame_rate_hz"])

    def test_project_switch_and_config_refused_while_active(self):
        self.connect()
        plan = self.plan()
        self.backend.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"], "timeout_s": 1})
        with self.assertRaisesRegex(RuntimeError, "changing project"):
            self.backend.create_project(Path(self.tmp.name) / "other.admetp")
        with self.assertRaisesRegex(RuntimeError, "Finish or abort"):
            self.backend.call("apply_corrections")
        self.backend.call("control_protocol", {"action": "abort", "timeout_s": 1})

    def test_emergency_does_not_wait_for_command_lock(self):
        import threading

        self.connect()
        done = threading.Event()
        with self.backend.lock:
            worker = threading.Thread(target=lambda: (self.backend.emergency_stop(), done.set()))
            worker.start()
            self.assertTrue(done.wait(2))
        worker.join()
        self.assertEqual({c["mode"] for c in self.backend.call("observe")["channels"]}, {"off"})

    def test_pressure_plan_runs_and_finishes_zeroed(self):
        self.connect()
        document = definition()
        document["steps"][0].update(sensor_setpoints={}, pressure_setpoints={"0": 2000})
        plan = self.plan(document)
        self.backend.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"], "timeout_s": 1})
        self.backend.call("control_protocol", {"action": "confirm", "timeout_s": 1})
        self.wait_completed(plan["plan_id"])
        observed = self.backend.call("observe")
        self.assertFalse(observed["recording"]["active"])
        self.assertTrue(all(c["requested_pressure_mbar"] in (None, 0) for c in observed["channels"]))

    def test_emergency_stops_a_protocol_and_leaves_nothing_set(self):
        self.connect()
        plan = self.plan(definition(10))
        self.backend.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"], "timeout_s": 1})
        self.backend.call("control_protocol", {"action": "confirm", "timeout_s": 0.1})
        stopped = self.backend.emergency_stop()
        self.assertTrue(stopped["channels_zeroed"])
        observed = self.backend.call("observe")
        self.assertEqual({c["mode"] for c in observed["channels"]}, {"off"})
        self.assertFalse(observed["recording"]["active"])

    def test_shutdown_reports_cleanup_errors(self):
        with patch.object(self.backend.service, "run") as run:
            run.return_value = type("Result", (), {"metadata": {"cleanup_errors": ["disconnect failed"]}})()
            with self.assertRaisesRegex(RuntimeError, "disconnect failed"):
                self.backend.shutdown()

    def test_duplicate_json_keys_refused(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            loads('{"name":"one","name":"two"}')

    def test_camera_actions_run_on_the_engine_with_mocked_camera(self):
        from admet.engines.acquisition.camera import Camera
        from tests.test_camera_backend import FakePylon

        self.backend.simulated = False
        self.backend.engine.camera = Camera(FakePylon)
        self.backend.run(RunJob("refresh", "acquisition", "list_cameras"))
        self.backend.run(RunJob("connect", "acquisition", "connect_camera", {"camera_index": 0}))
        self.assertTrue(self.backend.engine.camera.connected)
        result = self.backend.run(RunJob(
            "settings", "acquisition", "set_camera_settings", {"camera_width": 512},
        ))
        self.assertIsNotNone(result.metadata)
        self.backend.run(RunJob("disconnect", "acquisition", "disconnect_camera"))
        self.assertFalse(self.backend.engine.camera.connected)

    def test_pause_skip_and_abort_zero_all_channels(self):
        self.connect()
        for action in ("pause", "skip", "abort"):
            with self.subTest(action=action):
                plan = self.plan(definition(10))
                self.backend.call("control_protocol", {
                    "action": "execute", "plan_id": plan["plan_id"], "timeout_s": 1,
                })
                self.backend.call("control_protocol", {"action": "confirm", "timeout_s": 0.1})
                self.backend.call("control_protocol", {"action": action, "timeout_s": 0.1})
                observation = self.backend.call("observe")
                for channel in observation["channels"]:
                    self.assertIn(channel["requested_flow_ul_min"], (0, None))
                    self.assertIn(channel["requested_pressure_mbar"], (0, None))
                if action == "pause":
                    self.backend.call("control_protocol", {"action": "abort", "timeout_s": 0.5})
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    state = self.backend.call("planned_protocols", {"plan_id": plan["plan_id"]})["plans"][0]["state"]
                    if state != "executing":
                        break
                    time.sleep(0.01)
                self.assertNotEqual(state, "executing")

    def test_manual_user_channel_controls_work_idle_and_during_protocol(self):
        self.connect()
        self.backend.call("set_channel_flow", {"channel_index": 0, "channel_flow_ul_min": 5})
        self.assertEqual(self.backend.engine.channel_manager.channels[0].active_setpoint, 5)
        document = {"name": "one_channel", "steps": [{
            "sensor_setpoints": {"1": 15}, "trigger_type": "time",
            "trigger_params": {"duration_s": 1}, "on_complete": "zero",
        }]}
        plan = self.plan(document)
        self.backend.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"], "timeout_s": 0.1})
        self.assertEqual(self.backend.engine.channel_manager.channels[1].owner, "pipeline")
        self.backend.call("set_channel_pressure", {"channel_index": 0, "channel_pressure_mbar": 17})
        self.assertEqual(self.backend.engine.channel_manager.channels[0].pressure_setpoint, 17)
        self.backend.run(RunJob("stop", "acquisition", "stop_channel", {"channel_index": 0}))
        stopped = self.backend.engine.channel_manager.channels[0]
        self.assertEqual((stopped.mode, stopped.pressure_setpoint), ("pressure", 0.0))   # stopped by pressure
        for action, payload in (
            ("set_channel_flow", {"channel_flow_ul_min": 4}),
            ("set_channel_pressure", {"channel_pressure_mbar": 12}), ("stop_channel", {}),
        ):
            with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, "controlled by the protocol"):
                self.backend.run(RunJob("manual", "acquisition", action, {"channel_index": 1, **payload}))
        self.assertEqual(self.backend.engine.channel_manager.channels[1].active_setpoint, 15)
        self.wait_completed(plan["plan_id"])
        self.backend.call("set_channel_flow", {"channel_index": 1, "channel_flow_ul_min": 7})
        self.assertEqual(self.backend.engine.channel_manager.channels[1].active_setpoint, 7)
        self.backend.emergency_stop()
        self.assertTrue(all(c.mode == "off" and c.active_setpoint == 0
                            for c in self.backend.engine.channel_manager.channels))

    def test_manual_controls_reject_invalid_targets_and_work_after_emergency_stop(self):
        with self.assertRaisesRegex(RuntimeError, "not connected"):
            self.backend.call("set_channel_flow", {"channel_index": 0, "channel_flow_ul_min": 5})
        self.backend.call("connect_fluidics")
        with self.assertRaisesRegex(RuntimeError, "corrections"):
            self.backend.call("set_channel_flow", {"channel_index": 0, "channel_flow_ul_min": 5})
        self.backend.call("apply_corrections")
        with patch.object(self.backend.engine.sdk, "set_sensor_regulation", side_effect=AssertionError("invalid write")), \
                patch.object(self.backend.engine.sdk, "set_pressure", side_effect=AssertionError("invalid write")):
            for index, target in ((-1, 1), (True, 1), (100, 1), (0, -1), (0, float("nan")), (0, 1e9)):
                with self.subTest(index=index, target=target), self.assertRaises(ValueError):
                    self.backend.call("set_channel_flow", {"channel_index": index, "channel_flow_ul_min": target})
            self.backend.engine.hardware.state.sensor_channels[0].smax = None
            with self.assertRaisesRegex(ValueError, "range unavailable"):
                self.backend.call("set_channel_flow", {"channel_index": 0, "channel_flow_ul_min": 5})
        self.backend.emergency_stop()
        # Nothing is latched: the operator can drive the rig again straight away.
        self.backend.call("set_channel_pressure", {"channel_index": 0, "channel_pressure_mbar": 10})
        self.backend.run(RunJob("stop", "acquisition", "stop_channel", {"channel_index": 0}))

    def test_desktop_imports_no_transport_or_terminal(self):
        result = subprocess.run([
            sys.executable, "-c",
            "import sys; from admet.ui.backend import DesktopBackend; "
            "b = DesktopBackend(simulated=True); "
            "assert not any(k.startswith('admet.mcp') for k in sys.modules); "
            "assert 'admet.core.control' not in sys.modules",
        ], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
