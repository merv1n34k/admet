from __future__ import annotations

import unittest

from admet.ui.workflow_view import current_stage, guard_enabled, instruction_text, stage_by_id
from admet.workflows import Stage, StageInstruction, Workflow, WorkflowState


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


if __name__ == "__main__":
    unittest.main()
