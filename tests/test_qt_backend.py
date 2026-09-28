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
from admet.workflows.json_protocol import loads


def definition(duration=0.15):
    return {
        "name": "desktop_test",
        "pressure_limits_mbar": {"0": 500, "1": 400, "2": 400},
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
        path = Path(self.backend.workdir) / "records" / "protocols" / plan["plan_id"]
        summary = json.loads((path / "summary.json").read_text())
        self.assertEqual(summary["state"], "completed")
        self.assertTrue(Path(summary["artifacts"]["fluidics_csv"]).is_file())
        with self.assertRaisesRegex(RuntimeError, "completed"):
            self.backend.call("control_protocol", {"action": "execute", "plan_id": plan["plan_id"]})
        self.backend.shutdown()
        reopened = DesktopBackend(simulated=True)
        self.addCleanup(reopened.shutdown)
        reopened.open_project(self.backend.workdir)
        self.assertEqual(reopened.call("list_protocols")["protocols"][0]["name"], "desktop_test")
        self.assertEqual(reopened.call("planned_protocols")["plans"], [])
        self.assertTrue(reopened.service.project.session.files)

    def test_planning_never_invokes_engine_run(self):
        self.connect()
        with patch.object(self.backend.engine, "run", side_effect=AssertionError("hardware call")):
            plan = self.plan()
        self.assertEqual(plan["state"], "planned")

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
        for action in ("connect_camera", "list_cameras", "refresh_cameras"):
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
        self.assertTrue(self.backend.call("observe")["safety"]["tripped"])

    def test_shutdown_reports_cleanup_errors(self):
        with patch.object(self.backend.service, "run") as run:
            run.return_value = type("Result", (), {"metadata": {"cleanup_errors": ["disconnect failed"]}})()
            with self.assertRaisesRegex(RuntimeError, "disconnect failed"):
                self.backend.shutdown()

    def test_duplicate_json_keys_refused(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            loads('{"name":"one","name":"two"}')

    def test_legacy_camera_aliases_use_current_engine_with_mocked_camera(self):
        from admet.engines.acquisition.camera import Camera
        from tests.test_camera_backend import FakePylon

        self.backend.simulated = False
        self.backend.engine.camera = Camera(FakePylon)
        self.backend.run(RunJob("refresh", "acquisition", "refresh_cameras"))
        self.backend.run(RunJob("connect", "acquisition", "connect_camera", {"camera_index": 0}))
        self.assertTrue(self.backend.engine.camera.connected)
        result = self.backend.run(RunJob(
            "settings", "acquisition", "apply_camera_settings", {"camera_width": 512},
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

    def test_unbounded_manual_setters_are_not_desktop_controls(self):
        self.connect()
        with self.assertRaisesRegex(RuntimeError, "bounded JSON"):
            self.backend.call("set_channel_flow", {"channel_index": 0, "channel_flow_ul_min": 5})

    def test_desktop_imports_no_transport_or_terminal(self):
        result = subprocess.run([
            sys.executable, "-c",
            "import sys; from admet.ui.backend import DesktopBackend; "
            "b = DesktopBackend(simulated=True); "
            "assert not any(k.startswith('admet.mcp') for k in sys.modules); "
            "assert 'admet.core.control' not in sys.modules",
        ], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
