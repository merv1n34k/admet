"""Domain logic above the engines and below any interface.

What lives here is everything a run needs that is not device driving: the
planning arithmetic, the protocol documents, the batch analysis runner and the
record of past checks. None of it knows about an interface, and none of it is
required in order to drive hardware -- an engine is usable on its own.
"""

from __future__ import annotations

__all__ = [
    "AnalyzeBatchReport",
    "AnalyzeBatchRunner",
    "AnalyzeJobReport",
    "AnalyzeProjectReport",
    "AnalyzeTarget",
    "infer_engine",
]


def __getattr__(name: str):
    # Imported on use: the analysis runner pulls in heavy scientific stacks that
    # a control-only installation has no reason to load.
    if name in set(__all__):
        from . import analyze_runner

        return getattr(analyze_runner, name)
    raise AttributeError(name)
