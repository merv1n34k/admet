from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .triggers import Trigger


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class PipelineStep:
    name: str
    sensor_setpoints: dict[int, float]
    trigger: Trigger
    pressure_setpoints: dict[int, float] = field(default_factory=dict)
    on_complete: str = "hold"
    confirm_message: str = ""
    status: StepStatus = StepStatus.PENDING
    error_msg: str = ""
