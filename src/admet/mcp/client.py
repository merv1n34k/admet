"""Attach to an existing owner using its MCP socket; never start hardware."""

import hashlib
import json
from pathlib import Path
import socket

from admet.core.runtime import read_state


def software_digest():
    package = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        digest.update(str(path.relative_to(package)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class OwnerClient:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.socket = None
        self.reader = None
        self.sequence = 0

    def connect(self):
        state = read_state(self.directory) or {}
        header = state.get("runtime") or {}
        if header.get("state") != "running":
            raise RuntimeError("No running owner; ask the agent to start ADMET")
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(5)
        try:
            self.socket.connect(str(self.directory / "control.sock"))
            self.reader = self.socket.makefile("r", encoding="utf-8")
            self._send({
                "method": "admet/attach", "mode": header.get("mode"),
                "project": str(Path(header["project"]).resolve()) if header.get("project") else None,
                "software_digest": software_digest(),
            })
            response = self._receive()
            if not response.get("ok"):
                raise RuntimeError(response.get("error") or "attachment refused")
            self.request("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "admet-control", "version": "0.1.0"},
            })
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            self.close()
            raise
        return self

    def _send(self, payload):
        self.socket.sendall(json.dumps(payload, allow_nan=False).encode() + b"\n")

    def _receive(self):
        line = self.reader.readline()
        if not line:
            raise ConnectionError("owner closed the connection")
        return json.loads(line)

    def request(self, method, params):
        self.sequence += 1
        self._send({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})
        while True:
            response = self._receive()
            if response.get("id") != self.sequence:
                continue
            if "error" in response:
                raise RuntimeError(response["error"]["message"])
            return response["result"]

    def call(self, name, arguments=None):
        if self.socket is None:
            self.connect()
        arguments = arguments or {}
        self.socket.settimeout(max(5, float(arguments.get("timeout_s", 0)) + 5))
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(item["text"] for item in result.get("content", [])
                         if item.get("type") == "text")
        if result.get("isError"):
            raise RuntimeError(text)
        return json.loads(text)

    def close(self):
        connection, self.socket = self.socket, None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        if self.reader is not None:
            self.reader.close()
            self.reader = None
