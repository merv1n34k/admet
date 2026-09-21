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
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from admet.core.runtime import read_events, read_state
from admet.core.watch import render
from admet.mcp.server import serve


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

        # 4. The validation, which returns while it runs.
        started = self.call(
            "validate_oil_capacity",
            configuration="bypass_chip",
            flow_targets_ul_min=[5.0, 10.0, 20.0],
            settle_tolerance_ul_min=50.0,
            settle_window_s=1.0,
            settle_timeout_s=3.0,
            sample_window_s=1.0,
            oil_pressure_trip_mbar=1900.0,
            tick_s=0.1,
        )
        self.assertTrue(started["validation_id"])
        self.assertTrue(started["fluidics_csv"])

        # 5. Nothing flows until the operator confirms the channel mapping.
        self._wait_for(
            lambda: self.call("observe")["protocol"]["confirmation_message"] != "",
            "the mapping question",
        )
        asked = self.call("observe")
        self.assertIn("physically the oil line", asked["protocol"]["confirmation_message"])
        self.assertEqual(asked["channels"][0]["mode"], "off")
        self.call("confirm_protocol")

        # 6. Polled through the same surface while it runs, without taking
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

        # 7. It completes, the rig is back at zero, and the artifacts are real.
        finished = self.call("observe")
        self.assertEqual(finished["validation"]["classification"], "pass")
        self.assertFalse(finished["protocol"]["state"] == "running")
        self.assertEqual({c["mode"] for c in finished["channels"]}, {"off"})

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

        # 8. The manifest lists what was produced, and nothing that was not.
        manifest = json.loads((self.project / "manifest.json").read_text())
        roles = {entry["role"] for entry in manifest["files"]}
        self.assertIn("control_fluidics_csv", roles)
        self.assertNotIn("control_video", roles)
        self.assertEqual(list(self.project.rglob("*.avi")), [])

        # 9. Closing stdin leaves every channel at zero and the runtime stopped.
        self._stdin_w.close()
        self._server.join(timeout=15)
        stopped = read_state(self.runtime)
        self.assertEqual(stopped["runtime"]["state"], "stopped")
        self.assertEqual(
            {c["mode"] for c in stopped["observation"]["channels"]}, {"off"}
        )


if __name__ == "__main__":
    unittest.main()
