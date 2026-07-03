"""Acquisition control engines."""

__all__ = ["FluidicsControlEngine", "RecordingMetadata", "RecordingRun", "create_engine"]


def __getattr__(name: str):
    if name in {"FluidicsControlEngine", "create_engine"}:
        from .fluidics import FluidicsControlEngine, create_engine

        return {"FluidicsControlEngine": FluidicsControlEngine, "create_engine": create_engine}[name]
    if name in {"RecordingMetadata", "RecordingRun"}:
        from .recording import RecordingMetadata, RecordingRun

        return {"RecordingMetadata": RecordingMetadata, "RecordingRun": RecordingRun}[name]
    raise AttributeError(name)
