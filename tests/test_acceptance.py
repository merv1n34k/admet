"""The whole loop, driven the way a client drives it.

Everything else tests a piece. This drives the arrangement the milestone is
for: one process owning the instrument over MCP, a runtime directory a human
watches from another terminal, a validation run commanded and polled through
the same surface, and a shutdown that leaves the rig at zero.

Durations are short so it runs with the unit tests. The shape is the one from
the acceptance run, not a smaller thing that resembles it.
"""

import io
import json
import os
import signal
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from admet.core.runtime import read_events, read_state
from admet.core.watch import render
from admet.mcp.server import serve


def _terminate_owner(runtime, timeout_s=10.0):
    state = read_state(runtime)
    if not state or state["runtime"]["state"] == "stopped":
        return
    os.kill(int(state["runtime"]["pid"]), signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        current = read_state(runtime)
        if current and current["runtime"]["state"] == "stopped":
            return
        time.sleep(0.05)
    raise AssertionError("persistent owner did not stop after SIGTERM")


class AcceptanceRun(unittest.TestCase):
    def setUp(self):
        self.runtime = Path(tempfile.mkdtemp())
        self.project = Path(tempfile.mkdtemp()) / "oil.admetp"
        read_fd, write_fd = os.pipe()
        self._stdin = os.fdopen(read_fd, "r")
        self._stdin_w = os.fdopen(write_fd, "w")
        self._out = io.StringIO()
        self._replies: dict[int, dict] = {}
        self._id = 0
        self._stderr = unittest.mock.patch("sys.stderr", io.StringIO())
        self._stderr.start()
        self._server = threading.Thread(target=self._serve, daemon=True)
        self._server.start()
        self._wait_for(lambda: read_state(self.runtime) is not None, "the server to publish")

    def _serve(self):
        serve(
            simulated=True,
            project=None,
            runtime=str(self.runtime),
            stdin=self._stdin,
            stdout=self._out,
        )

    def tearDown(self):
        if not self._stdin_w.closed:
            self._stdin_w.close()
        self._server.join(timeout=15)
        _terminate_owner(self.runtime)
        self._stdin.close()
        self._stderr.stop()

    # -- talking to it the way a client does --------------------------------
    def call(self, name, **arguments):
        self._id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        self._stdin_w.write(json.dumps(request) + "\n")
        self._stdin_w.flush()
        self._wait_for(lambda: self._reply(self._id) is not None, f"a reply to {name}")
        reply = self._reply(self._id)
        content = reply["result"]["content"][0]["text"]
        if reply["result"].get("isError"):
            raise AssertionError(f"{name} was refused: {content}")
        return json.loads(content)

    def _call_error(self, name, **arguments):
        self._id += 1
        request = {
            "jsonrpc": "2.0", "id": self._id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        self._stdin_w.write(json.dumps(request) + "\n")
        self._stdin_w.flush()
        self._wait_for(lambda: self._reply(self._id) is not None, f"a refusal from {name}")
        reply = self._reply(self._id)["result"]
        self.assertTrue(reply["isError"])
        return reply["content"][0]["text"]

    def _reply(self, request_id):
        for line in self._out.getvalue().splitlines():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                return message
        return None

    def _wait_for(self, until, what, timeout_s=30.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if until():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    # -- the run ------------------------------------------------------------
    def test_the_whole_validation_loop(self):
        # 1. A project to write into, and the instrument, over MCP.
        opened = self.call("create_project", path=str(self.project))
        self.assertTrue(opened["open"])
        self.assertTrue(self.call("connect_fluidics")["connected"])
        self.call("apply_corrections", oil_l_scale=1.0)

        # 2. observe answers with three channels and fresh measurements.
        self._wait_for(
            lambda: self.call("observe")["channels"][0]["flow_ul_min"] is not None,
            "the first measurements",
        )
        observed = self.call("observe")
        self.assertEqual(len(observed["channels"]), 3)
        self.assertEqual(observed["channels"][0]["label"], "Oil L")
        self.assertTrue(observed["guards"]["corrections"]["met"])
        self.assertEqual(observed["runtime"]["mode"], "simulated")

        # 3. The human's monitor sees the same process, from the files alone.
        #    The runtime is published a few times a second, so it catches up
        #    rather than being current the instant a call returns.
        self._wait_for(
            lambda: (read_state(self.runtime)["observation"]["channels"] or []) != [],
            "the runtime to catch up with the connection",
        )
        frame = render(read_state(self.runtime), read_events(self.runtime))
        self.assertIn("SIMULATED", frame)
        self.assertIn("Oil L", frame)
        self.assertIn("read-only", frame)

        # 4. Planning builds and publishes the complete protocol without starting it.
        plan = self.call(
            "plan_protocol",
            operation_id="validate_oil_capacity",
            settings={
                "configuration": "bypass_chip",
                "flow_targets_ul_min": [5.0, 10.0, 20.0],
                "settle_tolerance_ul_min": 50.0,
                "settle_window_s": 1.0,
                "settle_timeout_s": 3.0,
                "sample_window_s": 1.0,
                "oil_pressure_trip_mbar": 1900.0,
                "tick_s": 0.1,
            },
        )
        before = self.call("observe")
        self.assertEqual(before["protocol"]["state"], "idle")
        self.assertFalse(before["recording"]["active"])
        self.assertEqual({channel["mode"] for channel in before["channels"]}, {"off"})
        self.assertEqual(before["planned_protocols"][0]["digest"], plan["digest"])
        listed = self.call("planned_protocols", plan_id=plan["plan_id"])["plans"][0]
        self.assertEqual(listed["steps"], plan["steps"])
        self._wait_for(
            lambda: plan["plan_id"] in render(read_state(self.runtime), read_events(self.runtime)),
            "the plan to appear in watch",
        )
        runtime_events = read_events(self.runtime, limit=0)
        self.assertTrue(any(event["type"] == "plans" for event in runtime_events))
        self.assertEqual([event["seq"] for event in runtime_events], sorted(
            event["seq"] for event in runtime_events
        ))

        # 5. Execution accepts only the immutable plan id and returns while it runs.
        started = self.call("execute_protocol_plan", plan_id=plan["plan_id"])
        self.assertTrue(started["validation_id"])
        self.assertTrue(started["fluidics_csv"])

        # 6. Nothing flows until the operator confirms the channel mapping.
        self._wait_for(
            lambda: self.call("observe")["protocol"]["confirmation_message"] != "",
            "the mapping question",
        )
        asked = self.call("observe")
        self.assertIn("physically the oil line", asked["protocol"]["confirmation_message"])
        self.assertEqual(asked["channels"][0]["mode"], "off")
        self.call("confirm_protocol")

        # 7. Polled through the same surface while it runs, without taking
        #    anything from anyone.
        seen_targets, sequence = set(), 0
        while self.call("observe")["validation"]["active"]:
            live = self.call("observe")
            if live["validation"]["current_target_ul_min"]:
                seen_targets.add(live["validation"]["current_target_ul_min"])
            events = self.call("protocol_events", after_sequence=sequence, limit=50)
            sequence = events["latest_sequence"]
            time.sleep(0.2)
        self.assertTrue(seen_targets, "the run never reported a target")

        # 8. It completes, the rig is back at zero, and the artifacts are real.
        finished = self.call("observe")
        self.assertEqual(finished["validation"]["classification"], "pass")
        self.assertFalse(finished["protocol"]["state"] == "running")
        self.assertEqual({c["mode"] for c in finished["channels"]}, {"off"})
        completed_plan = self.call("planned_protocols", plan_id=plan["plan_id"])["plans"][0]
        self.assertEqual(completed_plan["state"], "completed")
        refused = self._call_error("execute_protocol_plan", plan_id=plan["plan_id"])
        self.assertIn("completed", refused)

        summary_path = Path(finished["validation"]["artifacts"]["summary"])
        csv_path = Path(finished["validation"]["artifacts"]["fluidics_csv"])
        self.assertTrue(summary_path.is_file())
        self.assertTrue(csv_path.is_file())

        summary = json.loads(summary_path.read_text())
        self.assertTrue(summary["mapping_confirmed"])
        self.assertEqual(
            [target["requested_ul_min"] for target in summary["targets"]], [5.0, 10.0, 20.0]
        )
        for target in summary["targets"]:
            self.assertGreater(target["samples"], 0)
            self.assertIsNotNone(target["flow_ul_min"]["mean"])

        # 9. Cancelled and stale plans are refused.
        cancelled = self.call("plan_protocol", operation_id="run_steps", settings={
            "steps": [{"name": "never runs", "trigger_type": "time",
                       "trigger_params": {"duration_s": 0.1}}],
        })
        self.call("cancel_protocol_plan", plan_id=cancelled["plan_id"])
        self.assertIn("cancelled", self._call_error(
            "execute_protocol_plan", plan_id=cancelled["plan_id"]
        ))

        # 10. The manifest lists what was produced, and nothing that was not.
        manifest = json.loads((self.project / "manifest.json").read_text())
        roles = {entry["role"] for entry in manifest["files"]}
        self.assertIn("control_fluidics_csv", roles)
        self.assertNotIn("control_video", roles)
        self.assertEqual(list(self.project.rglob("*.avi")), [])

        # 11. Closing one chat detaches it without ending the durable owner.
        owner_pid = self.call("observe")["runtime"]["pid"]
        self._stdin_w.close()
        self._server.join(timeout=15)
        detached = read_state(self.runtime)
        self.assertEqual(detached["runtime"]["state"], "running")
        self.assertEqual(detached["runtime"]["pid"], owner_pid)
        self.assertEqual(
            {c["mode"] for c in detached["observation"]["channels"]}, {"off"}
        )

        # 12. Manual SIGTERM safely stops the owner and publishes the final state.
        _terminate_owner(self.runtime)
        self.assertEqual(read_state(self.runtime)["runtime"]["state"], "stopped")


if __name__ == "__main__":
    unittest.main()


class DocumentedCommandTests(unittest.TestCase):
    """The commands the README prints, run as real processes.

    In-process tests share an interpreter with the code under test, so an
    import that only happens at module level, or an argument the parser never
    really accepts, can pass there and fail the first time somebody types it.
    """

    def _admet(self, *arguments, stdin="", timeout=60):
        import subprocess
        import sys

        return subprocess.run(
            [sys.executable, "-m", "admet.app", *arguments],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(Path(__file__).resolve().parent.parent),
        )

    def test_serve_starts_and_creates_its_project_as_documented(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            project = Path(tmp) / "oil.admetp"

            done = self._admet(
                "serve", "--simulated",
                "--runtime", str(runtime),
                "--project", str(project),
                "--create-project",
                stdin="",  # EOF straight away: start, then shut down cleanly
            )

            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue((project / "manifest.json").is_file())
            self.assertEqual(read_state(runtime)["runtime"]["state"], "running")
            _terminate_owner(runtime)

    def test_the_same_flags_work_before_the_subcommand(self):
        # The acceptance run in the specification writes them this way.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, project = Path(tmp) / "runtime", Path(tmp) / "oil.admetp"

            done = self._admet(
                "--project", str(project), "--create-project",
                "--runtime", str(runtime),
                "serve", "--simulated",
                stdin="",
            )

            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue((project / "manifest.json").is_file())
            _terminate_owner(runtime)

    def test_serving_without_saying_which_hardware_fails(self):
        done = self._admet("serve")

        self.assertNotEqual(done.returncode, 0)
        self.assertIn("--simulated", done.stderr)

    def test_a_mode_mismatch_on_an_existing_runtime_is_refused(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            first = subprocess.Popen(
                [sys.executable, "-m", "admet.app", "serve", "--simulated",
                 "--runtime", str(runtime)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=str(Path(__file__).resolve().parent.parent),
            )
            try:
                deadline = time.monotonic() + 30
                while read_state(runtime) is None and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertIsNotNone(read_state(runtime), "the first server never published")

                second = self._admet("serve", "--live", "--runtime", str(runtime))

                self.assertNotEqual(second.returncode, 0)
                self.assertIn("already owns a simulated session", second.stderr)
            finally:
                first.stdin.close()
                first.wait(timeout=30)
                first.stdout.close()
                first.stderr.close()
                _terminate_owner(runtime)

    def test_watch_renders_a_real_server_and_stays_telemetry_only(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            server = subprocess.Popen(
                [sys.executable, "-m", "admet.app", "serve", "--simulated",
                 "--runtime", str(runtime)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=str(Path(__file__).resolve().parent.parent),
            )
            try:
                deadline = time.monotonic() + 30
                while read_state(runtime) is None and time.monotonic() < deadline:
                    time.sleep(0.1)

                watched = self._admet("watch", "--runtime", str(runtime), "--once")

                self.assertEqual(watched.returncode, 0, watched.stderr)
                self.assertIn("SIMULATED", watched.stdout)
                self.assertIn("read-only", watched.stdout)
            finally:
                server.stdin.close()
                server.wait(timeout=30)
                server.stdout.close()
                server.stderr.close()
                _terminate_owner(runtime)

    def test_watching_loads_neither_the_service_nor_an_engine(self):
        # Checked in a real process, because the eager import that made this
        # false lived in app.py rather than in the monitor itself.
        import subprocess
        import sys

        probe = (
            "import sys; sys.argv = ['admet', 'watch', '--runtime', '/nonexistent', '--once'];"
            "from admet.app import main; main();"
            "print('SERVICE', 'admet.core.service' in sys.modules);"
            "print('ENGINES', any(m.startswith('admet.engines') for m in sys.modules))"
        )
        done = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60,
            cwd=str(Path(__file__).resolve().parent.parent),
        )

        self.assertIn("SERVICE False", done.stdout)
        self.assertIn("ENGINES False", done.stdout)
