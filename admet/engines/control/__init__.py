"""Acquisition control engines."""

__all__ = ["FluidicsControlEngine", "RecordingMetadata", "RecordingSession", "create_engine"]


def __getattr__(name: str):
    if name in {"FluidicsControlEngine", "create_engine"}:
        from .engine import FluidicsControlEngine, create_engine

        return {"FluidicsControlEngine": FluidicsControlEngine, "create_engine": create_engine}[name]
    if name in {"RecordingMetadata", "RecordingSession"}:
        from .session import RecordingMetadata, RecordingSession

        return {"RecordingMetadata": RecordingMetadata, "RecordingSession": RecordingSession}[name]
    raise AttributeError(name)
