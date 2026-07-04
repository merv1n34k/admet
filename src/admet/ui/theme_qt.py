from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from admet.ui.design import (
    BUTTON_COLORS,
    PALETTE,
    RADII,
    SPACING,
    STATUS_COLORS,
    TEXT_COLORS,
    TYPOGRAPHY,
    spacing_value,
)

ButtonVariant = Literal["neutral", "primary", "success", "danger", "warning"]
ControlSize = Literal["inline", "default", "large", "stage"]


@dataclass(frozen=True)
class _ControlSize:
    height: int
    font_size: int


class Theme:
    BG_DARK = PALETTE.background
    BG_DARKER = PALETTE.background_alt
    BG_RAISED = PALETTE.raised
    BG_CONTROL = PALETTE.control
    BG_CONTROL_HOVER = PALETTE.control_hover
    BG_CONTROL_PRESSED = PALETTE.control_pressed
    TEXT_WHITE = PALETTE.text
    TEXT_MUTED = PALETTE.text_muted
    TEXT_SUBTLE = PALETTE.text_subtle
    TEXT_DISABLED = PALETTE.text_disabled
    BORDER_COOL = PALETTE.border
    BORDER_HOVER = PALETTE.border_hover
    ACCENT = PALETTE.accent
    ACCENT_HOVER = PALETTE.accent_hover
    SUCCESS = PALETTE.success
    SUCCESS_HOVER = PALETTE.success_hover
    DANGER = PALETTE.danger
    DANGER_HOVER = PALETTE.danger_hover
    WARNING = PALETTE.warning
    WARNING_DARK = PALETTE.warning_hover
    FONT_SIZE_SMALL = TYPOGRAPHY.small
    FONT_SIZE_BODY = TYPOGRAPHY.body
    FONT_SIZE_TITLE = TYPOGRAPHY.title
    SPACE_0 = SPACING.none
    SPACE_1 = SPACING.tight
    SPACE_2 = SPACING.default
    SPACE_3 = SPACING.panel
    SPACE_4 = SPACING.window
    CONTROL_GAP = SPACING.control
    GROUP_GAP = SPACING.default
    PANEL_PADDING = SPACING.panel
    WINDOW_PADDING = SPACING.window
    RADIUS = RADII.control


_SIZES = {
    "inline": _ControlSize(20, TYPOGRAPHY.body),
    "default": _ControlSize(20, TYPOGRAPHY.body),
    "large": _ControlSize(24, TYPOGRAPHY.body),
    "stage": _ControlSize(20, TYPOGRAPHY.body),
}


def control_size(size: ControlSize = "default") -> _ControlSize:
    return _SIZES.get(size, _SIZES["default"])


def status_color(status: str) -> str:
    return STATUS_COLORS[status]


def spacing(value: str | int | None = "default") -> int:
    return spacing_value(value)


def box_padding(
    value: str | int | tuple[int, int, int, int] | None = "none",
) -> tuple[int, int, int, int]:
    if isinstance(value, tuple):
        return value
    pad = spacing(value)
    return (pad, pad, pad, pad)


def text_qss(
    kind: str = "default",
    *,
    size: int | None = None,
    bold: bool = False,
) -> str:
    weight = "600" if bold else "400"
    font_size = size if size is not None else TYPOGRAPHY.body
    return (
        f"color: {TEXT_COLORS.get(kind, PALETTE.text)}; "
        f"font-size: {font_size}px; font-weight: {weight};"
    )


def button_qss(kind: ButtonVariant = "neutral", *, size: ControlSize = "default") -> str:
    color, hover = BUTTON_COLORS.get(kind, BUTTON_COLORS["neutral"])
    text = PALETTE.white if kind != "neutral" else PALETTE.text
    control = control_size(size)
    border = color if kind != "neutral" else PALETTE.border
    return f"""
    QPushButton {{
      background: {color};
      color: {text};
      border: 1px solid {border};
      border-radius: {RADII.control}px;
      min-height: {control.height}px;
      padding: 0 {SPACING.default}px;
      font-size: {control.font_size}px;
      font-weight: {TYPOGRAPHY.button_weight};
    }}
    QPushButton:hover {{ background: {hover}; border-color: {PALETTE.border_hover}; }}
    QPushButton:pressed, QPushButton:checked {{ background: {PALETTE.control_pressed}; color: {PALETTE.text}; }}
    QPushButton:disabled {{ color: {PALETTE.text_disabled}; background: {PALETTE.raised}; border-color: {PALETTE.border}; }}
    """


def stylesheet() -> str:
    return f"""
    QMainWindow, QWidget {{
      background: {PALETTE.background};
      color: {PALETTE.text};
      font-size: {TYPOGRAPHY.body}px;
    }}
    QLabel {{
      color: {PALETTE.text};
      font-size: {TYPOGRAPHY.body}px;
    }}
    QLabel#MutedText {{
      color: {PALETTE.text_muted};
      font-size: {TYPOGRAPHY.small}px;
    }}
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
      background: {PALETTE.control};
      color: {PALETTE.text};
      border: 1px solid {PALETTE.border};
      border-radius: {RADII.control}px;
      min-height: 22px;
      padding: 0 6px;
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
      border-color: {PALETTE.border_hover};
    }}
    QScrollArea {{
      border: 0;
      background: transparent;
    }}
    QTableWidget {{
      background: {PALETTE.control};
      border: 1px solid {PALETTE.border};
      border-radius: {RADII.control}px;
      gridline-color: {PALETTE.border};
    }}
    QHeaderView::section {{
      background: {PALETTE.raised};
      color: {PALETTE.text_muted};
      border: 0;
      padding: {SPACING.tight}px;
      font-weight: 600;
    }}
    {button_qss("neutral")}
    """
