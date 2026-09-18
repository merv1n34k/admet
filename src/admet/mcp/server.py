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
from admet.mcp.tools import tools_for

PROTOCOL_VERSION = "2024-11-05"
SERVER = {"name": "admet", "version": "0.1.0"}

# Engines offered by default. Analysis engines are heavy to import, so they are
# loaded only when asked for.
DEFAULT_ENGINES = ("acquisition",)


# Sessions are core's, not an engine's, so they are tools in their own right: a
# client opens a project first, then everything it does lands inside it.
PROJECT_TOOLS = (
    {
        "name": "project_open",
        "description": "Open an existing project. Actions that write files need one open.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path to a .admetp directory"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "project_create",
        "description": "Create a project and make it the one runs write into.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Where to create it, ending in .admetp"},
                "project_id": {"type": "string", "description": "Name for it; defaults to the directory name"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "project_status",
        "description": "Which project is open, and what it holds.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "project_list",
        "description": "Projects found under a directory.",
        "inputSchema": {
            "type": "object",
            "properties": {"root": {"type": "string", "description": "Directory to look in"}},
            "required": ["root"],
            "additionalProperties": False,
        },
    },
)

_PROJECT_CALLS = {
    "project_open": lambda admet, args: _opened(admet, admet.open_project(args["path"])),
    "project_create": lambda admet, args: _opened(
        admet, admet.create_project(args["path"], args.get("project_id", ""))
    ),
    "project_status": lambda admet, _args: admet.describe_project(),
    "project_list": lambda admet, args: {"projects": admet.discover_projects(args["root"])},
}


def _opened(admet: Admet, store: Any) -> dict[str, Any]:
    del store
    return admet.describe_project()


class SimulationRefused(Exception):
    """An action would have reached the instrument while in simulated mode."""


class AdmetServer:
    """Engine actions as MCP tools.

    In simulated mode the server connects simulated and refuses to be talked out
    of it. A caller asking for real hardware in a simulated session is told no
    rather than quietly given it.
    """

    def __init__(
        self,
        *,
        engines: tuple[str, ...] = DEFAULT_ENGINES,
        simulated: bool = False,
        project: str | None = None,
    ):
        self.simulated = simulated
        self._engine_ids = engines
        self.admet = Admet(project=project)

    # -- engines ------------------------------------------------------------
    def tools(self) -> list[dict[str, Any]]:
        tools = list(PROJECT_TOOLS)
        for engine_id in self._engine_ids:
            try:
                tools.extend(tools_for(self.admet.engine(engine_id)))
            except Exception as exc:  # an engine whose stack is not installed
                _warn(f"engine {engine_id} unavailable: {exc}")
        return tools

    # -- calling ------------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments = dict(arguments or {})
        if name in _PROJECT_CALLS:
            return _PROJECT_CALLS[name](self.admet, arguments)

        engine_id, _, action = name.partition("_")
        if engine_id not in self._engine_ids or not action:
            raise LookupError(f"no tool named {name!r}")

        settings = self._apply_simulation(action, arguments)
        result = self.admet.run(engine_id, action, settings, job_id=f"mcp_{action}")
        return {
            "status": result.status,
            "warnings": list(result.warnings),
            "metadata": result.metadata,
        }

    def _apply_simulation(self, action: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Keep a simulated session simulated.

        What makes a session safe is the connection, not the individual action:
        once the fluidics are connected simulated, everything downstream acts on
        the simulation. So the rule is about connecting, not about refusing the
        actions that follow -- running a protocol against a simulated rig is the
        point of the mode, not something to block.
        """
        if not self.simulated:
            return settings
        if settings.get("simulated") is False:
            raise SimulationRefused(
                f"{action} was asked for real hardware, but this server is simulated"
            )
        if "simulated" in self._params_of(action):
            settings["simulated"] = True
        elif action == "connect_camera" and not os.environ.get("PYLON_CAMEMU"):
            # The camera has no simulated flag; its emulation is switched on by
            # the Pylon environment before the process starts.
            raise SimulationRefused(
                "connect_camera would open a real camera. Set PYLON_CAMEMU=2 "
                "before starting the server to use emulated cameras instead"
            )
        return settings

    def _params_of(self, action: str) -> tuple[str, ...]:
        for engine_id in self._engine_ids:
            for spec in self.admet.engine(engine_id).actions:
                if spec.id == action:
                    return spec.params
        return ()


# -- JSON-RPC ---------------------------------------------------------------


def serve(
    *,
    simulated: bool = False,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Read requests until the stream closes. Returns a process exit code."""
    server = AdmetServer(simulated=simulated)
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
