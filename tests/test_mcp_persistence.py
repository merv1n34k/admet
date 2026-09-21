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
        self.stop_reasons = []
        self.shutdown_calls = 0

    def emergency_stop(self, reason):
        self.stop_reasons.append(reason)
        return {"errors": []}

    def engine_action(self, engine_id, action, settings):
        self.shutdown_calls += 1
        self.shutdown_action = (engine_id, action, settings)
        return type("Result", (), {"metadata": {"cleanup_errors": []}})()


class _Server:
    def __init__(self):
        self.admet = _Admet()
        self.calls = 0

    def call(self, name, arguments):
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
        client.sendall(json.dumps(request).encode() + b"\n")
        return json.loads(client.makefile("r").readline())


@unittest.skipUnless(hasattr(signal, "SIGUSR1"), "POSIX signals required")
class PersistentOwnerTests(unittest.TestCase):
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

        worker = threading.Thread(target=client_work)
        worker.start()
        with (
            patch.object(mcp_server, "AdmetServer", return_value=server),
            patch.object(mcp_server, "_claim_runtime", return_value=(owner, publisher)),
        ):
            result = mcp_server._serve_owner(
                simulated=True, project=None, create_project=False, runtime=str(runtime)
            )
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


if __name__ == "__main__":
    unittest.main()
