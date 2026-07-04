from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ButtonVariant = Literal["neutral", "primary", "success", "danger", "warning"]
ControlSize = Literal["inline", "default", "large", "stage"]


@dataclass(frozen=True)
class _ControlSize:
    height: int
    font_size: int


class Theme:
    BG_DARK = "#f7fafc"
    BG_DARKER = "#edf3f7"
    BG_RAISED = "#f0f5f8"
    BG_CONTROL = "#ffffff"
    BG_CONTROL_HOVER = "#f0f5f8"
    BG_CONTROL_PRESSED = "#e6eef4"

    TEXT_WHITE = "#16212b"
    TEXT_MUTED = "#52677a"
    TEXT_SUBTLE = "#7b8c9a"
    TEXT_DISABLED = "#9aa7b2"

    BORDER_COOL = "#d7e2ea"
    BORDER_HOVER = "#a9bac8"

    ACCENT = "#225d82"
    ACCENT_HOVER = "#1b4a68"
    SUCCESS = "#1b6b53"
    SUCCESS_HOVER = "#185e49"
    DANGER = "#8b2b2b"
    DANGER_HOVER = "#742323"
    WARNING = "#b7791f"
    WARNING_DARK = "#9d661a"

    FONT_SIZE_SMALL = 11
    FONT_SIZE_BODY = 13
    FONT_SIZE_TITLE = 16

    SPACE_0 = 0
    SPACE_1 = 4
    SPACE_2 = 8
    SPACE_3 = 12
    SPACE_4 = 16
    CONTROL_GAP = SPACE_1
    GROUP_GAP = SPACE_2
    PANEL_PADDING = SPACE_3
    WINDOW_PADDING = SPACE_4
    RADIUS = 8


STATUS_COLORS = {
    "done": Theme.SUCCESS,
    "processing": Theme.WARNING,
    "error": Theme.DANGER,
    "inactive": Theme.TEXT_SUBTLE,
}

_SIZES = {
    "inline": _ControlSize(20, Theme.FONT_SIZE_BODY),
    "default": _ControlSize(20, Theme.FONT_SIZE_BODY),
    "large": _ControlSize(24, Theme.FONT_SIZE_BODY),
    "stage": _ControlSize(20, Theme.FONT_SIZE_BODY),
}

_SPACING = {
    "none": Theme.SPACE_0,
    "tight": Theme.SPACE_1,
    "control": Theme.CONTROL_GAP,
    "default": Theme.GROUP_GAP,
    "group": Theme.GROUP_GAP,
    "panel": Theme.PANEL_PADDING,
    "window": Theme.WINDOW_PADDING,
}

_TEXT_COLORS = {
    "default": Theme.TEXT_WHITE,
    "muted": Theme.TEXT_MUTED,
    "subtle": Theme.TEXT_SUBTLE,
    "primary": Theme.ACCENT,
    "success": Theme.SUCCESS,
    "danger": Theme.DANGER_HOVER,
    "warning": Theme.WARNING,
}

_BUTTON_COLORS = {
    "neutral": (Theme.BG_CONTROL, Theme.BG_CONTROL_HOVER),
    "primary": (Theme.ACCENT, Theme.ACCENT_HOVER),
    "success": (Theme.SUCCESS, Theme.SUCCESS_HOVER),
    "danger": (Theme.DANGER, Theme.DANGER_HOVER),
    "warning": (Theme.WARNING_DARK, Theme.WARNING),
}


def control_size(size: ControlSize = "default") -> _ControlSize:
    return _SIZES.get(size, _SIZES["default"])


def status_color(status: str) -> str:
    return STATUS_COLORS[status]


def spacing(value: str | int | None = "default") -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return _SPACING.get(value, Theme.GROUP_GAP)


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
    font_size = size if size is not None else Theme.FONT_SIZE_BODY
    return f"color: {_TEXT_COLORS.get(kind, Theme.TEXT_WHITE)}; font-size: {font_size}px; font-weight: {weight};"


def button_qss(kind: ButtonVariant = "neutral", *, size: ControlSize = "default") -> str:
    color, hover = _BUTTON_COLORS.get(kind, _BUTTON_COLORS["neutral"])
    text = "#ffffff" if kind != "neutral" else Theme.TEXT_WHITE
    control = control_size(size)
    border = color if kind != "neutral" else Theme.BORDER_COOL
    return f"""
    QPushButton {{
      background: {color};
      color: {text};
      border: 1px solid {border};
      border-radius: {Theme.RADIUS}px;
      min-height: {control.height}px;
      padding: 0 8px;
      font-size: {control.font_size}px;
      font-weight: 500;
    }}
    QPushButton:hover {{ background: {hover}; border-color: {Theme.BORDER_HOVER}; }}
    QPushButton:pressed, QPushButton:checked {{ background: {Theme.BG_CONTROL_PRESSED}; color: {Theme.TEXT_WHITE}; }}
    QPushButton:disabled {{ color: {Theme.TEXT_DISABLED}; background: {Theme.BG_RAISED}; border-color: {Theme.BORDER_COOL}; }}
    """


def stylesheet() -> str:
    return f"""
    QMainWindow, QWidget {{
      background: {Theme.BG_DARK};
      color: {Theme.TEXT_WHITE};
      font-size: {Theme.FONT_SIZE_BODY}px;
    }}
    QLabel {{
      color: {Theme.TEXT_WHITE};
      font-size: {Theme.FONT_SIZE_BODY}px;
    }}
    QLabel#MutedText {{
      color: {Theme.TEXT_MUTED};
      font-size: {Theme.FONT_SIZE_SMALL}px;
    }}
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
      background: {Theme.BG_CONTROL};
      color: {Theme.TEXT_WHITE};
      border: 1px solid {Theme.BORDER_COOL};
      border-radius: {Theme.RADIUS}px;
      min-height: 22px;
      padding: 0 6px;
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
      border-color: {Theme.BORDER_HOVER};
    }}
    QScrollArea {{
      border: 0;
      background: transparent;
    }}
    QTableWidget {{
      background: {Theme.BG_CONTROL};
      border: 1px solid {Theme.BORDER_COOL};
      border-radius: {Theme.RADIUS}px;
      gridline-color: {Theme.BORDER_COOL};
    }}
    QHeaderView::section {{
      background: {Theme.BG_RAISED};
      color: {Theme.TEXT_MUTED};
      border: 0;
      padding: 4px;
      font-weight: 600;
    }}
    {button_qss("neutral")}
    """
