from __future__ import annotations

from admet.core.engine import EngineRegistry, LazyEngineSpec


def create_engine_registry(scope: str | None = None) -> EngineRegistry:
    if scope not in {None, "analyze", "control"}:
        raise ValueError(f"unknown engine registry scope: {scope!r}")

    registry = EngineRegistry()
    if scope in {None, "analyze"}:
        registry.register_lazy(LazyEngineSpec("opencv", "admet.engines.analyze.opencv"))
        registry.register_lazy(LazyEngineSpec("cellpose", "admet.engines.analyze.cellpose"))
    if scope in {None, "control"}:
        registry.register_lazy(LazyEngineSpec("fluidics", "admet.engines.control.fluidics"))
    return registry
