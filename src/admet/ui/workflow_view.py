from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from admet.workflows import Stage, StageAction, StageStatus, Workflow, WorkflowState


@dataclass(frozen=True)
class ActionButtonState:
    label: str
    enabled: bool = True
    active: bool = False
    warning: bool = False
    toggle: bool = False


@dataclass(frozen=True)
class TocRowState:
    index: int
    stage_id: str
    label: str
    selected: bool
    status: Any


def current_stage(workflow: Workflow, state: WorkflowState) -> Stage:
    index = max(0, min(state.index, len(workflow.stages) - 1))
    return workflow.stages[index]


def stage_by_id(workflow: Workflow, state: WorkflowState, stage_id: str) -> Stage:
    return next((stage for stage in workflow.stages if stage.id == stage_id), current_stage(workflow, state))


def has_feature(stage: Stage, feature: str) -> bool:
    return feature in stage.features


# Every guard name the control window answers to. An unrecognised guard reads as
# true, so a name that never reaches the window would silently enable its button.
KNOWN_GUARDS = frozenset(
    {
        "project_ready",
        "camera_connected",
        "camera_live",
        "fluidics_connected",
        "pipeline_running",
        "pipeline_waiting",
        "pipeline_complete",
        "corrections_applied",
        "check_infeasible",
        "check_recorded",
        "check_due",
        "check_satisfied",
    }
)


# What a protocol reports while it is doing something, and once it is not.
PIPELINE_BUSY = frozenset({"running", "paused", "stopping"})
PIPELINE_FINISHED = frozenset({"completed", "idle", "error"})


def pipeline_is_running(polled_state: str, event_state: str = "") -> bool:
    """Whether a protocol is still going, from two sources that disagree in time.

    The polled state lags: it is read on a timer. The protocol's own events are
    immediate but stop arriving the moment it finishes -- which is exactly when
    the buttons guarded on this need to change. Taking a finished event as final
    closes the window where the poll still says running and nothing is left to
    say otherwise.
    """
    if event_state in PIPELINE_FINISHED:
        return False
    return polled_state in PIPELINE_BUSY


def guard_terms(guard: str) -> tuple[str, ...]:
    """The names a guard expression refers to, without their negation."""
    terms = []
    for part in guard.split(" and "):
        part = part.strip()
        if part.startswith("not "):
            part = part[4:].strip()
        if part:
            terms.append(part)
    return tuple(terms)


def guard_enabled(guard: str, value: Callable[[str], bool]) -> bool:
    """Evaluate a guard: names joined by " and ", each optionally negated by "not ".

    Deliberately not an expression language -- there is no "or" and no bracketing,
    so anything more involved has to be named and computed where the state lives
    rather than smuggled into a string that would quietly evaluate to true.
    """
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


def action_button_state(
    action: StageAction,
    guard_value: Callable[[str], bool],
    *,
    enabled: bool = True,
    active: bool | None = None,
    toggle: bool = False,
    label: str | None = None,
) -> ActionButtonState:
    return ActionButtonState(
        label=label or action.label,
        enabled=enabled and guard_enabled(action.guard, guard_value),
        active=action.variant in {"primary", "success"} if active is None else active,
        warning=action.variant == "warning",
        toggle=toggle,
    )


def stage_controls(stage: Stage) -> tuple[StageAction, ...]:
    if stage.actions:
        return stage.actions
    if stage.pipeline:
        return ()
    controls: list[StageAction] = []
    if stage.action:
        controls.append(StageAction("Run Stage", stage.action, completes=True))
    if stage.skippable:
        controls.append(StageAction("Skip", skippable=True, variant="secondary"))
    if not controls:
        controls.append(StageAction("Complete", completes=True, variant="success"))
    return tuple(controls)


def toc_row_states(
    workflow: Workflow,
    state: WorkflowState,
    *,
    status_for: Callable[[int, Stage], Any] | None = None,
) -> tuple[TocRowState, ...]:
    rows = []
    for index, stage in enumerate(workflow.stages):
        status = status_for(index, stage) if status_for is not None else state.statuses.get(stage.id, StageStatus.PENDING)
        rows.append(
            TocRowState(
                index=index,
                stage_id=stage.id,
                label=stage.label,
                selected=index == state.index,
                status=status,
            )
        )
    return tuple(rows)


def instruction_text(stage: Stage, guard_value: Callable[[str], bool]) -> str:
    for instruction in stage.instruction_cards:
        if not instruction.guard or guard_enabled(instruction.guard, guard_value):
            return instruction.text
    return stage.description or stage.label
