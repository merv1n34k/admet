from __future__ import annotations

from collections.abc import Callable

from admet.workflows import Stage, Workflow, WorkflowState


def current_stage(workflow: Workflow, state: WorkflowState) -> Stage:
    index = max(0, min(state.index, len(workflow.stages) - 1))
    return workflow.stages[index]


def stage_by_id(workflow: Workflow, state: WorkflowState, stage_id: str) -> Stage:
    return next((stage for stage in workflow.stages if stage.id == stage_id), current_stage(workflow, state))


def has_feature(stage: Stage, feature: str) -> bool:
    return feature in stage.features


def guard_enabled(guard: str, value: Callable[[str], bool]) -> bool:
    if not guard:
        return True
    for part in guard.split(" and "):
        part = part.strip()
        if not part:
            continue
        expected = True
        if part.startswith("not "):
            expected = False
            part = part[4:].strip()
        if value(part) is not expected:
            return False
    return True


def active_when(active_when: str, value: Callable[[str], bool]) -> bool:
    return bool(active_when) and value(active_when)


def instruction_text(stage: Stage, guard_value: Callable[[str], bool]) -> str:
    for instruction in stage.instruction_cards:
        if not instruction.guard or guard_enabled(instruction.guard, guard_value):
            return instruction.text
    return stage.description or stage.label
