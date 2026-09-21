"""One-way telemetry from the process that owns the instrument.

This is not a control surface. It is a directory the serving process writes and
anything else may read:

    <runtime>/owner.lock    who owns the instrument
    <runtime>/state.json    replaced whole, several times a second
    <runtime>/events.jsonl  appended when something changes, never on a tick

The split matters. state.json answers "what is true now" and is overwritten, so
a reader always gets one coherent picture and never a half-written one -- it is
written to a temporary file in the same directory and renamed, which is atomic.
events.jsonl answers "what happened" and is only ever appended to, so nothing
that happened can be lost by a later refresh.

The lock is what makes "one long-lived process owns the Fluigent connection"
true rather than hoped for. It is an advisory flock on an ordinary file: a
crashed process releases it when the kernel closes its file descriptor, so a
stale file cannot lock everyone out for good.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:  # POSIX only, which is where this runs.
    import fcntl
except ImportError:  # pragma: no cover - documented in acquire()
    fcntl = None  # type: ignore[assignment]

STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"
LOCK_FILE = "owner.lock"

# Four times a second: fast enough to watch a flow settle, slow enough that the
# monitor is reading whole files rather than fighting the writer.
PUBLISH_INTERVAL_S = 0.25


class RuntimeBusy(Exception):
    """Another process already owns this runtime directory."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class RuntimeOwner:
    """The exclusive claim on one runtime directory.

    Held for as long as the serving process runs. A second server pointed at the
    same directory is refused here rather than discovering the conflict by
    opening the same instrument twice.
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.path = self.directory / LOCK_FILE
        self._handle: Any = None

    def acquire(self) -> dict[str, Any]:
        if fcntl is None:  # pragma: no cover - POSIX only
            raise RuntimeError("runtime locking needs fcntl, which this platform lacks")
        self.directory.mkdir(parents=True, exist_ok=True)
        # Opened, not created exclusively: a lock file left behind by a crash
        # must not keep everyone out, and the flock is what actually decides.
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            held_by = self._read_holder(handle)
            handle.close()
            raise RuntimeBusy(
                f"{self.directory} is already served by {held_by}; "
                f"one process owns the instrument, so stop that one or use another "
                f"--runtime directory"
            ) from exc

        claim = {"pid": os.getpid(), "started_at": _now()}
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(claim))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return claim

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    @staticmethod
    def _read_holder(handle: Any) -> str:
        try:
            handle.seek(0)
            claim = json.loads(handle.read() or "{}")
        except (OSError, json.JSONDecodeError):
            return "another process"
        pid = claim.get("pid")
        started = claim.get("started_at")
        if pid and started:
            return f"pid {pid}, started {started}"
        return f"pid {pid}" if pid else "another process"

    def __enter__(self) -> RuntimeOwner:
        self.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


class RuntimePublisher:
    """Publishes what the serving process can see, for anyone watching.

    Takes an observe callable rather than the service itself: this only ever
    reads, and taking the narrow thing makes that true by construction.
    """

    def __init__(
        self,
        directory: str | Path,
        observe: Callable[[], dict[str, Any]],
        events_since: Callable[[int], list[dict[str, Any]]],
        *,
        mode: str,
        pid: int | None = None,
        interval_s: float = PUBLISH_INTERVAL_S,
    ):
        self.directory = Path(directory)
        self.mode = mode
        self.pid = pid or os.getpid()
        self.started_at = _now()
        self._observe = observe
        self._events_since = events_since
        self._interval_s = interval_s

        self._state = "starting"
        self._stop_reason = ""
        self._heartbeat = self.started_at
        self._project: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # What was last published, so events.jsonl records changes rather than
        # repeating the same line four times a second.
        self._previous: dict[str, Any] = {}
        self._protocol_sequence = 0
        self._event_sequence = 0

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._state = "running"
        self.publish_once()
        self._thread = threading.Thread(target=self._loop, name="RuntimePublisher", daemon=True)
        self._thread.start()

    def stop(self, *, state: str = "stopped", reason: str = "") -> None:
        """Leave the runtime saying it stopped, so a reader is not left guessing.

        Without this the last published state says running for ever, and a
        monitor can only infer the truth from a heartbeat going stale.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._state = state
        self._stop_reason = reason
        # The state change is recorded by the publish itself. Appending it here
        # as well would put the same thing in the history twice.
        self.publish_once()

    def describe(self) -> dict[str, Any]:
        """What observe reports about the runtime."""
        with self._lock:
            return {
                "publishing": self._state == "running",
                "path": str(self.directory),
                "pid": self.pid,
                "mode": self.mode,
                "state": self._state,
                "started_at": self.started_at,
                "heartbeat": self._heartbeat,
            }

    # -- publishing ---------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self.publish_once()
            except Exception as exc:  # a monitor going blind must not stop a run
                self._append({"type": "error", "where": "publisher", "message": str(exc)})

    def publish_once(self) -> dict[str, Any]:
        observation = self._observe()
        with self._lock:
            self._heartbeat = _now()
            self._project = (observation.get("project") or {}).get("path")
            header = {
                "pid": self.pid,
                "mode": self.mode,
                "state": self._state,
                "started_at": self.started_at,
                "heartbeat": self._heartbeat,
                "project": self._project,
                "runtime": str(self.directory),
            }
        payload = {"runtime": header, "observation": observation}
        self._write_atomically(payload)
        self._record_changes(observation, header)
        return payload

    def _write_atomically(self, payload: dict[str, Any]) -> None:
        """Replace state.json whole, so no reader ever sees half of one."""
        target = self.directory / STATE_FILE
        temporary = self.directory / f".{STATE_FILE}.{os.getpid()}.tmp"
        with temporary.open("w") as handle:
            json.dump(payload, handle, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)  # atomic within one directory

    # -- the append-only history --------------------------------------------
    def _record_changes(self, observation: dict[str, Any], header: dict[str, Any]) -> None:
        watched = {
            "process": {"state": header["state"], "reason": self._stop_reason}
            if self._stop_reason
            else {"state": header["state"]},
            "project": header["project"],
            "connection": observation.get("connection"),
            "polling": observation.get("polling"),
            "recording": observation.get("recording"),
            "safety": observation.get("safety"),
            "validation": (observation.get("validation") or {}).get("state"),
        }
        for name, value in watched.items():
            if self._previous.get(name, _UNSET) != value:
                self._previous[name] = value
                self._append({"type": name, "detail": value})

        # Protocol events are already sequenced, so ask for what has not been
        # published rather than diffing them.
        for event in self._events_since(self._protocol_sequence):
            self._protocol_sequence = max(self._protocol_sequence, event["sequence"])
            self._append({"type": "protocol", "detail": event})

    def _append(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._event_sequence += 1
            line = {"seq": self._event_sequence, "at": _now(), **entry}
        try:
            with (self.directory / EVENTS_FILE).open("a") as handle:
                handle.write(json.dumps(line, default=str) + "\n")
        except OSError:
            pass  # telemetry must never take the instrument down with it


class _Unset:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET = _Unset()


def read_state(directory: str | Path) -> dict[str, Any] | None:
    """The last published state, or None if nothing has published yet.

    A half-written file is impossible by construction, but a file being replaced
    as it is read can still be missed; that is a reason to try again, not to
    report a stopped process.
    """
    path = Path(directory) / STATE_FILE
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def read_events(directory: str | Path, *, limit: int = 20) -> list[dict[str, Any]]:
    """The most recent entries from the append-only history, oldest first."""
    path = Path(directory) / EVENTS_FILE
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    entries = []
    for line in lines[-limit:] if limit else lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a line being appended as we read is not a corrupt file
    return entries


def heartbeat_age_s(state: dict[str, Any] | None) -> float | None:
    """How long since the serving process last said anything. None if unknown."""
    heartbeat = ((state or {}).get("runtime") or {}).get("heartbeat")
    if not heartbeat:
        return None
    try:
        published = datetime.fromisoformat(heartbeat)
    except ValueError:
        return None
    return max(0.0, (datetime.now().astimezone() - published).total_seconds())
