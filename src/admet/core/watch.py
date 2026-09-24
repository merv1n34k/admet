"""A telemetry window onto the process that owns the instrument.

This reads two files and draws them. It opens no SDK, constructs no engine, and
never becomes a second instrument controller. Its only mutations are process
signals to the published owner PID: emergency stop and graceful shutdown. The
owner remains solely responsible for zeroing and releasing hardware.

Everything here is standard library and ANSI escapes. The renderer is a pure
function of the two files' contents, so what the eye sees can be asserted
against without a terminal.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import sys
import textwrap
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
    width: int = len(RULE),
    interactive: bool = False,
    notice: str = "",
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
                _footer(paint, interactive=interactive, notice=notice),
            ]
        )

    runtime = state.get("runtime") or {}
    observation = state.get("observation") or {}
    rule = "─" * max(1, width)
    lines = [
        _header(runtime, paint, now),
        rule,
        *_session(runtime, observation, paint),
        rule,
        *_fluidics_configuration(observation, paint),
        rule,
        *_channels(observation, paint),
        rule,
        *_camera(observation, paint),
        rule,
        *_plans(observation, paint, now, width),
        rule,
        *_protocol(observation, paint, width),
        rule,
        *_validation(observation, paint),
        rule,
        *_history(events or [], paint),
        rule,
        _footer(paint, interactive=interactive, notice=notice),
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


def _plans(
    observation: dict[str, Any], paint: _Paint, now: float | None, width: int
) -> list[str]:
    plans = observation.get("planned_protocols") or []
    lines = [paint("  PLANNED PROTOCOLS", DIM)]
    if not plans:
        return [*lines, paint("  none", DIM)]
    lines.append(
        paint(
            f"  {'PLAN ID':<16}{'OPERATION':<22}{'STATE':<10}{'AGE':>9}"
            f"{'STEPS':>6}{'EST. DURATION':>13}",
            DIM,
        )
    )
    selected = next((plan for plan in plans if plan.get("state") == "executing"), None)
    selected = selected or next((plan for plan in plans if plan.get("state") == "planned"), None)
    selected = selected or plans[-1]
    for plan in plans:
        age = _iso_age(plan.get("created_at"), now)
        age_text = _elapsed(age) if age is not None else "—"
        limits = ((plan.get("armed_safety_limits") or {}).get("pressure_mbar") or {})
        duration = plan.get("expected_duration_s")
        duration_text = f"{_number(duration)}s" if duration is not None else "open-ended"
        lines.append(
            f"  {str(plan.get('plan_id') or ''):<16.16}{str(plan.get('operation_id') or ''):<22.22}"
            f"{str(plan.get('state') or ''):<10}{age_text:>9}"
            f"{_number(plan.get('step_count'), '.0f'):>6}"
            f"{duration_text:>13}"
        )
        limit_text = ", ".join(
            f"ch{channel}={_number(value)} mbar" for channel, value in limits.items()
        ) or "UNARMED"
        lines.extend(_fold_plan_note("PRESSURE LIMIT: ", limit_text, paint, width, RED if not limits else DIM))
        lines.extend(_fold_plan_note("DIGEST: ", str(plan.get("digest") or "")[:12], paint, width, DIM))
        for warning in plan.get("warnings") or []:
            lines.extend(_fold_plan_note("WARNING: ", str(warning), paint, width, YELLOW))
        for assumption in plan.get("assumptions") or []:
            lines.extend(_fold_plan_note("NOTE: ", str(assumption), paint, width, DIM))
        if plan.get("unmet_guards"):
            lines.extend(
                _fold_plan_note("UNMET: ", ", ".join(plan["unmet_guards"]), paint, width, YELLOW)
            )
    lines.append(paint(f"  STEPS · {selected.get('plan_id')}", DIM))
    lines.append(
        paint(
            f"  {'#':<3}{'UNIT':<7}{'CONTROL':<9}{'TARGET':<15}{'TRIGGER':<10}"
            f"{'CONDITION':<13}{'ETA':<8}{'END':<8}",
            DIM,
        )
    )
    for step in selected.get("steps") or []:
        controls = _plan_controls(step)
        trigger = str(step.get("trigger_type") or "")
        if trigger == "confirmation":
            trigger = "confirm"
        timeout = step.get("timeout_s")
        condition = _plan_trigger_condition(step)
        if timeout is not None and "timeout" not in condition:
            condition += f" ≤{_number(timeout)}s"
        expected = step.get("expected_duration_s")
        expected_text = f"{_number(expected)}s" if expected is not None else "—"
        for row, (unit, control, target) in enumerate(controls):
            first = row == 0
            lines.append(
                f"  {(str(step.get('number') or '') if first else ''):<3}"
                f"{unit:<7.7}{control:<9.9}{target:<15.15}"
                f"{(trigger if first else ''):<10.10}{(condition if first else ''):<13.13}"
                f"{(expected_text if first else ''):<8.8}"
                f"{(str(step.get('on_complete') or '') if first else ''):<8.8}"
            )
        if step.get("confirmation"):
            lines.extend(
                _fold_plan_note("CONFIRM: ", str(step["confirmation"]), paint, width, BOLD, YELLOW)
            )
    return lines


def _fold_plan_note(
    prefix: str, value: str, paint: _Paint, width: int, *codes: str
) -> list[str]:
    indent = "    "
    continuation = " " * len(prefix)
    wrapped = textwrap.wrap(
        prefix + value,
        width=max(1, width - len(indent)),
        subsequent_indent=continuation,
        break_long_words=True,
        break_on_hyphens=False,
    ) or [prefix]
    return [indent + paint(line, *codes) for line in wrapped]


def _plan_controls(step: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows = [
        (f"ch{channel}", "flow", f"{_number(value)} uL/min")
        for channel, value in (step.get("flow_setpoints_ul_min") or {}).items()
    ]
    rows.extend(
        (f"ch{channel}", "pressure", f"{_number(value)} mbar")
        for channel, value in (step.get("pressure_setpoints_mbar") or {}).items()
    )
    return rows or [("—", "—", "—")]


def _plan_trigger_condition(step: dict[str, Any]) -> str:
    trigger = str(step.get("trigger_type") or "")
    params = step.get("trigger_params") or {}
    if trigger == "volume":
        return f"{_number(params.get('target_volume_ul'))} uL"
    if trigger == "time":
        return f"{_number(params.get('duration_s'))} s"
    if trigger == "confirmation":
        return "operator"
    if trigger == "stability":
        tolerance = _number(params.get("tolerance_ul_min"))
        window = _number(params.get("window_s"))
        return f"±{tolerance}, {window}s"
    if trigger == "threshold":
        return f"target {_number(params.get('target'))}"
    if trigger == "condition":
        minimum = _number(params.get("min_value"))
        maximum = _number(params.get("max_value"))
        return f"{minimum}..{maximum}"
    return "—"


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


def _protocol(observation: dict[str, Any], paint: _Paint, width: int) -> list[str]:
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
        lines.extend(
            _fold_plan_note(
                "WAITING: ", str(protocol["confirmation_message"]), paint, width, BOLD, YELLOW
            )
        )
        lines.extend(
            _fold_plan_note(
                "MCP: ",
                "confirm_protocol=proceed · skip_protocol=zero+skip · "
                "pause_protocol=zero+pause · stop_protocol=zero+abort",
                paint,
                width,
                DIM,
            )
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


def _footer(paint: _Paint, *, interactive: bool = False, notice: str = "") -> str:
    if notice:
        return paint(f"  {notice}", BOLD, YELLOW)
    if interactive:
        return paint(
            "  [q] QUIT  [↑↓/Pg] SCROLL  [E] E-STOP  [X] KILL  · PHYSICAL E-STOP WINS",
            DIM,
        )
    return paint(
        "  read-only telemetry snapshot · physical emergency stop remains authoritative",
        DIM,
    )


def _frame(
    directory: str,
    colour: bool,
    *,
    width: int = len(RULE),
    interactive: bool = False,
    notice: str = "",
) -> str:
    return render(
        read_state(directory),
        read_events(directory, limit=20),
        directory=str(directory),
        colour=colour,
        width=width,
        interactive=interactive,
        notice=notice,
    )


def _fit_frame(frame: str, columns: int, rows: int, *, offset: int = 0) -> str:
    """Draw a body viewport and keep its controls pinned on the last row."""
    available_rows = max(1, rows)
    source = frame.splitlines()
    if not source:
        lines = []
    elif available_rows == 1:
        lines = [source[-1]]
    elif len(source) <= available_rows:
        lines = source
    else:
        body = source[:-1]
        body_rows = available_rows - 1
        start = min(max(0, offset), max(0, len(body) - body_rows))
        lines = [*body[start : start + body_rows], source[-1]]
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

    notice = ""
    scroll_offset = 0
    with _watch_keys(out) as pressed:
        out.write("\x1b[?1049h\x1b[?25l")
        try:
            size = shutil.get_terminal_size(fallback=(80, 24))
            raw_frame = _frame(
                directory,
                colour,
                width=size.columns,
                interactive=True,
                notice=notice,
            )
            next_refresh = time.monotonic() + interval_s
            frame = _fit_frame(raw_frame, size.columns, size.lines, offset=scroll_offset)
            out.write("\x1b[H" + frame.replace("\n", "\x1b[K\n") + "\x1b[J")
            out.flush()
            while True:
                wait_s = max(0.0, next_refresh - time.monotonic())
                key = pressed(wait_s)
                now = time.monotonic()
                if key is None and now < next_refresh:
                    # An incomplete terminal escape sequence arrived. Read its
                    # remaining bytes immediately instead of waiting for the
                    # next telemetry refresh.
                    continue
                if key in {"q", "Q"}:
                    break
                refresh = now >= next_refresh
                if key == "E":
                    notice = _signal_owner(directory, signal.SIGUSR1, "emergency stop requested")
                    refresh = True
                elif key == "X":
                    notice = _signal_owner(directory, signal.SIGTERM, "server shutdown requested")
                    refresh = True
                latest_size = shutil.get_terminal_size(fallback=(80, 24))
                if latest_size != size:
                    size = latest_size
                    refresh = True
                if refresh:
                    raw_frame = _frame(
                        directory,
                        colour,
                        width=size.columns,
                        interactive=True,
                        notice=notice,
                    )
                    next_refresh = now + interval_s
                body_rows = max(0, len(raw_frame.splitlines()) - 1)
                viewport_rows = max(0, size.lines - 1)
                max_scroll = max(0, body_rows - viewport_rows)
                if key in {"down", "j"}:
                    scroll_offset = min(max_scroll, scroll_offset + 1)
                elif key in {"up", "k"}:
                    scroll_offset = max(0, scroll_offset - 1)
                elif key == "page_down":
                    scroll_offset = min(max_scroll, scroll_offset + max(1, viewport_rows - 1))
                elif key == "page_up":
                    scroll_offset = max(0, scroll_offset - max(1, viewport_rows - 1))
                elif key == "home":
                    scroll_offset = 0
                elif key == "end":
                    scroll_offset = max_scroll
                frame = _fit_frame(
                    raw_frame,
                    size.columns,
                    size.lines,
                    offset=scroll_offset,
                )
                out.write("\x1b[H" + frame.replace("\n", "\x1b[K\n") + "\x1b[J")
                out.flush()
        except KeyboardInterrupt:
            pass
        finally:
            out.write("\x1b[?25h\x1b[?1049l")
            out.flush()
    return 0


def _signal_owner(directory: str, signum: int, success: str) -> str:
    state = read_state(directory)
    runtime = (state or {}).get("runtime") or {}
    pid = runtime.get("pid")
    age = heartbeat_age_s(state)
    if not isinstance(pid, int) or pid <= 1:
        return "REFUSED: runtime has no valid owner PID"
    if runtime.get("state") != "running":
        return "REFUSED: runtime owner is not running"
    if age is None or age > STALE_AFTER_S:
        return "REFUSED: runtime heartbeat is stale; PID identity is not trusted"
    try:
        os.kill(pid, signum)
    except (OSError, ValueError) as exc:
        return f"FAILED: {exc}"
    return success


def _wants_colour(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


class _watch_keys:
    """Read one-key lifecycle controls and restore the terminal afterwards.

    If stdin is not a terminal there is nothing to read and nothing to restore,
    so this becomes a loop that only Ctrl-C ends.
    """

    def __init__(self, out: TextIO):
        self._out = out
        self._settings: Any = None
        self._fd: int | None = None
        self._buffer = b""

    def __enter__(self):
        try:
            import termios
            import tty

            if not sys.stdin.isatty():
                return self._wait_without_input
            self._fd = sys.stdin.fileno()
            self._termios = termios
            self._settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:
            self._settings = None
            return lambda: False
        return self._pressed

    @staticmethod
    def _wait_without_input(timeout_s: float = 0.0) -> None:
        time.sleep(max(0.0, timeout_s))
        return None

    def _pressed(self, timeout_s: float = 0.0) -> str | None:
        import select

        buffered = self._pop_buffered_key()
        if buffered is not None:
            return buffered
        ready, _, _ = select.select([sys.stdin], [], [], max(0.0, timeout_s))
        if not ready:
            return None
        if self._fd is None:
            return None
        self._buffer += os.read(self._fd, 16)
        return self._pop_buffered_key()

    def _pop_buffered_key(self) -> str | None:
        keys = {
            b"\x1b[A": "up",
            b"\x1b[B": "down",
            b"\x1b[5~": "page_up",
            b"\x1b[6~": "page_down",
            b"\x1b[H": "home",
            b"\x1b[F": "end",
        }
        for sequence, name in keys.items():
            if self._buffer.startswith(sequence):
                self._buffer = self._buffer[len(sequence) :]
                return name
        if self._buffer.startswith(b"\x1b") and any(
            sequence.startswith(self._buffer) for sequence in keys
        ):
            return None
        value, self._buffer = self._buffer[:1], self._buffer[1:]
        return value.decode(errors="ignore") if value else None

    def __exit__(self, *_exc: Any) -> None:
        if self._settings is not None and self._fd is not None:
            # Restored even on an exception: a terminal left in cbreak mode
            # stops echoing, which looks like a broken shell.
            self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._settings)
