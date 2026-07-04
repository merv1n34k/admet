from __future__ import annotations

from admet.ui.theme import (
    ButtonVariant,
    ControlSize,
    Theme,
    apply_button_style,
    box_padding,
    button,
    button_qss,
    button_row,
    check_box,
    combo_box,
    control_row,
    control_size,
    double_box,
    field_row,
    int_box,
    line_edit,
    section,
    spacing,
    stage_button,
    stylesheet,
    text_qss,
    toolbar,
)


_STATUS_COLORS = {
    "done": Theme.SUCCESS,
    "processing": Theme.WARNING,
    "error": Theme.DANGER,
    "inactive": Theme.TEXT_SUBTLE,
    "complete": Theme.SUCCESS,
    "active": Theme.ACCENT,
    "skipped": Theme.TEXT_SUBTLE,
    "pending": Theme.TEXT_DISABLED,
}


def status_color(status: str) -> str:
    return _STATUS_COLORS.get(status, Theme.TEXT_SUBTLE)


__all__ = [
    "ButtonVariant",
    "ControlSize",
    "Theme",
    "apply_button_style",
    "box_padding",
    "button",
    "button_qss",
    "button_row",
    "check_box",
    "combo_box",
    "control_row",
    "control_size",
    "double_box",
    "field_row",
    "int_box",
    "line_edit",
    "section",
    "spacing",
    "stage_button",
    "status_color",
    "stylesheet",
    "text_qss",
    "toolbar",
]
