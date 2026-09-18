"""An MCP server over stdio, so a program can drive the instrument.

The protocol is JSON-RPC 2.0, one message per line on stdin and stdout. It is
implemented here rather than pulled in as a dependency because it is a hundred
lines of dispatch, and this codebase is meant to be readable end to end by
someone who has a week, not a month.

Every tool is generated from an engine's own action declarations, so the tool
list cannot drift from what the engine actually does.

Nothing is printed to stdout except protocol messages. Anything else -- a log
line, a stray print -- corrupts the stream, so diagnostics go to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, TextIO

from admet.core.service import Admet
from admet.mcp.tools import DESCRIBE_TOOL, operation_tools

PROTOCOL_VERSION = "2024-11-05"
SERVER = {"name": "admet", "version": "0.1.0"}


class SimulationRefused(Exception):
    """An action would have reached the instrument while in simulated mode."""


class AdmetServer:
    """Engine actions as MCP tools.

    In simulated mode the server connects simulated and refuses to be talked out
    of it. A caller asking for real hardware in a simulated session is told no
    rather than quietly given it.
    """

    def __init__(self, *, simulated: bool = False, project: str | None = None):
        self.simulated = simulated
        self.admet = Admet(project=project)

    def tools(self) -> list[dict[str, Any]]:
        """Guarded operations, and describe. Nothing below them.

        An engine action has no guards, which is what it is for; over a wire to
        a model that is the wrong default. The Python binding still reaches them
        -- Admet.engine_action -- as the expert escape hatch.
        """
        return [DESCRIBE_TOOL, *operation_tools()]

    # -- calling ------------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments = dict(arguments or {})
        if name == "describe":
            return self.admet.describe(str(arguments.get("target") or ""))
        return self.admet.do(name, self._simulated(name, arguments))

    def _simulated(self, name: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Keep a simulated session simulated.

        What makes a session safe is the connection, not each operation: once the
        fluidics are connected simulated, everything after acts on the simulation.
        So the rule is about connecting, and running a protocol against a
        simulated rig is the point of the mode rather than something to refuse.
        """
        if not self.simulated:
            return settings
        if settings.get("simulated") is False:
            raise SimulationRefused(
                f"{name} was asked for real hardware, but this server is simulated"
            )
        if _takes_simulated(name):
            return {**settings, "simulated": True}
        if name == "connect_camera" and not os.environ.get("PYLON_CAMEMU"):
            # The camera has no simulated flag: its emulation is switched on by
            # the Pylon environment before the process starts.
            raise SimulationRefused(
                "connect_camera would open a real camera. Set PYLON_CAMEMU=2 before "
                "starting the server to use emulated cameras instead"
            )
        return settings


def _takes_simulated(name: str) -> bool:
    """Whether this operation has a simulated switch of its own."""
    from admet.workflows.operations import BY_ID

    operation = BY_ID.get(name)
    return bool(operation and any(param.name == "simulated" for param in operation.params))


# -- JSON-RPC ---------------------------------------------------------------


def serve(
    *,
    simulated: bool,
    project: str | None = None,
    runtime: str | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Read requests until the stream closes. Returns a process exit code."""
    server = AdmetServer(simulated=simulated, project=project)
    _ = runtime  # published from here once there is a publisher to do it
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    _warn(f"admet mcp ready ({'simulated' if simulated else 'live hardware'})")

    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            _respond(sink, _error(None, -32700, f"invalid JSON: {exc}"))
            continue
        response = handle(server, request)
        if response is not None:
            _respond(sink, response)
    return 0


def handle(server: AdmetServer, request: dict[str, Any]) -> dict[str, Any] | None:
    """One request in, one response out -- or None for a notification."""
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        return _ok(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER,
            },
        )
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "ping":
        return _ok(request_id, {})
    if method == "tools/list":
        return _ok(request_id, {"tools": server.tools()})
    if method == "tools/call":
        return _call(server, request_id, params)
    return _error(request_id, -32601, f"unknown method {method!r}")


def _call(server: AdmetServer, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name", "")
    try:
        payload = server.call(name, params.get("arguments") or {})
    except SimulationRefused as exc:
        # A refusal is an answer, not a transport failure: the caller is told why.
        return _ok(request_id, _content(str(exc), is_error=True))
    except Exception as exc:
        _warn(traceback.format_exc())
        return _ok(request_id, _content(f"{type(exc).__name__}: {exc}", is_error=True))
    return _ok(request_id, _content(json.dumps(payload, default=str, indent=2)))


def _content(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _ok(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _respond(sink: TextIO, message: dict[str, Any]) -> None:
    sink.write(json.dumps(message) + "\n")
    sink.flush()


def _warn(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
