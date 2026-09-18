"""Binding parameters and expanding a protocol into the steps that will run.

Compilation is where a document stops being general. Loops become their
iterations, substitutions become values, and every resulting step carries an
identifier that is stable across runs of the same document with the same
parameters -- so two runs can be compared step by step, and a stored run can be
read back against the protocol it came from.

Nothing is evaluated. A substitution is a lookup of a name that validation has
already proven exists; there is no arithmetic and no way to reach anything that
was not bound.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from itertools import product
from typing import Any

from admet.workflows.protocols.model import (
    CaptureWindow,
    Confirm,
    Foreach,
    Matrix,
    OperatorInput,
    ParameterSpec,
    ParameterType,
    Problem,
    Protocol,
    Repeat,
    SetFlow,
    SetPressure,
    StartRecording,
    Step,
    StopChannel,
    StopRecording,
    WaitCondition,
    WaitStable,
    WaitTime,
    WaitVolume,
)
from admet.workflows.protocols.validator import SUBSTITUTION, check_value

MAX_EXPANDED_STEPS = 10_000


@dataclass(frozen=True)
class CompiledStep:
    """One step as it will actually run."""

    step_id: str
    source_step_id: str
    step: Step
    bindings: tuple[tuple[str, Any], ...] = ()

    @property
    def kind(self) -> str:
        return type(self.step).__name__


@dataclass
class CompileResult:
    """The expanded run, or why it could not be produced."""

    steps: tuple[CompiledStep, ...] = ()
    parameters: dict[str, Any] = field(default_factory=dict)
    errors: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, message: str, where: str = "") -> None:
        self.errors.append(Problem(message, where))


def bind_parameters(
    protocol: Protocol, supplied: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[Problem]]:
    """Fill in defaults and check what the operator supplied.

    A value that is absent, of the wrong type or out of bounds is a problem
    here, where it can be shown next to the field, rather than mid-run.
    """
    supplied = dict(supplied or {})
    values: dict[str, Any] = {}
    problems: list[Problem] = []

    for name in supplied:
        if protocol.parameter(name) is None:
            problems.append(
                Problem(f"{name!r} is not a parameter of this protocol", "parameters")
            )

    for spec in protocol.parameters:
        if spec.name in supplied and supplied[spec.name] is not None:
            value = supplied[spec.name]
        elif spec.default is not None:
            value = spec.default
        elif spec.required:
            problems.append(Problem("is required", f"parameters.{spec.name}"))
            continue
        else:
            values[spec.name] = None
            continue

        value = _coerce(value, spec)
        problem = check_value(value, spec.type, spec.minimum, spec.maximum, spec.choices)
        if problem:
            problems.append(Problem(problem, f"parameters.{spec.name}"))
            continue
        values[spec.name] = value

    return values, problems


def compile_protocol(
    protocol: Protocol, supplied: dict[str, Any] | None = None
) -> CompileResult:
    """Expand a protocol into the ordered steps of one run."""
    values, problems = bind_parameters(protocol, supplied)
    result = CompileResult(parameters=values)
    result.errors.extend(problems)
    if problems:
        return result

    counters: dict[str, int] = {}
    steps: list[CompiledStep] = []
    try:
        _expand(protocol.steps, values, (), counters, steps)
    except (_TooManySteps, _NotANumber) as exc:
        result.error(str(exc), "steps")
        return result

    result.steps = tuple(steps)
    if not steps:
        result.error("expands to no steps at all", "steps")
    return result


def resolve_actions(
    steps: tuple[Step, ...], values: dict[str, Any]
) -> tuple[CompiledStep, ...]:
    """Expand a safety block, which may not contain loops."""
    counters: dict[str, int] = {}
    out: list[CompiledStep] = []
    _expand(steps, values, (), counters, out)
    return tuple(out)


class _TooManySteps(Exception):
    """The expansion is too large to be what anyone intended."""


class _NotANumber(Exception):
    """A substitution resolved to something that cannot be a setpoint.

    Validation proves references exist and are numbers, so reaching this means
    the two disagree -- which must stop the run rather than send a guess to the
    hardware.
    """


def _expand(
    steps: tuple[Step, ...],
    values: dict[str, Any],
    bindings: tuple[tuple[str, Any], ...],
    counters: dict[str, int],
    out: list[CompiledStep],
) -> None:
    for step in steps:
        if isinstance(step, Repeat):
            count = _value_of(step.count, values, bindings)
            for index in range(int(count)):
                inner = (*bindings, (f"{step.source_id}#repeat", index + 1))
                _expand(step.steps, values, inner, counters, out)
        elif isinstance(step, Foreach):
            for item in _sequence_of(step.values, values, bindings):
                inner = (*bindings, (step.variable, item))
                _expand(step.steps, values, inner, counters, out)
        elif isinstance(step, Matrix):
            names = [name for name, _ in step.variables]
            columns = [
                list(_sequence_of(column, values, bindings)) for _, column in step.variables
            ]
            # Declared order is visiting order: the last variable moves fastest,
            # so a sweep reads the way it was written.
            for combination in product(*columns):
                inner = (*bindings, *zip(names, combination, strict=True))
                _expand(step.steps, values, inner, counters, out)
        else:
            out.append(_emit(step, values, bindings, counters))
        if len(out) > MAX_EXPANDED_STEPS:
            raise _TooManySteps(
                f"expands to more than {MAX_EXPANDED_STEPS} steps, which is not a run "
                "anyone meant to start"
            )


def _emit(
    step: Step,
    values: dict[str, Any],
    bindings: tuple[tuple[str, Any], ...],
    counters: dict[str, int],
) -> CompiledStep:
    occurrence = counters.get(step.source_id, 0) + 1
    counters[step.source_id] = occurrence
    return CompiledStep(
        step_id=f"{step.source_id}#{occurrence:04d}",
        source_step_id=step.source_id,
        step=_resolve(step, values, bindings),
        bindings=bindings,
    )


def _resolve(step: Step, values: dict[str, Any], bindings: tuple[tuple[str, Any], ...]) -> Step:
    """Replace every substitution in a step with the value it names."""
    if isinstance(step, SetFlow | SetPressure):
        return replace(step, value=_number(step.value, values, bindings))
    if isinstance(step, WaitTime):
        return replace(step, seconds=_number(step.seconds, values, bindings))
    if isinstance(step, WaitStable):
        return replace(
            step,
            minimum_duration=_number(step.minimum_duration, values, bindings),
            tolerance_ul_min=_number(step.tolerance_ul_min, values, bindings),
            timeout=_number(step.timeout, values, bindings),
        )
    if isinstance(step, WaitCondition):
        return replace(
            step,
            value=_number(step.value, values, bindings),
            timeout=_number(step.timeout, values, bindings),
        )
    if isinstance(step, WaitVolume):
        return replace(
            step,
            volume_ul=_number(step.volume_ul, values, bindings),
            timeout=_number(step.timeout, values, bindings),
        )
    if isinstance(step, CaptureWindow):
        return replace(step, duration=_number(step.duration, values, bindings))
    if isinstance(step, StartRecording):
        return replace(
            step, recording_label=_text(step.recording_label, values, bindings)
        )
    if isinstance(step, Confirm):
        return replace(step, message=_text(step.message, values, bindings))
    if isinstance(step, OperatorInput | StopChannel | StopRecording):
        return step
    return step


def _value_of(raw: Any, values: dict[str, Any], bindings: tuple[tuple[str, Any], ...]) -> Any:
    if not isinstance(raw, str):
        return raw
    scope = {**values, **dict(bindings)}
    whole = SUBSTITUTION.fullmatch(raw.strip())
    if whole:
        return scope.get(whole.group(1).strip())
    return SUBSTITUTION.sub(lambda m: str(scope.get(m.group(1).strip(), "")), raw)


def _number(raw: Any, values: dict[str, Any], bindings: tuple[tuple[str, Any], ...]) -> float:
    value = _value_of(raw, values, bindings)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _NotANumber(f"{raw!r} resolved to {type(value).__name__}, not a number")
    if not math.isfinite(value):
        raise _NotANumber(f"{raw!r} resolved to {value}, which is not finite")
    return float(value)


def _text(raw: Any, values: dict[str, Any], bindings: tuple[tuple[str, Any], ...]) -> str:
    value = _value_of(raw, values, bindings)
    return "" if value is None else str(value)


def _sequence_of(
    raw: Any, values: dict[str, Any], bindings: tuple[tuple[str, Any], ...]
) -> tuple[Any, ...]:
    value = _value_of(raw, values, bindings)
    if isinstance(value, list | tuple):
        return tuple(value)
    return () if value is None else (value,)


def _coerce(value: Any, spec: ParameterSpec) -> Any:
    """Accept a whole number where a number is wanted, and a list where a list is.

    Nothing else is converted: a string is never read as a number, because a
    parameter typed by hand into a form should fail visibly rather than become
    something the operator did not write.
    """
    if spec.type is ParameterType.NUMBER and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if spec.type in (ParameterType.NUMBER_LIST, ParameterType.STRING_LIST):
        if isinstance(value, tuple):
            value = list(value)
        if spec.type is ParameterType.NUMBER_LIST and isinstance(value, list):
            return [
                float(item) if isinstance(item, int) and not isinstance(item, bool) else item
                for item in value
            ]
    return value
