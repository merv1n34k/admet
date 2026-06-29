from __future__ import annotations

from admet.core.engine import EngineRegistry, LazyEngineSpec


def create_control_registry() -> EngineRegistry:
    registry = EngineRegistry()
    registry.register_lazy(LazyEngineSpec("fluidics", "admet.engines.control.engine"))
    return registry
