from .model import (
    EditorSpec,
    LogSpec,
    ResultsSpec,
    Stage,
    StageAction,
    StageControl,
    StageInstruction,
    SettingsSpec,
    StageStatus,
    StageSurface,
    Workflow,
    WorkflowState,
)
from .runner import WorkflowRunner

__all__ = [
    "AnalyzeBatchReport",
    "AnalyzeBatchRunner",
    "AnalyzeJobReport",
    "AnalyzeProjectReport",
    "AnalyzeTarget",
    "EditorSpec",
    "LogSpec",
    "ResultsSpec",
    "SettingsSpec",
    "Stage",
    "StageAction",
    "StageControl",
    "StageInstruction",
    "StageStatus",
    "StageSurface",
    "Workflow",
    "WorkflowRunner",
    "WorkflowState",
    "create_analyze_workflow",
    "create_control_workflow",
    "infer_engine",
]


def __getattr__(name: str):
    if name in {
        "AnalyzeBatchReport",
        "AnalyzeBatchRunner",
        "AnalyzeJobReport",
        "AnalyzeProjectReport",
        "AnalyzeTarget",
        "create_analyze_workflow",
        "infer_engine",
    }:
        from . import analyze

        return getattr(analyze, name)
    if name == "create_control_workflow":
        from .control import create_control_workflow

        return create_control_workflow
    raise AttributeError(name)
