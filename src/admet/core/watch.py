"""A read-only window onto the process that owns the instrument.

This reads two files and draws them. It opens no SDK, constructs no engine, and
has no way to command anything -- the only key it answers to is the one that
quits. That is not a limitation to be lifted later: a monitor that could also
act is a second controller, and the whole arrangement depends on there being
one.

Everything here is standard library and ANSI escapes. The renderer is a pure
function of the two files' contents, so what the eye sees can be asserted
against without a terminal.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from typing import Any, TextIO

from admet.core.runtime import heartbeat_age_s, read_events, read_state

# Past this, the process has missed several publishes and what is on screen can
# no longer be trusted as current.
STALE_AFTER_S = 3.0
REFRESH_S = 0.5

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"
REVERSE = "\x1b[7m"

RULE = "─" * 78
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class _Paint:
    """Colour, unless the output is not a terminal or the user said no."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        return f"{''.join(codes)}{text}{RESET}"


def _number(value: Any, spec: str = ".1f") -> str:
    """A measurement, or a dash where there has not been one.

    Never a zero standing in for an absent reading: on this screen that would
    be indistinguishable from a channel sitting still.
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return str(value)


def _elapsed(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _bar(progress: Any, width: int = 24) -> str:
    if progress is None:
        return " " * width
    filled = max(0, min(width, int(float(progress) * width)))
    return "█" * filled + "░" * (width - filled)


def render(
    state: dict[str, Any] | None,
    events: list[dict[str, Any]] | None = None,
    *,
    directory: str = "",
    colour: bool = False,
    now: float | None = None,
) -> str:
    """One frame, from the published files alone."""
    paint = _Paint(colour)
    if state is None:
        return "\n".join(
            [
                paint("ADMET MONITOR", BOLD),
                "",
                f"Nothing is publishing into {directory or 'this directory'} yet.",
                "",
                paint("Start the controller with:", DIM),
                paint(f"  admet --runtime {directory or 'PATH'} serve --simulated", DIM),
                "",
                _footer(paint),
            ]
        )

    runtime = state.get("runtime") or {}
    observation = state.get("observation") or {}
    lines = [
        _header(runtime, paint, now),
        RULE,
        *_session(runtime, observation, paint),
        RULE,
        *_fluidics_configuration(observation, paint),
        RULE,
        *_channels(observation, paint),
        RULE,
        *_camera(observation, paint),
        RULE,
        *_plans(observation, paint, now),
        RULE,
        *_protocol(observation, paint),
        RULE,
        *_validation(observation, paint),
        RULE,
        *_history(events or [], paint),
        RULE,
        _footer(paint),
    ]
    return "\n".join(lines)


def _header(runtime: dict[str, Any], paint: _Paint, now: float | None) -> str:
    mode = str(runtime.get("mode") or "?").upper()
    badge = paint(f" {mode} ", REVERSE, RED if mode == "LIVE" else CYAN)
    pid = runtime.get("pid")
    started = runtime.get("started_at") or ""
    uptime = ""
    if started:
        try:
            from datetime import datetime

            begin = datetime.fromisoformat(started)
            reference = datetime.now().astimezone() if now is None else None
            if reference is not None:
                uptime = f" · up {_elapsed((reference - begin).total_seconds())}"
        except ValueError:
            uptime = ""
    return f"{paint('ADMET', BOLD)} {badge}  {paint(f'pid {pid}{uptime}', DIM)}"


def _session(runtime: dict[str, Any], observation: dict[str, Any], paint: _Paint) -> list[str]:
    age = heartbeat_age_s({"runtime": runtime})
    process_state = str(runtime.get("state") or "?")
    if age is None:
        beat = paint("no heartbeat", RED)
    elif age > STALE_AFTER_S:
        beat = paint(f"STALE {age:.1f}s — this screen is not current", BOLD, RED)
    else:
        beat = paint(f"{age:.1f}s ago", GREEN if process_state == "running" else DIM)
    state_colour = GREEN if process_state == "running" else YELLOW
    connection = observation.get("connection") or {}
    recording = observation.get("recording") or {}
    safety = observation.get("safety") or {}

    if safety.get("tripped"):
        safety_line = paint(f"TRIPPED — {safety.get('reason') or 'no reason given'}", BOLD, RED)
    elif safety.get("armed"):
        limits = ", ".join(f"{k} {v}" for k, v in (safety.get("limits") or {}).items())
        safety_line = paint(f"armed{f' ({limits})' if limits else ''}", GREEN)
    else:
        safety_line = paint("not armed", DIM)

    project = (observation.get("project") or {}).get("path") or paint("none open", DIM)
    fluidics = (
        paint("connected", GREEN) if connection.get("fluidics") else paint("disconnected", DIM)
    )
    polling = paint("polling", GREEN) if observation.get("polling") else paint("not polling", DIM)
    if recording.get("active"):
        record = paint(f"recording {recording.get('id') or ''}", GREEN)
    else:
        record = paint("not recording", DIM)

    lines = [
        f"  {'process':<11}{paint(process_state, state_colour)}   heartbeat {beat}",
        f"  {'project':<11}{project}",
        f"  {'rig':<11}{fluidics}   {polling}   {record}",
        f"  {'safety':<11}{safety_line}",
    ]
    csv_path = recording.get("fluidics_csv")
    if csv_path:
        lines.append(f"  {'csv':<11}{paint(str(csv_path), DIM)}")
    return lines


def _channels(observation: dict[str, Any], paint: _Paint) -> list[str]:
    channels = observation.get("channels") or []
    # Columns are padded before colour is applied. Padding a string that
    # already contains escape codes counts them as width, which pulls every
    # row out of line by however much colour it happens to carry.
    heading = paint(
        f"  {'CH':<4}{'LABEL':<10}{'MODE':<7}{'REQUESTED':>10}"
        f"{'PRESSURE mean±sd':>22}{'FLOW mean±sd':>20}{'MARGIN':>10}"
        f"{'VOL':>9}{'AGE':>8}  STABLE",
        DIM,
    )
    if not channels:
        return [heading, paint("  no channels — the rig is not connected", DIM)]

    rows = [heading]
    for channel in channels:
        mode = str(channel.get("mode") or "off")
        requested = (
            _number(channel.get("requested_flow_ul_min"))
            if mode == "flow"
            else _number(channel.get("requested_pressure_mbar"))
        )
        pressure = (
            f"{_number(channel.get('pressure_mbar'))} "
            f"({_number(channel.get('pressure_mean_mbar'))}±"
            f"{_number(channel.get('pressure_std_mbar'))})"
        )
        flow = (
            f"{_number(channel.get('flow_ul_min'), '.2f')} "
            f"({_number(channel.get('flow_mean_ul_min'), '.2f')}±"
            f"{_number(channel.get('flow_std_ul_min'), '.2f')})"
        )
        age_value = channel.get("measurement_age_s")
        age = f"{_number(age_value)}s"
        if age_value is not None and float(age_value) > STALE_AFTER_S:
            age = paint(f"STALE {age}", BOLD, RED)
        stable = channel.get("stable")
        stability = (
            paint("yes", GREEN) if stable else (paint("no", YELLOW) if stable is False else "—")
        )
        rows.append(
            f"  {str(channel.get('index', '?')):<4}{str(channel.get('label') or ''):<10}"
            f"{mode:<7}{requested:>10}{pressure:>22}{flow:>20}"
            f"{_number(channel.get('pressure_margin_mbar')):>10}"
            f"{_number(channel.get('volume_ul'), '.2f'):>9}{age:>8}  {stability}"
        )
    return rows


def _fluidics_configuration(observation: dict[str, Any], paint: _Paint) -> list[str]:
    configuration = observation.get("fluidics_configuration") or {}
    channels = configuration.get("channels") or []
    applied = configuration.get("corrections_applied")
    status = paint("APPLIED", GREEN) if applied else paint("NOT APPLIED", YELLOW)
    lines = [f"  FLUID CONFIGURATION · corrections {status}"]
    lines.append(
        paint(
            f"  {'CH':<4}{'KEY':<10}{'LABEL':<11}{'UNIT':<6}{'CALIBRATION':<13}"
            f"{'SCALE':>10}{'OFFSET':>11}{'QUADRATIC':>12}",
            DIM,
        )
    )
    if not channels:
        return [*lines, paint("  configuration unavailable", DIM)]
    for channel in channels:
        lines.append(
            f"  {str(channel.get('index', '?')):<4}{str(channel.get('key') or ''):<10}"
            f"{str(channel.get('label') or ''):<11}{str(channel.get('flow_unit') or ''):<6}"
            f"{str(channel.get('calibration') or '—'):<13}"
            f"{_number(channel.get('scale'), '.6g'):>10}"
            f"{_number(channel.get('offset'), '.6g'):>11}"
            f"{_number(channel.get('quadratic'), '.6g'):>12}"
        )
    return lines


def _camera(observation: dict[str, Any], paint: _Paint) -> list[str]:
    camera = observation.get("camera") or {}
    state = "connected" if camera.get("connected") else "disconnected"
    live = "live" if camera.get("live") else "not live"
    identity = " ".join(
        str(value) for value in (camera.get("model"), camera.get("serial_number")) if value
    ) or "—"
    size = (
        f"{_number(camera.get('frame_width'), '.0f')}×"
        f"{_number(camera.get('frame_height'), '.0f')}"
    )
    age = camera.get("latest_frame_age_s")
    age_text = f"{_number(age)}s"
    if age is not None and float(age) > STALE_AFTER_S:
        age_text = paint(f"STALE {age_text}", BOLD, RED)
    return [
        paint("  CAMERA", DIM),
        f"  {'state':<11}{state}   {live}   {identity}",
        f"  {'image':<11}{size}   {camera.get('pixel_format') or '—'}   "
        f"configured {_number(camera.get('configured_frame_rate_hz'))} fps   "
        f"measured {_number(camera.get('measured_frame_rate_hz'))} fps",
        f"  {'capture':<11}frames {_number(camera.get('frame_count'), '.0f')}   "
        f"dropped {_number(camera.get('dropped_frame_count'), '.0f')}   age {age_text}   "
        f"recording {'yes' if camera.get('recording') else 'no'}",
        f"  {'settings':<11}exposure {_number(camera.get('exposure_us'))} us   "
        f"gain {_number(camera.get('gain'))}   transport {camera.get('transport') or '—'}",
    ]


def _plans(observation: dict[str, Any], paint: _Paint, now: float | None) -> list[str]:
    plans = observation.get("planned_protocols") or []
    lines = [paint("  PLANNED PROTOCOLS", DIM)]
    if not plans:
        return [*lines, paint("  none", DIM)]
    lines.append(
        paint(
            f"  {'PLAN ID':<22}{'OPERATION':<24}{'STATE':<11}{'AGE':>7}"
            f"{'STEPS':>7}{'DURATION':>11}{'LIMIT':>9}  DIGEST",
            DIM,
        )
    )
    selected = next((plan for plan in plans if plan.get("state") == "executing"), None)
    selected = selected or next((plan for plan in plans if plan.get("state") == "planned"), None)
    selected = selected or plans[-1]
    for plan in plans:
        age = _iso_age(plan.get("created_at"), now)
        limits = ((plan.get("armed_safety_limits") or {}).get("pressure_mbar") or {})
        limit = next(iter(limits.values()), None)
        lines.append(
            f"  {str(plan.get('plan_id') or ''):<22.22}{str(plan.get('operation_id') or ''):<24.24}"
            f"{str(plan.get('state') or ''):<11}{_number(age):>6}s"
            f"{_number(plan.get('step_count'), '.0f'):>7}"
            f"{_number(plan.get('expected_duration_s')):>10}s"
            f"{_number(limit):>9}  {str(plan.get('digest') or '')[:12]}"
        )
        for warning in plan.get("warnings") or []:
            lines.append(f"    {paint('warning: ' + str(warning), YELLOW)}")
        if plan.get("unmet_guards"):
            lines.append(f"    unmet: {', '.join(plan['unmet_guards'])}")
    lines.append(paint(f"  STEPS · {selected.get('plan_id')}", DIM))
    lines.append(
        paint(f"  {'#':<4}{'NAME':<28}{'SETPOINTS':<30}{'TRIGGER':<22}{'END':<8}", DIM)
    )
    for step in selected.get("steps") or []:
        setpoints = _plan_setpoints(step)
        trigger = str(step.get("trigger_type") or "")
        timeout = step.get("timeout_s")
        if timeout is not None:
            trigger += f" ≤{_number(timeout)}s"
        lines.append(
            f"  {str(step.get('number') or ''):<4}{str(step.get('name') or ''):<28.28}"
            f"{setpoints:<30.30}{trigger:<22.22}{str(step.get('on_complete') or ''):<8}"
        )
        if step.get("confirmation"):
            lines.append(f"      {paint('CONFIRM: ' + str(step['confirmation']), BOLD, YELLOW)}")
    return lines


def _plan_setpoints(step: dict[str, Any]) -> str:
    flow = ", ".join(
        f"ch{k}={_number(v)}uL/m" for k, v in (step.get("flow_setpoints_ul_min") or {}).items()
    )
    pressure = ", ".join(
        f"ch{k}={_number(v)}mbar"
        for k, v in (step.get("pressure_setpoints_mbar") or {}).items()
    )
    return ", ".join(value for value in (flow, pressure) if value) or "—"


def _iso_age(created_at: Any, now: float | None) -> float | None:
    if not created_at:
        return None
    try:
        from datetime import datetime

        created = datetime.fromisoformat(str(created_at))
        reference = datetime.now().astimezone()
        return max(0.0, (reference - created).total_seconds())
    except (TypeError, ValueError):
        return None


def _protocol(observation: dict[str, Any], paint: _Paint) -> list[str]:
    protocol = observation.get("protocol") or {}
    state = str(protocol.get("state") or "idle")
    step_index = protocol.get("step_index")
    total = protocol.get("total_steps")
    where = f"step {step_index + 1}/{total}" if step_index is not None and total else ""
    colour = GREEN if state == "running" else (YELLOW if state == "paused" else DIM)
    lines = [
        f"  {'protocol':<11}{paint(state, colour)}   {where}   {protocol.get('step_name') or ''}",
        f"  {'':<11}{_bar(protocol.get('progress'))} "
        f"{_number((protocol.get('progress') or 0) * 100, '.0f')}%   "
        f"{paint('outcome ' + str(protocol.get('outcome') or '—'), DIM)}",
    ]
    if protocol.get("confirmation_message"):
        lines.append(
            f"  {'waiting':<11}"
            + paint(str(protocol["confirmation_message"]), BOLD, YELLOW)
            + paint("  (answer with confirm_protocol over MCP)", DIM)
        )
    if protocol.get("error"):
        lines.append(f"  {'error':<11}" + paint(str(protocol["error"]), BOLD, RED))
    return lines


def _validation(observation: dict[str, Any], paint: _Paint) -> list[str]:
    validation = observation.get("validation") or {}
    if not validation.get("active") and not validation.get("id"):
        return [f"  {'validation':<11}{paint('none running', DIM)}"]

    classification = validation.get("classification")
    colours = {"pass": GREEN, "capacity_limited": YELLOW, "unstable": YELLOW, "invalid": RED}
    lines = [
        f"  {'validation':<11}{validation.get('id') or ''}   "
        f"{validation.get('state') or ''}   "
        f"target {_number(validation.get('current_target_ul_min'))} uL/min   "
        + (
            paint(str(classification), BOLD, colours.get(str(classification), DIM))
            if classification
            else paint("unclassified", DIM)
        )
    ]
    for name, path in (validation.get("artifacts") or {}).items():
        lines.append(f"  {'':<11}{paint(f'{name}: {path}', DIM)}")
    if validation.get("error"):
        lines.append(f"  {'':<11}" + paint(str(validation["error"]), BOLD, RED))
    return lines


def _history(events: list[dict[str, Any]], paint: _Paint) -> list[str]:
    if not events:
        return [paint("  no events yet", DIM)]
    lines = []
    for entry in events[-5:]:
        at = str(entry.get("at") or "")[11:23]
        summary = _summarise(entry)
        lines.append(f"  {paint(at, DIM)}  {str(entry.get('type') or ''):<11}{summary[:52]}")
    return lines


def _summarise(entry: dict[str, Any]) -> str:
    """One line saying what happened, not the first four fields of it."""
    detail = entry.get("detail")
    if not isinstance(detail, dict):
        return str(detail if detail is not None else "")
    if entry.get("type") == "protocol":
        # A protocol event's own bookkeeping -- sequence, timestamps -- is the
        # least interesting part of it. What happened is the outcome and where.
        where = detail.get("step_name") or f"step {detail.get('step_index')}"
        note = detail.get("confirmation_message") or detail.get("error") or ""
        return f"{detail.get('outcome') or detail.get('state') or ''} · {where}" + (
            f" · {note}" if note else ""
        )
    interesting = {k: v for k, v in detail.items() if v not in (None, "", {}, [])}
    return " ".join(f"{k}={v}" for k, v in list(interesting.items())[:4]) or "—"


def _footer(paint: _Paint) -> str:
    return paint(
        "  read-only monitor · it cannot stop the rig · "
        "the physical emergency stop remains authoritative · q to quit",
        DIM,
    )


def _frame(directory: str, colour: bool) -> str:
    return render(
        read_state(directory),
        read_events(directory, limit=20),
        directory=str(directory),
        colour=colour,
    )


def _fit_frame(frame: str, columns: int, rows: int) -> str:
    """Keep a live frame inside the terminal so redraws cannot scroll it."""
    available_rows = max(1, rows)
    lines = frame.splitlines()[:available_rows]
    return "\n".join(_clip_ansi(line, max(1, columns)) for line in lines)


def _clip_ansi(line: str, width: int) -> str:
    result: list[str] = []
    visible = 0
    position = 0
    for match in ANSI_RE.finditer(line):
        text = line[position:match.start()]
        remaining = width - visible
        if remaining <= 0:
            break
        result.append(text[:remaining])
        visible += min(len(text), remaining)
        if visible < width:
            result.append(match.group())
        position = match.end()
    if visible < width:
        result.append(line[position:position + width - visible])
    if ANSI_RE.search(line):
        result.append(RESET)
    return "".join(result)


def watch(
    directory: str,
    *,
    once: bool = False,
    interval_s: float = REFRESH_S,
    stream: TextIO | None = None,
) -> int:
    """Draw the runtime until told to stop. Returns a process exit code."""
    out = stream or sys.stdout
    colour = _wants_colour(out)

    if once:
        out.write(_frame(directory, colour) + "\n")
        out.flush()
        return 0

    with _quit_key(out) as pressed_quit:
        out.write("\x1b[?1049h\x1b[?25l")
        try:
            next_refresh = time.monotonic()
            while not pressed_quit():
                size = shutil.get_terminal_size(fallback=(80, 24))
                frame = _fit_frame(_frame(directory, colour), size.columns, size.lines)
                out.write("\x1b[H" + frame.replace("\n", "\x1b[K\n") + "\x1b[J")
                out.flush()
                next_refresh += interval_s
                time.sleep(max(0.0, next_refresh - time.monotonic()))
        except KeyboardInterrupt:
            pass
        finally:
            out.write("\x1b[?25h\x1b[?1049l")
            out.flush()
    return 0


def _wants_colour(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


class _quit_key:
    """Answer to q, and leave the terminal exactly as it was found.

    If stdin is not a terminal there is nothing to read and nothing to restore,
    so this becomes a loop that only Ctrl-C ends.
    """

    def __init__(self, out: TextIO):
        self._out = out
        self._settings: Any = None
        self._fd: int | None = None

    def __enter__(self):
        try:
            import termios
            import tty

            if not sys.stdin.isatty():
                return lambda: False
            self._fd = sys.stdin.fileno()
            self._termios = termios
            self._settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:
            self._settings = None
            return lambda: False
        return self._pressed

    def _pressed(self) -> bool:
        import select

        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if not ready:
            return False
        return sys.stdin.read(1).lower() == "q"

    def __exit__(self, *_exc: Any) -> None:
        if self._settings is not None and self._fd is not None:
            # Restored even on an exception: a terminal left in cbreak mode
            # stops echoing, which looks like a broken shell.
            self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._settings)
