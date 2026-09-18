"""Turning a read document into a protocol, or explaining why it is not one.

Validation collects every problem it can find rather than stopping at the first,
because an operator fixing a protocol wants the list, not a game of whack-a-mole.
Errors mean the protocol cannot run; warnings mean it can, but something is worth
knowing.

Unknown keys are errors. A document that asks for something this version does not
understand is more likely to be a typo or a newer protocol than a thing safe to
ignore -- and ignoring it silently would mean a step the operator wrote never
ran, while the run reported success.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from admet.workflows.protocols.loader import ProtocolSource
from admet.workflows.protocols.model import (
    PROTOCOL_VERSION,
    Capabilities,
    CaptureWindow,
    Comparison,
    Confirm,
    Field,
    Foreach,
    Matrix,
    OperatorInput,
    ParameterSpec,
    ParameterType,
    Problem,
    Protocol,
    Repeat,
    SafetyLimits,
    SetFlow,
    SetPressure,
    StartRecording,
    Step,
    StopChannel,
    StopRecording,
    Summary,
    WaitCondition,
    WaitStable,
    WaitTime,
    WaitVolume,
)

IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
SUBSTITUTION = re.compile(r"\$\{([^}]*)\}")

TOP_LEVEL_KEYS = frozenset(
    {
        "protocol_version",
        "id",
        "name",
        "description",
        "capabilities",
        "parameters",
        "safety",
        "steps",
        "analysis",
    }
)


@dataclass
class ValidationReport:
    """What validation found, and the protocol if it survived."""

    errors: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)
    protocol: Protocol | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and self.protocol is not None

    def error(self, message: str, where: str = "") -> None:
        self.errors.append(Problem(message, where))

    def warn(self, message: str, where: str = "") -> None:
        self.warnings.append(Problem(message, where))


def validate(source: ProtocolSource) -> ValidationReport:
    """Parse and check one document. Never raises for a bad protocol."""
    report = ValidationReport()
    raw = source.raw

    version = raw.get("protocol_version")
    if version is None:
        report.error("protocol_version is required", "protocol_version")
    elif not isinstance(version, int) or isinstance(version, bool):
        report.error(f"must be an integer, found {_name(version)}", "protocol_version")
    elif version != PROTOCOL_VERSION:
        report.error(
            f"this build understands protocol_version {PROTOCOL_VERSION}, document is {version}",
            "protocol_version",
        )

    _reject_unknown(raw, TOP_LEVEL_KEYS, "", report)

    protocol_id = _identifier(raw.get("id"), "id", report)
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        report.error("a human-readable name is required", "name")
        name = protocol_id

    capabilities = _capabilities(raw.get("capabilities"), report)
    parameters = _parameters(raw.get("parameters"), report)
    known = {spec.name for spec in parameters}

    safety = _safety(raw.get("safety"), capabilities, known, report)
    steps = _steps(raw.get("steps"), "steps", capabilities, known, report, depth=0)
    if not steps and not report.errors:
        report.error("a protocol must have at least one step", "steps")

    analyzer = ""
    analysis = raw.get("analysis")
    if analysis is not None:
        if not isinstance(analysis, dict):
            report.error(f"must be a mapping, found {_name(analysis)}", "analysis")
        else:
            _reject_unknown(analysis, {"analyzer"}, "analysis", report)
            analyzer = analysis.get("analyzer") or ""
            if analyzer and not isinstance(analyzer, str):
                report.error("must be the name of a registered analyzer", "analysis.analyzer")
                analyzer = ""

    if report.errors:
        return report

    report.protocol = Protocol(
        id=protocol_id,
        name=str(name),
        version=PROTOCOL_VERSION,
        description=str(raw.get("description") or ""),
        capabilities=capabilities,
        parameters=parameters,
        safety=safety,
        steps=steps,
        analyzer=analyzer,
        source_path=str(source.path),
        content_hash=source.content_hash,
    )
    return report


# ---- sections ---------------------------------------------------------------


def _capabilities(raw: Any, report: ValidationReport) -> Capabilities:
    if raw is None:
        report.warn("no capabilities declared, so no channel may be driven", "capabilities")
        return Capabilities()
    if not isinstance(raw, dict):
        report.error(f"must be a mapping, found {_name(raw)}", "capabilities")
        return Capabilities()
    _reject_unknown(raw, {"channels", "camera", "recording"}, "capabilities", report)

    channels: list[str] = []
    declared = raw.get("channels", ())
    if declared in (None, ()):
        report.warn("no channels declared, so no channel may be driven", "capabilities.channels")
    elif not isinstance(declared, list):
        report.error(f"must be a list, found {_name(declared)}", "capabilities.channels")
    else:
        for index, value in enumerate(declared):
            where = f"capabilities.channels[{index}]"
            if not isinstance(value, str) or not IDENTIFIER.match(value):
                report.error(
                    "a channel is a semantic name such as 'oil', in lower case", where
                )
                continue
            if value in channels:
                report.error(f"channel {value!r} is declared twice", where)
                continue
            channels.append(value)

    return Capabilities(
        channels=tuple(channels),
        camera=_flag(raw.get("camera"), "capabilities.camera", report),
        recording=_flag(raw.get("recording"), "capabilities.recording", report),
    )


def _parameters(raw: Any, report: ValidationReport) -> tuple[ParameterSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        report.error(f"must be a mapping of name to definition, found {_name(raw)}", "parameters")
        return ()

    specs: list[ParameterSpec] = []
    for name, body in raw.items():
        where = f"parameters.{name}"
        if not isinstance(name, str) or not IDENTIFIER.match(name):
            report.error("a parameter name is lower case, starting with a letter", where)
            continue
        if not isinstance(body, dict):
            report.error(f"must be a mapping, found {_name(body)}", where)
            continue
        _reject_unknown(
            body,
            {
                "type",
                "required",
                "default",
                "unit",
                "minimum",
                "maximum",
                "choices",
                "description",
            },
            where,
            report,
        )
        kind = _parameter_type(body.get("type"), where, report)
        if kind is None:
            continue

        minimum = _number_or_none(body.get("minimum"), f"{where}.minimum", report)
        maximum = _number_or_none(body.get("maximum"), f"{where}.maximum", report)
        if minimum is not None and maximum is not None and minimum > maximum:
            report.error(f"minimum {minimum} is above maximum {maximum}", where)

        choices = body.get("choices")
        if choices is not None and not isinstance(choices, list):
            report.error(f"must be a list, found {_name(choices)}", f"{where}.choices")
            choices = None

        default = body.get("default")
        required = _flag(body.get("required"), f"{where}.required", report)
        if default is not None:
            problem = check_value(default, kind, minimum, maximum, tuple(choices or ()))
            if problem:
                report.error(f"default {problem}", where)
        elif not required:
            report.warn(
                "has no default and is not required, so it is empty unless supplied", where
            )

        specs.append(
            ParameterSpec(
                name=name,
                type=kind,
                required=required,
                default=default,
                unit=str(body.get("unit") or ""),
                minimum=minimum,
                maximum=maximum,
                choices=tuple(choices or ()),
                description=str(body.get("description") or ""),
            )
        )
    return tuple(specs)


def _safety(
    raw: Any,
    capabilities: Capabilities,
    known: set[str],
    report: ValidationReport,
) -> SafetyLimits:
    if raw is None:
        report.warn("no safety limits declared; the hardware's own limits still apply", "safety")
        return SafetyLimits()
    if not isinstance(raw, dict):
        report.error(f"must be a mapping, found {_name(raw)}", "safety")
        return SafetyLimits()
    _reject_unknown(
        raw, {"max_pressure_mbar", "max_flow_ul_min", "on_error", "finally"}, "safety", report
    )
    return SafetyLimits(
        max_pressure_mbar=_number_or_none(
            raw.get("max_pressure_mbar"), "safety.max_pressure_mbar", report
        ),
        max_flow_ul_min=_number_or_none(
            raw.get("max_flow_ul_min"), "safety.max_flow_ul_min", report
        ),
        on_error=_steps(
            raw.get("on_error"), "safety.on_error", capabilities, known, report, depth=0
        ),
        finally_=_steps(
            raw.get("finally"), "safety.finally", capabilities, known, report, depth=0
        ),
    )


# ---- steps ------------------------------------------------------------------

MAX_DEPTH = 4


def _steps(
    raw: Any,
    where: str,
    capabilities: Capabilities,
    known: set[str],
    report: ValidationReport,
    *,
    depth: int,
) -> tuple[Step, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        report.error(f"must be a list of steps, found {_name(raw)}", where)
        return ()
    if depth > MAX_DEPTH:
        report.error(f"steps are nested more than {MAX_DEPTH} deep", where)
        return ()

    steps: list[Step] = []
    for index, entry in enumerate(raw):
        source_id = f"{where}[{index}]"
        step = _step(entry, source_id, capabilities, known, report, depth=depth)
        if step is not None:
            steps.append(step)
    return tuple(steps)


def _step(
    raw: Any,
    source_id: str,
    capabilities: Capabilities,
    known: set[str],
    report: ValidationReport,
    *,
    depth: int,
) -> Step | None:
    if not isinstance(raw, dict):
        report.error(f"a step must be a mapping, found {_name(raw)}", source_id)
        return None
    if len(raw) != 1:
        report.error(
            f"a step names exactly one action, found {len(raw)}: {', '.join(map(str, raw))}",
            source_id,
        )
        return None

    action, body = next(iter(raw.items()))
    builder = _ACTIONS.get(action)
    if builder is None:
        report.error(
            f"unknown action {action!r}; this build understands: {', '.join(sorted(_ACTIONS))}",
            source_id,
        )
        return None
    if body is None:
        body = {}
    if not isinstance(body, dict):
        # `- stop_channel: oil` is the readable shorthand for a one-argument step.
        body = {_SHORTHAND[action]: body} if action in _SHORTHAND else None
        if body is None:
            report.error(f"{action} needs a mapping of settings", source_id)
            return None

    context = _Context(source_id, capabilities, known, report, depth)
    return builder(body, context)


@dataclass
class _Context:
    source_id: str
    capabilities: Capabilities
    known: set[str]
    report: ValidationReport
    depth: int

    def unknown(self, body: dict[str, Any], allowed: set[str]) -> None:
        _reject_unknown(body, allowed | {"label"}, self.source_id, self.report)

    def label(self, body: dict[str, Any]) -> str:
        return str(body.get("label") or "")

    def channel(self, value: Any, where: str) -> str:
        name = value if isinstance(value, str) else ""
        if not name:
            self.report.error("a channel name is required", f"{self.source_id}.{where}")
            return ""
        if name not in self.capabilities.channels:
            self.report.error(
                f"channel {name!r} is not in capabilities.channels "
                f"({', '.join(self.capabilities.channels) or 'none declared'})",
                f"{self.source_id}.{where}",
            )
        return name

    def channels(self, value: Any, where: str) -> tuple[str, ...]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not value:
            self.report.error("at least one channel is required", f"{self.source_id}.{where}")
            return ()
        return tuple(self.channel(entry, where) for entry in value)

    def number(self, value: Any, where: str, *, minimum: float | None = None) -> Any:
        """A number, or a substitution that must produce one."""
        if isinstance(value, str):
            return self.substitution(value, where, ParameterType.NUMBER)
        if isinstance(value, bool) or not isinstance(value, int | float):
            self.report.error(
                f"must be a number, found {_name(value)}", f"{self.source_id}.{where}"
            )
            return 0.0
        if not math.isfinite(value):
            self.report.error("must be a finite number", f"{self.source_id}.{where}")
            return 0.0
        if minimum is not None and value < minimum:
            self.report.error(f"must be at least {minimum}", f"{self.source_id}.{where}")
        return float(value)

    def substitution(self, value: str, where: str, expects: ParameterType) -> Any:
        """Check a `${name}` reference without evaluating anything."""
        names = SUBSTITUTION.findall(value)
        if not names:
            self.report.error(
                f"must be a number or a ${{parameter}} reference, found {value!r}",
                f"{self.source_id}.{where}",
            )
            return 0.0
        for name in names:
            if not IDENTIFIER.match(name.strip()):
                self.report.error(
                    f"${{{name}}} is not a plain parameter name; arithmetic and "
                    "attribute access are not supported",
                    f"{self.source_id}.{where}",
                )
            elif name.strip() not in self.known:
                self.report.error(
                    f"${{{name}}} is not a parameter or loop variable of this protocol",
                    f"{self.source_id}.{where}",
                )
        del expects
        return value

    def values(self, value: Any, where: str) -> Any:
        """A list of values, or a substitution naming one."""
        if isinstance(value, str):
            return self.substitution(value, where, ParameterType.NUMBER_LIST)
        if not isinstance(value, list) or not value:
            self.report.error(
                "must be a non-empty list, or a ${parameter} naming one",
                f"{self.source_id}.{where}",
            )
            return ()
        for index, entry in enumerate(value):
            if isinstance(entry, float) and not math.isfinite(entry):
                self.report.error("must be finite", f"{self.source_id}.{where}[{index}]")
        return tuple(value)


def _set_flow(body: dict[str, Any], ctx: _Context) -> Step:
    ctx.unknown(body, {"channel", "value"})
    return SetFlow(
        source_id=ctx.source_id,
        label=ctx.label(body),
        channel=ctx.channel(body.get("channel"), "channel"),
        value=ctx.number(body.get("value"), "value", minimum=0.0),
    )


def _set_pressure(body: dict[str, Any], ctx: _Context) -> Step:
    ctx.unknown(body, {"channel", "value"})
    return SetPressure(
        source_id=ctx.source_id,
        label=ctx.label(body),
        channel=ctx.channel(body.get("channel"), "channel"),
        value=ctx.number(body.get("value"), "value"),
    )


def _stop_channel(body: dict[str, Any], ctx: _Context) -> Step:
    ctx.unknown(body, {"channel"})
    return StopChannel(
        source_id=ctx.source_id,
        label=ctx.label(body),
        channel=ctx.channel(body.get("channel"), "channel"),
    )


def _wait_time(body: dict[str, Any], ctx: _Context) -> Step:
    ctx.unknown(body, {"seconds"})
    return WaitTime(
        source_id=ctx.source_id,
        label=ctx.label(body),
        seconds=ctx.number(body.get("seconds"), "seconds", minimum=0.0),
    )


def _wait_stable(body: dict[str, Any], ctx: _Context) -> Step:
    ctx.unknown(body, {"channels", "minimum_duration", "tolerance_ul_min", "timeout"})
    return WaitStable(
        source_id=ctx.source_id,
        label=ctx.label(body),
        channels=ctx.channels(body.get("channels"), "channels"),
        minimum_duration=ctx.number(
            body.get("minimum_duration", 5.0), "minimum_duration", minimum=0.0
        ),
        tolerance_ul_min=ctx.number(
            body.get("tolerance_ul_min", 2.0), "tolerance_ul_min", minimum=0.0
        ),
        timeout=ctx.number(body.get("timeout", 60.0), "timeout", minimum=0.0),
    )


def _wait_condition(body: dict[str, Any], ctx: _Context) -> Step:
    ctx.unknown(body, {"channel", "field", "comparison", "value", "timeout"})
    return WaitCondition(
        source_id=ctx.source_id,
        label=ctx.label(body),
        channel=ctx.channel(body.get("channel"), "channel"),
        field=_enum(body.get("field", "flow"), Field, f"{ctx.source_id}.field", ctx.report),
        comparison=_enum(
            body.get("comparison", "at_least"),
            Comparison,
            f"{ctx.source_id}.comparison",
            ctx.report,
        ),
        value=ctx.number(body.get("value"), "value"),
        timeout=ctx.number(body.get("timeout", 60.0), "timeout", minimum=0.0),
    )


def _wait_volume(body: dict[str, Any], ctx: _Context) -> Step:
    ctx.unknown(body, {"channel", "volume_ul", "timeout"})
    return WaitVolume(
        source_id=ctx.source_id,
        label=ctx.label(body),
        channel=ctx.channel(body.get("channel"), "channel"),
        volume_ul=ctx.number(body.get("volume_ul"), "volume_ul", minimum=0.0),
        timeout=ctx.number(body.get("timeout", 0.0), "timeout", minimum=0.0),
    )


def _confirm(body: dict[str, Any], ctx: _Context) -> Step:
    ctx.unknown(body, {"message"})
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        ctx.report.error("a message to show the operator is required", f"{ctx.source_id}.message")
        message = ""
    return Confirm(source_id=ctx.source_id, label=ctx.label(body), message=str(message))


def _operator_input(body: dict[str, Any], ctx: _Context) -> Step:
    ctx.unknown(body, {"id", "type", "prompt", "unit", "minimum", "maximum", "required"})
    identifier = _identifier(body.get("id"), f"{ctx.source_id}.id", ctx.report)
    kind = _parameter_type(body.get("type", "number"), f"{ctx.source_id}.type", ctx.report)
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        ctx.report.error("a prompt is required", f"{ctx.source_id}.prompt")
        prompt = ""
    return OperatorInput(
        source_id=ctx.source_id,
        label=ctx.label(body),
        id=identifier,
        type=kind or ParameterType.NUMBER,
        prompt=str(prompt),
        unit=str(body.get("unit") or ""),
        minimum=_number_or_none(body.get("minimum"), f"{ctx.source_id}.minimum", ctx.report),
        maximum=_number_or_none(body.get("maximum"), f"{ctx.source_id}.maximum", ctx.report),
        required=_flag(body.get("required", True), f"{ctx.source_id}.required", ctx.report),
    )


def _capture_window(body: dict[str, Any], ctx: _Context) -> Step:
    ctx.unknown(body, {"id", "channels", "duration", "fields", "summaries"})
    return CaptureWindow(
        source_id=ctx.source_id,
        label=ctx.label(body),
        id=_identifier(body.get("id"), f"{ctx.source_id}.id", ctx.report),
        channels=ctx.channels(body.get("channels"), "channels"),
        duration=ctx.number(body.get("duration"), "duration", minimum=0.0),
        fields=_enum_list(
            body.get("fields", ["flow", "pressure"]), Field, f"{ctx.source_id}.fields", ctx.report
        ),
        summaries=_enum_list(
            body.get("summaries", ["mean", "stddev", "count"]),
            Summary,
            f"{ctx.source_id}.summaries",
            ctx.report,
        ),
    )


def _start_recording(body: dict[str, Any], ctx: _Context) -> Step:
    ctx.unknown(body, {"recording_label"})
    if not ctx.capabilities.recording:
        ctx.report.error(
            "records, so capabilities.recording must be true", ctx.source_id
        )
    label = body.get("recording_label", "")
    if isinstance(label, str) and SUBSTITUTION.search(label):
        label = ctx.substitution(label, "recording_label", ParameterType.STRING)
    return StartRecording(
        source_id=ctx.source_id, label=ctx.label(body), recording_label=label or ""
    )


def _stop_recording(body: dict[str, Any], ctx: _Context) -> Step:
    ctx.unknown(body, set())
    return StopRecording(source_id=ctx.source_id, label=ctx.label(body))


def _repeat(body: dict[str, Any], ctx: _Context) -> Step:
    ctx.unknown(body, {"count", "steps"})
    count = body.get("count")
    if isinstance(count, str):
        count = ctx.substitution(count, "count", ParameterType.INTEGER)
    elif not isinstance(count, int) or isinstance(count, bool) or count < 1:
        ctx.report.error("must be a whole number of at least 1", f"{ctx.source_id}.count")
        count = 1
    return Repeat(
        source_id=ctx.source_id,
        label=ctx.label(body),
        count=count,
        steps=_steps(
            body.get("steps"),
            f"{ctx.source_id}.steps",
            ctx.capabilities,
            ctx.known,
            ctx.report,
            depth=ctx.depth + 1,
        ),
    )


def _foreach(body: dict[str, Any], ctx: _Context) -> Step:
    ctx.unknown(body, {"variable", "values", "steps"})
    variable = _identifier(body.get("variable"), f"{ctx.source_id}.variable", ctx.report)
    inner = ctx.known | ({variable} if variable else set())
    return Foreach(
        source_id=ctx.source_id,
        label=ctx.label(body),
        variable=variable,
        values=ctx.values(body.get("values"), "values"),
        steps=_steps(
            body.get("steps"),
            f"{ctx.source_id}.steps",
            ctx.capabilities,
            inner,
            ctx.report,
            depth=ctx.depth + 1,
        ),
    )


def _matrix(body: dict[str, Any], ctx: _Context) -> Step:
    ctx.unknown(body, {"variables", "steps"})
    raw_variables = body.get("variables")
    variables: list[tuple[str, Any]] = []
    inner = set(ctx.known)
    if not isinstance(raw_variables, dict) or not raw_variables:
        ctx.report.error(
            "must be a mapping of variable name to values", f"{ctx.source_id}.variables"
        )
    else:
        for name, values in raw_variables.items():
            variable = _identifier(name, f"{ctx.source_id}.variables.{name}", ctx.report)
            if variable:
                inner.add(variable)
            variables.append((variable, ctx.values(values, f"variables.{name}")))
    return Matrix(
        source_id=ctx.source_id,
        label=ctx.label(body),
        variables=tuple(variables),
        steps=_steps(
            body.get("steps"),
            f"{ctx.source_id}.steps",
            ctx.capabilities,
            inner,
            ctx.report,
            depth=ctx.depth + 1,
        ),
    )


_ACTIONS = {
    "set_flow": _set_flow,
    "set_pressure": _set_pressure,
    "stop_channel": _stop_channel,
    "wait_time": _wait_time,
    "wait_stable": _wait_stable,
    "wait_condition": _wait_condition,
    "wait_volume": _wait_volume,
    "confirm": _confirm,
    "operator_input": _operator_input,
    "capture_window": _capture_window,
    "start_recording": _start_recording,
    "stop_recording": _stop_recording,
    "repeat": _repeat,
    "foreach": _foreach,
    "matrix": _matrix,
}

# Actions readable enough to write as `- stop_channel: oil`.
_SHORTHAND = {
    "stop_channel": "channel",
    "wait_time": "seconds",
    "confirm": "message",
    "start_recording": "recording_label",
}


# ---- small helpers ----------------------------------------------------------


def _reject_unknown(
    body: dict[str, Any], allowed: set[str], where: str, report: ValidationReport
) -> None:
    for key in body:
        if key not in allowed:
            prefix = f"{where}." if where else ""
            report.error(
                f"unknown key {key!r}; allowed here: {', '.join(sorted(allowed))}",
                f"{prefix}{key}",
            )


def _identifier(value: Any, where: str, report: ValidationReport) -> str:
    if not isinstance(value, str) or not IDENTIFIER.match(value):
        report.error(
            "must be lower case, starting with a letter, using only letters, digits "
            "and underscores",
            where,
        )
        return ""
    return value


def _parameter_type(value: Any, where: str, report: ValidationReport) -> ParameterType | None:
    try:
        return ParameterType(value)
    except ValueError:
        report.error(
            f"unknown type {value!r}; supported: "
            f"{', '.join(kind.value for kind in ParameterType)}",
            f"{where}.type" if not where.endswith(".type") else where,
        )
        return None


def _enum(value: Any, enum: type, where: str, report: ValidationReport) -> Any:
    try:
        return enum(value)
    except ValueError:
        options = ", ".join(member.value for member in enum)
        report.error(f"unknown value {value!r}; supported: {options}", where)
        return next(iter(enum))


def _enum_list(value: Any, enum: type, where: str, report: ValidationReport) -> tuple[Any, ...]:
    if not isinstance(value, list) or not value:
        report.error(f"must be a non-empty list, found {_name(value)}", where)
        return ()
    return tuple(_enum(entry, enum, f"{where}[{index}]", report) for index, entry in enumerate(value))


def _flag(value: Any, where: str, report: ValidationReport) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        report.error(f"must be true or false, found {_name(value)}", where)
        return False
    return value


def _number_or_none(value: Any, where: str, report: ValidationReport) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        report.error(f"must be a number, found {_name(value)}", where)
        return None
    if not math.isfinite(value):
        report.error("must be a finite number", where)
        return None
    return float(value)


def check_value(
    value: Any,
    kind: ParameterType,
    minimum: float | None,
    maximum: float | None,
    choices: tuple[Any, ...],
) -> str:
    """Why this value is not acceptable for this parameter, or an empty string."""
    if kind in (ParameterType.NUMBER_LIST, ParameterType.STRING_LIST):
        if not isinstance(value, list):
            return f"must be a list, found {_name(value)}"
        member = ParameterType.NUMBER if kind is ParameterType.NUMBER_LIST else ParameterType.STRING
        for index, entry in enumerate(value):
            problem = check_value(entry, member, minimum, maximum, ())
            if problem:
                return f"item {index} {problem}"
        return ""
    if kind is ParameterType.STRING:
        if not isinstance(value, str):
            return f"must be a string, found {_name(value)}"
    elif kind is ParameterType.BOOLEAN:
        if not isinstance(value, bool):
            return f"must be true or false, found {_name(value)}"
    elif kind is ParameterType.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            return f"must be a whole number, found {_name(value)}"
    else:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return f"must be a number, found {_name(value)}"
        if not math.isfinite(value):
            return "must be finite"
    if choices and value not in choices:
        return f"must be one of {', '.join(map(str, choices))}"
    if isinstance(value, int | float) and not isinstance(value, bool):
        if minimum is not None and value < minimum:
            return f"must be at least {minimum}"
        if maximum is not None and value > maximum:
            return f"must be at most {maximum}"
    return ""


def _name(value: Any) -> str:
    if value is None:
        return "nothing"
    return type(value).__name__
