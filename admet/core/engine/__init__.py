"""Engine contracts and lazy registry."""

from .actions import ActionSpec, action_spec, validate_action_settings
from .base import Engine, EngineContext, EngineResult
from .registry import EngineRegistry, LazyEngineSpec

__all__ = [
    "ActionSpec",
    "Engine",
    "EngineContext",
    "EngineRegistry",
    "EngineResult",
    "LazyEngineSpec",
    "action_spec",
    "validate_action_settings",
]
