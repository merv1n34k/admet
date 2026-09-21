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
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
import traceback
from typing import Any, TextIO

from admet.core.service import Admet
from admet.mcp.tools import DESCRIBE_TOOL, PLAN_TOOLS, operation_tools
from admet.workflows.operations import operation as find_operation

PROTOCOL_VERSION = "2024-11-05"
SERVER = {"name": "admet", "version": "0.1.0"}
CONTROL_SOCKET = "control.sock"
OWNER_LOG = "owner.log"
OWNER_CHILD_ENV = "ADMET_OWNER_CHILD"


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
        simulated: bool = False,
        project: str | None = None,
        create: bool = False,
    ):
        self.simulated = simulated
        self.admet = Admet()
        if project:
            # Created here rather than by whoever started us, so that starting
            # a server on a new project is one command and not two.
            path = Path(project)
            if create and not (path / "manifest.json").is_file():
                self.admet.create_project(path)
            else:
                self.admet.open_project(path)

    def tools(self) -> list[dict[str, Any]]:
        """Guarded operations, and describe. Nothing below them.

        An engine action has no guards, which is what it is for; over a wire to
        a model that is the wrong default. The Python binding still reaches them
        -- Admet.engine_action -- as the expert escape hatch.
        """
        return [DESCRIBE_TOOL, *PLAN_TOOLS, *operation_tools()]

    # -- calling ------------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments = dict(arguments or {})
        if name == "describe":
            return self.admet.describe(str(arguments.get("target") or ""))
        if name == "plan_protocol":
            return self.admet.plan_protocol(
                str(arguments.get("operation_id") or ""), dict(arguments.get("settings") or {})
            )
        if name == "planned_protocols":
            return self.admet.planned_protocols(str(arguments.get("plan_id") or ""))
        if name == "execute_protocol_plan":
            return self.admet.execute_protocol_plan(str(arguments.get("plan_id") or ""))
        if name == "cancel_protocol_plan":
            return self.admet.cancel_protocol_plan(str(arguments.get("plan_id") or ""))
        operation = find_operation(name)
        if operation.starts_protocol:
            raise RuntimeError(
                f"{name} cannot start directly over MCP; use plan_protocol, review the plan, "
                "then execute_protocol_plan with its plan_id"
            )
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
    create_project: bool = False,
    runtime: str | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Read requests until the stream closes. Returns a process exit code."""
    if runtime and stdin is None and stdout is None:
        if os.environ.get(OWNER_CHILD_ENV) == "1":
            return _serve_owner(
                simulated=simulated,
                project=project,
                create_project=create_project,
                runtime=runtime,
            )
        return _serve_relay(
            simulated=simulated,
            project=project,
            create_project=create_project,
            runtime=runtime,
            source=sys.stdin,
            sink=sys.stdout,
        )
    if runtime and os.environ.get(OWNER_CHILD_ENV) != "1":
        return _serve_relay(
            simulated=simulated,
            project=project,
            create_project=create_project,
            runtime=runtime,
            source=stdin or sys.stdin,
            sink=stdout or sys.stdout,
        )
    return _serve_attached(
        simulated=simulated,
        project=project,
        create_project=create_project,
        runtime=runtime,
        stdin=stdin,
        stdout=stdout,
    )


def _serve_attached(
    *,
    simulated: bool,
    project: str | None,
    create_project: bool,
    runtime: str | None,
    stdin: TextIO | None,
    stdout: TextIO | None,
) -> int:
    """Legacy attached serving when no durable runtime was requested."""
    server = AdmetServer(simulated=simulated, project=project, create=create_project)
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    mode = "simulated" if simulated else "live"

    owner, publisher = _claim_runtime(runtime, server, mode)
    _warn(f"admet mcp ready ({mode}{f', runtime {runtime}' if runtime else ''})")
    try:
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
    except BaseException as exc:
        # Includes KeyboardInterrupt, which is not an Exception. A rig left
        # flowing because the operator pressed Ctrl-C is the worst case here.
        _shut_down(server, publisher, f"{type(exc).__name__}: {exc}")
        raise
    else:
        _shut_down(server, publisher, "stdin closed")
    finally:
        # Reached however the loop ended. The runtime must not be left saying
        # running after this process is gone.
        if owner is not None:
            owner.release()
    return 0


def _serve_relay(
    *,
    simulated: bool,
    project: str | None,
    create_project: bool,
    runtime: str,
    source: TextIO,
    sink: TextIO,
) -> int:
    """Attach this chat's stdio to the durable owner, then detach on EOF."""
    runtime_path = Path(runtime)
    control_path = runtime_path / CONTROL_SOCKET
    _ensure_owner(
        simulated=simulated,
        project=project,
        create_project=create_project,
        runtime=runtime_path,
    )
    _validate_owner(runtime_path, simulated=simulated, project=project)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(control_path))
    with client:
        reader = client.makefile("r", encoding="utf-8")
        try:
            for line in source:
                if not line.strip():
                    continue
                client.sendall(line.encode("utf-8") if line.endswith("\n") else (line + "\n").encode())
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    request = {"id": None}
                if "id" in request:
                    response = reader.readline()
                    if not response:
                        raise RuntimeError("the persistent ADMET owner disconnected")
                    sink.write(response)
                    sink.flush()
        finally:
            reader.close()
    return 0


