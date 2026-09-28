import json
import os
import signal
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from admet.mcp import server as mcp_server


class _Admet:
    def __init__(self):
        self.project = None
        self.stop_reasons = []
        self.shutdown_calls = 0
        self.stopped_channels = []

    def emergency_stop(self, reason):
        self.stop_reasons.append(reason)
        return {"errors": []}

    def engine_action(self, engine_id, action, settings):
        self.shutdown_calls += 1
        self.shutdown_action = (engine_id, action, settings)
        return type("Result", (), {"metadata": {"cleanup_errors": []}})()

    def do(self, operation, settings):
        if operation == "stop_channel":
            self.stopped_channels.append(settings["channel_index"])
        return {}


class _Server:
    def __init__(self):
        self.admet = _Admet()
        self.calls = 0
        self.block_entered = threading.Event()
        self.block_release = threading.Event()

    def call(self, name, arguments):
        if name == "block":
            self.block_entered.set()
            self.block_release.wait(5.0)
        self.calls += 1
        return {"name": name, "arguments": arguments, "calls": self.calls}

    def tools(self):
        return []


class _Owner:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class _Publisher:
    def __init__(self):
        self.stopped = []

    def stop(self, *, state, reason):
        self.stopped.append((state, reason))


def _round_trip(path, request):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + 5.0
    while True:
        try:
            client.connect(str(path))
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    with client:
        client.sendall(json.dumps({
            "method": mcp_server.ATTACH_METHOD,
            "mode": "simulated",
            "project": None,
            "software_digest": mcp_server._software_digest(),
        }).encode() + b"\n")
        reader = client.makefile("r")
        try:
            acknowledgement = json.loads(reader.readline())
            if not acknowledgement["ok"]:
                raise RuntimeError(acknowledgement["error"])
            client.sendall(json.dumps(request).encode() + b"\n")
            return json.loads(reader.readline())
        finally:
            reader.close()


@unittest.skipUnless(hasattr(signal, "SIGUSR1"), "POSIX signals required")
class PersistentOwnerTests(unittest.TestCase):
    def _run_owner(self, runtime, server, owner, publisher):
        with (
            patch.object(mcp_server, "AdmetServer", return_value=server),
            patch.object(mcp_server, "_claim_runtime", return_value=(owner, publisher)),
        ):
            return mcp_server._serve_owner(
                simulated=True, project=None, create_project=False, runtime=str(runtime)
            )

    def test_clients_reattach_and_signals_stop_activity_or_owner(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        runtime = Path(temporary.name)
        control = runtime / mcp_server.CONTROL_SOCKET
        server = _Server()
        owner = _Owner()
        publisher = _Publisher()
        errors = []

        def client_work():
            try:
                first = _round_trip(control, {
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "observe", "arguments": {}},
                })
                second = _round_trip(control, {
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "planned_protocols", "arguments": {}},
                })
                self.assertEqual(first["result"]["content"][0]["text"].count("calls"), 1)
                self.assertIn('"calls": 2', second["result"]["content"][0]["text"])
                os.kill(os.getpid(), signal.SIGUSR1)
                deadline = time.monotonic() + 2.0
                while not server.admet.stop_reasons and time.monotonic() < deadline:
                    time.sleep(0.01)
                os.kill(os.getpid(), signal.SIGTERM)
            except BaseException as exc:
                errors.append(exc)
                os.kill(os.getpid(), signal.SIGTERM)

        worker = threading.Thread(target=client_work)
        worker.start()
        result = self._run_owner(runtime, server, owner, publisher)
        worker.join(timeout=5.0)

        self.assertEqual(result, 0)
        self.assertEqual(errors, [])
        self.assertIn("manual SIGUSR1 emergency stop", server.admet.stop_reasons)
        self.assertTrue(any("manual SIGTERM" in reason for reason in server.admet.stop_reasons))
        self.assertEqual(server.admet.shutdown_calls, 1)
        self.assertEqual(
            server.admet.shutdown_action,
            ("acquisition", "shutdown_instrument", {}),
        )
        self.assertTrue(owner.released)
        self.assertEqual(publisher.stopped[-1][0], "stopped")
        self.assertFalse(control.exists())

    def test_manual_flow_lease_zeros_on_expiry_and_client_loss(self):
        server = _Server()
        leases = mcp_server._ManualLeaseManager(server)
        leases.start()
        try:
            leases.reserve("first", 0)
            leases.activate("first", 0, 0.05)
            deadline = time.monotonic() + 1.0
            while 0 not in server.admet.stopped_channels and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn(0, server.admet.stopped_channels)

            leases.reserve("second", 1)
            leases.activate("second", 1, 60.0)
            leases.release_client("second")
            self.assertIn(1, server.admet.stopped_channels)
        finally:
            leases.stop()

    def test_manual_flow_lease_is_exclusive_per_channel(self):
        server = _Server()
        leases = mcp_server._ManualLeaseManager(server)
        leases.reserve("first", 0)

        with self.assertRaisesRegex(RuntimeError, "another MCP client"):
            leases.reserve("second", 0)

    def test_owner_validation_rejects_missing_or_different_project(self):
        runtime = Path("/private/tmp/admet-validation-test")
        state = {"runtime": {"mode": "live", "project": None}}
        with patch("admet.core.runtime.read_state", return_value=state):
            with self.assertRaisesRegex(RuntimeError, "already serves project None"):
                mcp_server._validate_owner(runtime, simulated=False, project="/runs/oil.admetp")

    def test_blocked_client_does_not_block_other_clients_or_emergency_signal(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        runtime = Path(temporary.name)
        control = runtime / mcp_server.CONTROL_SOCKET
        server = _Server()
        owner = _Owner()
        publisher = _Publisher()
        errors = []

        def client_work():
            blocker = threading.Thread(
                target=lambda: _round_trip(control, {
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "block", "arguments": {}},
                }),
                daemon=True,
            )
            try:
                blocker.start()
                self.assertTrue(server.block_entered.wait(2.0))
                observed = _round_trip(control, {
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "observe", "arguments": {}},
                })
                self.assertEqual(observed["id"], 2)
                os.kill(os.getpid(), signal.SIGUSR1)
                deadline = time.monotonic() + 2.0
                while not server.admet.stop_reasons and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(server.admet.stop_reasons)
            except BaseException as exc:
                errors.append(exc)
            finally:
                server.block_release.set()
                blocker.join(timeout=2.0)
                os.kill(os.getpid(), signal.SIGTERM)

        worker = threading.Thread(target=client_work)
        worker.start()
        result = self._run_owner(runtime, server, owner, publisher)
        worker.join(timeout=5.0)

        self.assertEqual(result, 0)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
