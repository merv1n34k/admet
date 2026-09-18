"""Typed protocol model.

A protocol is data: what to do, in what order, under what limits. Nothing here
knows about hardware, widgets or files -- it is the shape that a YAML document
is parsed into and that a compiler expands into a run.

Steps are a closed set. A document naming anything outside it is rejected rather
than ignored, because a step that is silently dropped is a step the operator
believes happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

PROTOCOL_VERSION = 1


class ProtocolError(Exception):
    """A protocol could not be read, understood or trusted."""


@dataclass(frozen=True)
class Problem:
    """One reason a protocol was rejected, or one thing worth saying about it."""

    message: str
    where: str = ""

    def __str__(self) -> str:
        return f"{self.where}: {self.message}" if self.where else self.message


class ParameterType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    NUMBER_LIST = "list[number]"
    STRING_LIST = "list[string]"


LIST_TYPES = frozenset({ParameterType.NUMBER_LIST, ParameterType.STRING_LIST})


@dataclass(frozen=True)
class ParameterSpec:
    """One value the operator supplies before a run."""

    name: str
    type: ParameterType
    required: bool = False
    default: Any = None
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[Any, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class Capabilities:
    """What a protocol needs from the rig before it may run."""

    channels: tuple[str, ...] = ()
    camera: bool = False
    recording: bool = False


@dataclass(frozen=True)
class SafetyLimits:
    """Ceilings a protocol declares for itself.

    These narrow the hardware's own limits; they never widen them. A protocol
    asking for more than the instrument can deliver is an error, not a request.
    """

    max_pressure_mbar: float | None = None
    max_flow_ul_min: float | None = None
    on_error: tuple[Step, ...] = ()
    finally_: tuple[Step, ...] = ()


# ---- steps -----------------------------------------------------------------
#
# Every step carries where it came from, so an expanded run can always be traced
# back to the line the operator wrote.


@dataclass(frozen=True)
class Step:
    source_id: str = ""
    label: str = ""


@dataclass(frozen=True)
class SetFlow(Step):
    channel: str = ""
    value: Any = 0.0


@dataclass(frozen=True)
class SetPressure(Step):
    channel: str = ""
    value: Any = 0.0


@dataclass(frozen=True)
class StopChannel(Step):
    channel: str = ""


@dataclass(frozen=True)
class WaitTime(Step):
    seconds: Any = 0.0


@dataclass(frozen=True)
class WaitStable(Step):
    channels: tuple[str, ...] = ()
    minimum_duration: Any = 5.0
    tolerance_ul_min: Any = 2.0
    timeout: Any = 60.0


class Comparison(StrEnum):
    ABOVE = "above"
    BELOW = "below"
    AT_LEAST = "at_least"
    AT_MOST = "at_most"


class Field(StrEnum):
    FLOW = "flow"
    PRESSURE = "pressure"
    VOLUME = "volume"


@dataclass(frozen=True)
class WaitCondition(Step):
    """A bounded wait on one reading crossing one threshold.

    Deliberately not an expression language: one channel, one field, one
    comparison, one value, and always a timeout.
    """

    channel: str = ""
    field: Field = Field.FLOW
    comparison: Comparison = Comparison.AT_LEAST
    value: Any = 0.0
    timeout: Any = 60.0


@dataclass(frozen=True)
class WaitVolume(Step):
    """Wait until a channel has delivered a volume. Always bounded by a timeout."""

    channel: str = ""
    volume_ul: Any = 0.0
    timeout: Any = 0.0


@dataclass(frozen=True)
class Confirm(Step):
    """Hold until the operator acknowledges. The message is text, never a signal."""

    message: str = ""


@dataclass(frozen=True)
class OperatorInput(Step):
    """Ask the operator for a typed value and keep it with the run."""

    id: str = ""
    type: ParameterType = ParameterType.NUMBER
    prompt: str = ""
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    required: bool = True


class Summary(StrEnum):
    MEAN = "mean"
    STDDEV = "stddev"
    MIN = "min"
    MAX = "max"
    COUNT = "count"


@dataclass(frozen=True)
class CaptureWindow(Step):
    """Record a fixed window of readings and summarise it."""

    id: str = ""
    channels: tuple[str, ...] = ()
    duration: Any = 0.0
    fields: tuple[Field, ...] = (Field.FLOW, Field.PRESSURE)
    summaries: tuple[Summary, ...] = (Summary.MEAN, Summary.STDDEV, Summary.COUNT)


@dataclass(frozen=True)
class StartRecording(Step):
    """Begin a recording. Explicit, never inferred from what a prompt says."""

    recording_label: Any = ""


@dataclass(frozen=True)
class StopRecording(Step):
    pass


@dataclass(frozen=True)
class Repeat(Step):
    count: Any = 1
    steps: tuple[Step, ...] = ()


@dataclass(frozen=True)
class Foreach(Step):
    variable: str = ""
    values: Any = ()
    steps: tuple[Step, ...] = ()


@dataclass(frozen=True)
class Matrix(Step):
    """A deterministic sweep over the product of several variables.

    Order is the order the variables are declared in, so two runs of the same
    document visit the same points in the same sequence.
    """

    variables: tuple[tuple[str, Any], ...] = ()
    steps: tuple[Step, ...] = ()


CONTAINER_STEPS = (Repeat, Foreach, Matrix)


@dataclass(frozen=True)
class Protocol:
    """A whole protocol document, parsed and typed."""

    id: str
    name: str
    version: int = PROTOCOL_VERSION
    description: str = ""
    capabilities: Capabilities = field(default_factory=Capabilities)
    parameters: tuple[ParameterSpec, ...] = ()
    safety: SafetyLimits = field(default_factory=SafetyLimits)
    steps: tuple[Step, ...] = ()
    analyzer: str = ""
    source_path: str = ""
    content_hash: str = ""

    def parameter(self, name: str) -> ParameterSpec | None:
        return next((spec for spec in self.parameters if spec.name == name), None)
