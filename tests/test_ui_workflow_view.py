from __future__ import annotations

import unittest

from admet.ui.workflow_view import (
    action_button_state,
    current_stage,
    guard_enabled,
    instruction_text,
    stage_by_id,
    stage_controls,
    toc_row_states,
    workflow_progress_percent,
)
from admet.workflows import Stage, StageAction, StageInstruction, StageStatus, Workflow, WorkflowState


class WorkflowViewTests(unittest.TestCase):
    def test_current_stage_clamps_state_index(self) -> None:
        workflow = Workflow("test", "Test", (Stage("one", "One"), Stage("two", "Two")))

        self.assertEqual(current_stage(workflow, WorkflowState(index=-4)).id, "one")
        self.assertEqual(current_stage(workflow, WorkflowState(index=42)).id, "two")

    def test_stage_by_id_falls_back_to_current_stage(self) -> None:
        workflow = Workflow("test", "Test", (Stage("one", "One"), Stage("two", "Two")))
        state = WorkflowState(index=1)

        self.assertEqual(stage_by_id(workflow, state, "one").id, "one")
        self.assertEqual(stage_by_id(workflow, state, "missing").id, "two")

    def test_guard_enabled_supports_and_and_not(self) -> None:
        values = {"project_ready": True, "camera_live": False}

        self.assertTrue(guard_enabled("project_ready and not camera_live", values.__getitem__))
        self.assertFalse(guard_enabled("project_ready and camera_live", values.__getitem__))

    def test_instruction_text_uses_first_matching_card(self) -> None:
        stage = Stage(
            "camera",
            "Camera",
            description="Fallback",
            instruction_cards=(
                StageInstruction("Connect camera.", "not camera_connected"),
                StageInstruction("Start live preview.", "camera_connected"),
            ),
        )

        self.assertEqual(
            instruction_text(stage, {"camera_connected": False}.__getitem__),
            "Connect camera.",
        )
        self.assertEqual(
            instruction_text(stage, {"camera_connected": True}.__getitem__),
            "Start live preview.",
        )

    def test_action_button_state_resolves_guard_and_variant(self) -> None:
        action = StageAction("Run", guard="project_ready", variant="warning")

        state = action_button_state(action, {"project_ready": True}.__getitem__)
        self.assertEqual(state.label, "Run")
        self.assertTrue(state.enabled)
        self.assertFalse(state.active)
        self.assertTrue(state.warning)

        disabled = action_button_state(action, {"project_ready": False}.__getitem__)
        self.assertFalse(disabled.enabled)

    def test_action_button_state_accepts_mode_overrides(self) -> None:
        action = StageAction("Start live", guard="camera_connected", variant="primary")

        state = action_button_state(
            action,
            {"camera_connected": True}.__getitem__,
            enabled=False,
            active=True,
            toggle=True,
            label="Live",
        )
        self.assertEqual(state.label, "Live")
        self.assertFalse(state.enabled)
        self.assertTrue(state.active)
        self.assertTrue(state.toggle)

    def test_toc_row_states_use_workflow_state_by_default(self) -> None:
        workflow = Workflow("test", "Test", (Stage("one", "One"), Stage("two", "Two")))
        state = WorkflowState(index=1, statuses={"one": StageStatus.COMPLETE, "two": StageStatus.ACTIVE})

        rows = toc_row_states(workflow, state)
        self.assertEqual([row.stage_id for row in rows], ["one", "two"])
        self.assertEqual([row.label for row in rows], ["One", "Two"])
        self.assertFalse(rows[0].selected)
        self.assertTrue(rows[1].selected)
        self.assertIs(rows[0].status, StageStatus.COMPLETE)

    def test_toc_row_states_accept_mode_status_resolver(self) -> None:
        workflow = Workflow("test", "Test", (Stage("one", "One"), Stage("two", "Two")))

        rows = toc_row_states(
            workflow,
            WorkflowState(index=0),
            status_for=lambda index, _stage: "done" if index == 0 else "inactive",
        )

        self.assertEqual([row.status for row in rows], ["done", "inactive"])

    def test_workflow_progress_percent_counts_done_and_active_stage(self) -> None:
        workflow = Workflow("test", "Test", (Stage("one", "One"), Stage("two", "Two")))
        state = WorkflowState(index=1, statuses={"one": StageStatus.COMPLETE, "two": StageStatus.ACTIVE})

        self.assertEqual(workflow_progress_percent(workflow, state), 77.5)

    def test_stage_controls_use_declared_actions_or_fallbacks(self) -> None:
        explicit = Stage("explicit", "Explicit", actions=(StageAction("Run", "run"),))
        self.assertEqual(stage_controls(explicit), explicit.actions)

        runnable = stage_controls(Stage("run", "Run", action="run_stage"))
        self.assertEqual([(action.label, action.action) for action in runnable], [("Run Stage", "run_stage")])

        skippable = stage_controls(Stage("skip", "Skip", skippable=True))
        self.assertEqual([(action.label, action.skippable) for action in skippable], [("Skip", True)])

        self.assertEqual(stage_controls(Stage("pipe", "Pipe", pipeline=True)), ())


if __name__ == "__main__":
    unittest.main()
