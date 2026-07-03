"""Engine contracts and lazy registry."""

from .actions import ActionSpec, action_spec, validate_action_settings
from .base import Engine, RunJob, RunResult, RunSink
from .registry import EngineRegistry, LazyEngineSpec

__all__ = [
    "ActionSpec",
    "Engine",
    "EngineRegistry",
    "LazyEngineSpec",
    "RunJob",
    "RunResult",
    "RunSink",
    "action_spec",
    "validate_action_settings",
]