def _ensure_owner(
    *, simulated: bool, project: str | None, create_project: bool, runtime: Path
) -> None:
    control_path = runtime / CONTROL_SOCKET
    if _socket_alive(control_path):
        time.sleep(0.2)
        return
    runtime.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "admet.app", "serve"]
    command.append("--simulated" if simulated else "--live")
    command.extend(["--runtime", str(runtime)])
    if project:
        command.extend(["--project", project])
    if create_project:
        command.append("--create-project")
    environment = {**os.environ, OWNER_CHILD_ENV: "1"}
    with (runtime / OWNER_LOG).open("a", encoding="utf-8") as log:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _socket_alive(control_path):
            time.sleep(0.2)
            return
        time.sleep(0.05)
    raise RuntimeError(f"persistent ADMET owner did not start; see {runtime / OWNER_LOG}")


def _socket_alive(path: Path) -> bool:
    if not path.exists():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _validate_owner(runtime: Path, *, simulated: bool, project: str | None) -> None:
    from admet.core.runtime import read_state

    state = read_state(runtime) or {}
    header = state.get("runtime") or {}
    expected_mode = "simulated" if simulated else "live"
    if header.get("mode") != expected_mode:
        raise RuntimeError(
            f"runtime already owns a {header.get('mode')} session; requested {expected_mode}"
        )
    existing_project = header.get("project")
    if project and existing_project and Path(existing_project) != Path(project):
        raise RuntimeError(
            f"runtime already serves project {existing_project}; requested {project}"
        )


def _serve_owner(
    *, simulated: bool, project: str | None, create_project: bool, runtime: str
) -> int:
    """Long-lived hardware owner; chat relays may come and go."""
    server = AdmetServer(simulated=simulated, project=project, create=create_project)
    mode = "simulated" if simulated else "live"
    owner, publisher = _claim_runtime(runtime, server, mode)
    control_path = Path(runtime) / CONTROL_SOCKET
    previous_handlers: dict[int, Any] = {}
    try:
        control_path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(control_path))
        os.chmod(control_path, 0o600)
        listener.listen(1)
        listener.setblocking(False)
        stop_requested = False
        emergency_requested = False

        def request_stop(_signum: int, _frame: Any) -> None:
            nonlocal stop_requested
            stop_requested = True

        def request_emergency(_signum: int, _frame: Any) -> None:
            nonlocal emergency_requested
            emergency_requested = True

        previous_handlers[signal.SIGTERM] = signal.getsignal(signal.SIGTERM)
        previous_handlers[signal.SIGINT] = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGUSR1"):
            previous_handlers[signal.SIGUSR1] = signal.getsignal(signal.SIGUSR1)
            signal.signal(signal.SIGUSR1, request_emergency)

        client: socket.socket | None = None
        pending = b""
        while not stop_requested:
            if emergency_requested:
                server.admet.emergency_stop("manual SIGUSR1 emergency stop")
                emergency_requested = False
            watched = [listener, *( [client] if client is not None else [])]
            readable, _, _ = select.select(watched, [], [], 0.1)
            if listener in readable:
                candidate, _ = listener.accept()
                if client is None:
                    client = candidate
                    pending = b""
                else:
                    candidate.close()
            if client is not None and client in readable:
                try:
                    chunk = client.recv(65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    client.close()
                    client = None
                    pending = b""
                    continue
                pending += chunk
                while b"\n" in pending:
                    raw, pending = pending.split(b"\n", 1)
                    if not raw.strip():
                        continue
                    try:
                        request = json.loads(raw)
                        response = handle(server, request)
                    except Exception as exc:
                        response = _error(None, -32603, f"{type(exc).__name__}: {exc}")
                    if response is not None:
                        try:
                            client.sendall(json.dumps(response).encode() + b"\n")
                        except OSError:
                            client.close()
                            client = None
                            pending = b""
                            break
        _shut_down(server, publisher, "manual SIGTERM shutdown")
    except BaseException as exc:
        _shut_down(server, publisher, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        try:
            listener.close()
        except UnboundLocalError:
            pass
        control_path.unlink(missing_ok=True)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if owner is not None:
            owner.release()
    return 0


def _shut_down(server: AdmetServer, publisher: Any, reason: str) -> None:
    """Leave the rig safe, then say so.

    The instrument comes first and the telemetry second: if publishing fails
    the channels are still at zero, whereas the other order could leave a rig
    flowing because a file could not be written.
    """
    try:
        stopped = server.admet.emergency_stop(f"server shutting down ({reason})")
        _warn(f"admet stopped the rig: {stopped.get('errors') or 'no errors'}")
    except Exception as exc:
        _warn(f"admet could not stop the rig cleanly: {exc}")
    try:
        cleaned = server.admet.engine_action("acquisition", "shutdown_instrument", {})
        cleanup_errors = cleaned.metadata.get("cleanup_errors", [])
        if cleanup_errors:
            _warn(f"admet cleanup reported errors: {cleanup_errors}")
    except Exception as exc:
        _warn(f"admet could not release instrument resources cleanly: {exc}")
    if publisher is not None:
        publisher.stop(state="stopped", reason=reason)


def _claim_runtime(
    runtime: str | None, server: AdmetServer, mode: str
) -> tuple[Any | None, Any | None]:
    """Take the runtime directory, and start publishing into it.

    The claim comes first. A second server pointed at the same directory is
    refused here, before anything opens an instrument that is already open.
    """
    if not runtime:
        return None, None
    from admet.core.runtime import RuntimeOwner, RuntimePublisher

    owner = RuntimeOwner(runtime)
    claim = owner.acquire()
    publisher = RuntimePublisher(
        runtime,
        observe=lambda: server.admet.do("observe"),
        events_since=lambda sequence: server.admet.protocol_events(
            after_sequence=sequence, limit=200
        ),
        mode=mode,
        pid=claim["pid"],
    )
    server.admet.attach_runtime(publisher)
    publisher.start()
    return owner, publisher


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
