from __future__ import annotations

import unittest

from admet.core.engine import Param
from admet.ui.presenter import build_screen
from admet.ui.surfaces import NICEGUI_SURFACES, QT_SURFACES
from admet.workflows import Stage, StageControl, StageStatus, Workflow, WorkflowState
from admet.workflows.analyze import create_analyze_workflow
from admet.workflows.control import create_control_workflow


class PresenterTests(unittest.TestCase):
    def test_presenter_maps_every_stage_from_workflow_declarations(self):
        for workflow in (create_analyze_workflow(), create_control_workflow()):
            for index, stage in enumerate(workflow.stages):
                with self.subTest(workflow=workflow.id, stage=stage.id):
                    screen = build_screen(workflow, _state_for(workflow, index))

                    self.assertEqual(screen.workflow_id, workflow.id)
                    self.assertEqual(
                        [step.id for step in screen.steps],
                        [stage.id for stage in workflow.stages],
                    )
                    self.assertEqual([step.current for step in screen.steps].count(True), 1)
                    self.assertTrue(screen.steps[index].current)
                    self.assertEqual(screen.title, stage.label)
                    self.assertEqual(screen.description, stage.description)
                    self.assertEqual(screen.instructions, _stage_instructions(stage))
                    self.assertEqual(
                        [field.name for field in screen.fields],
                        _stage_visible_field_names(stage),
                    )
                    self.assertEqual(
                        [field.name for field in screen.settings.all_fields],
                        _stage_all_field_names(stage),
                    )
                    self.assertEqual(
                        [surface.kind for surface in screen.surfaces],
                        [surface.kind for surface in stage.surfaces],
                    )
                    self.assertEqual(
                        [button.command for button in screen.buttons],
                        _stage_button_commands(stage, index),
                    )

    def test_presenter_keeps_show_settings_false_on_control_camera_surface(self):
        workflow = create_control_workflow()
        screen = build_screen(workflow, workflow.initial_state())

        self.assertEqual(screen.fields, ())
        self.assertEqual(len(screen.surfaces), 1)
        surface = screen.surfaces[0]
        self.assertEqual(surface.kind, "camera")
        self.assertEqual(surface.title, "Camera")
        self.assertIn("camera_width", [field.name for field in surface.fields])
        self.assertEqual(
            [button.command for button in surface.buttons],
            [
                "refresh_cameras",
                "connect_camera",
                "start_camera_live",
                "stop_camera_live",
            ],
        )

    def test_presenter_runtime_is_command_keyed(self):
        workflow = create_analyze_workflow()
        screen = build_screen(workflow, _state_for(workflow, 1), CommandRuntime("analyze"))
        run_button = next(button for button in screen.buttons if button.command == "analyze")

        self.assertEqual(run_button.label, "Run OpenCV")
        self.assertFalse(run_button.enabled)
        self.assertTrue(run_button.active)

    def test_presenter_adds_lifecycle_buttons_once(self):
        workflow = create_analyze_workflow()
        screen = build_screen(workflow, _state_for(workflow, 4))
        commands = [button.command for button in screen.buttons]

        self.assertEqual(commands[0], "back")
        self.assertEqual(commands.count("skip"), 1)
        self.assertEqual(commands, ["back", "skip", "complete"])

    def test_correction_settings_are_grouped_by_presenter(self):
        workflow = create_control_workflow()
        screen = build_screen(workflow, _state_for(workflow, 2))

        self.assertEqual(
            [field.name for field in screen.settings.fields],
            [
                "oil_l_calibration",
                "oil_l_scale",
                "cells_m_calibration",
                "cells_m_scale",
                "beads_m_calibration",
                "beads_m_scale",
            ],
        )
        self.assertEqual(
            [field.name for field in screen.settings.all_fields[6:12]],
            [
                "oil_l_offset",
                "oil_l_quadratic",
                "cells_m_offset",
                "cells_m_quadratic",
                "beads_m_offset",
                "beads_m_quadratic",
            ],
        )

    def test_all_declared_surface_kinds_have_renderers(self):
        kinds = {
            surface.kind
            for workflow in (create_analyze_workflow(), create_control_workflow())
            for stage in workflow.stages
            for surface in stage.surfaces
        }

        self.assertLessEqual(kinds, set(NICEGUI_SURFACES))
        self.assertLessEqual(kinds, set(QT_SURFACES))


def _state_for(workflow: Workflow, index: int) -> WorkflowState:
    statuses = {stage.id: StageStatus.PENDING for stage in workflow.stages}
    statuses[workflow.stages[index].id] = StageStatus.ACTIVE
    return WorkflowState(index=index, statuses=statuses)


def _stage_visible_field_names(stage: Stage) -> list[str]:
    names = _stage_all_field_names(stage)
    collapsed_count = int(stage.settings_options.get("collapsed_count") or 0)
    if collapsed_count:
        return names[:collapsed_count]
    if len(names) > 6:
        return names[:6]
    return names


def _stage_all_field_names(stage: Stage) -> list[str]:
    if not stage.show_settings:
        return []
    primary = _stage_named_fields(stage, "primary")
    secondary = _stage_named_fields(stage, "secondary")
    ordered = {*primary, *secondary}
    return [*primary, *secondary, *(field.name for field in stage.settings.params if field.name not in ordered)]


def _stage_named_fields(stage: Stage, key: str) -> list[str]:
    available = {field.name for field in stage.settings.params}
    return [name for name in tuple(stage.settings_options.get(key) or ()) if name in available]


def _stage_button_commands(stage: Stage, index: int) -> list[str]:
    commands = [_control_command(control) for control in stage.controls]
    if index > 0:
        commands.insert(0, "back")
    if stage.skippable and "skip" not in commands:
        commands.append("skip")
    return commands


def _control_command(control: StageControl) -> str:
    if control.action:
        return control.action
    if control.completes:
        return "complete"
    if control.skippable:
        return "skip"
    if control.advances:
        return "advance"
    return "_".join(control.label.strip().lower().split())


def _stage_instructions(stage: Stage) -> tuple[str, ...]:
    if stage.instructions:
        return stage.instructions
    if stage.description:
        return (stage.description,)
    return ()


class CommandRuntime:
    def __init__(self, disabled_command: str):
        self.disabled_command = disabled_command

    def value_for(self, field: Param):
        return field.default

    def button_enabled(self, command: str) -> bool:
        return command != self.disabled_command

    def button_active(self, command: str) -> bool:
        return command == self.disabled_command

    def instructions_for(self, stage: Stage) -> tuple[str, ...]:
        return _stage_instructions(stage)


if __name__ == "__main__":
    unittest.main()
