from __future__ import annotations

from admet.core.engine import EngineRegistry, LazyEngineSpec


def create_analyze_registry() -> EngineRegistry:
    registry = EngineRegistry()
    registry.register_lazy(LazyEngineSpec("opencv", "admet.engines.analyze.opencv"))
    registry.register_lazy(LazyEngineSpec("cellpose", "admet.engines.analyze.cellpose"))
    return registry
