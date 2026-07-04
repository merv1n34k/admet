from __future__ import annotations

from admet.core.engine import EngineRegistry


def create_engine_registry(scope: str | None = None) -> EngineRegistry:
    if scope not in {None, "analyze", "control"}:
        raise ValueError(f"unknown engine registry scope: {scope!r}")

    registry = EngineRegistry()
    if scope in {None, "analyze"}:
        registry.register("opencv", _create_opencv_engine)
        registry.register("cellpose", _create_cellpose_engine)
    if scope in {None, "control"}:
        registry.register("acquisition", _create_acquisition_engine)
    return registry


def _create_opencv_engine():
    from admet.engines.opencv import create_engine

    return create_engine()


def _create_cellpose_engine():
    from admet.engines.cellpose import create_engine

    return create_engine()


def _create_acquisition_engine():
    from admet.engines.acquisition import create_engine

    return create_engine()
