from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from admet.ui import design

ButtonVariant = Literal["neutral", "primary", "success", "danger", "warning"]
ControlSize = Literal["inline", "default", "large", "stage"]


@dataclass(frozen=True)
class _ControlSize:
    height: int
    font_size: int


class Theme:
    BG_BLACK = "#101820"
    BG_DARK = design.PALETTE.background
    BG_DARKER = design.PALETTE.background_alt
    BG_MEDIUM = design.PALETTE.background_alt
    BG_RAISED = design.PALETTE.raised
    BG_CONTROL = design.PALETTE.control
    BG_CONTROL_HOVER = design.PALETTE.control_hover
    BG_CONTROL_PRESSED = design.PALETTE.control_pressed

    TEXT_WHITE = design.PALETTE.text
    TEXT_MUTED = design.PALETTE.text_muted
    TEXT_SUBTLE = design.PALETTE.text_subtle
    TEXT_DISABLED = design.PALETTE.text_disabled

    BORDER_COOL = design.PALETTE.border
    BORDER_HOVER = design.PALETTE.border_hover

    ACCENT = design.PALETTE.accent
    ACCENT_HOVER = design.PALETTE.accent_hover
    SUCCESS = design.PALETTE.success
    SUCCESS_HOVER = design.PALETTE.success_hover
    DANGER = design.PALETTE.danger
    DANGER_HOVER = design.PALETTE.danger_hover
    WARNING = design.PALETTE.warning
    WARNING_DARK = design.PALETTE.warning_hover

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
    SPLITTER_HANDLE_WIDTH = 8


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


def spacing(value: str | int | None = "default") -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return _SPACING.get(value, Theme.GROUP_GAP)


def box_padding(value: str | int | tuple[int, int, int, int] | None = "none") -> tuple[int, int, int, int]:
    if isinstance(value, tuple):
        return value
    pad = spacing(value)
    return pad, pad, pad, pad


def text_qss(
    kind: str = "default",
    *,
    font_size: int | None = None,
    bold: bool = False,
    padding: str | None = None,
) -> str:
    parts = [f"color: {_TEXT_COLORS.get(kind, kind)};"]
    if font_size is not None:
        parts.append(f"font-size: {font_size}px;")
    if bold:
        parts.append("font-weight: 600;")
    if padding is not None:
        parts.append(f"padding: {padding};")
    return " ".join(parts)


def button_qss(kind: ButtonVariant = "neutral", *, size: ControlSize = "default") -> str:
    bg, hover = _BUTTON_COLORS.get(kind, _BUTTON_COLORS["neutral"])
    token = control_size(size)
    text = "#ffffff" if kind != "neutral" else Theme.TEXT_WHITE
    border = bg if kind != "neutral" else Theme.BORDER_COOL
    return (
        f"QPushButton {{ background-color: {bg}; border: 1px solid {border}; color: {text}; "
        f"border-radius: {Theme.RADIUS}px; padding: 0; font-size: {token.font_size}px; "
        f"font-weight: 600; min-height: {token.height}px; max-height: {token.height}px; }}"
        f"QPushButton:hover {{ background-color: {hover}; }}"
        f"QPushButton:pressed {{ background-color: {Theme.BG_CONTROL_PRESSED}; }}"
        f"QPushButton:disabled {{ background-color: {Theme.BG_MEDIUM}; "
        f"color: {Theme.TEXT_DISABLED}; }}"
    )


def apply_button_style(
    widget,
    *,
    variant: ButtonVariant = "neutral",
    size: ControlSize = "default",
):
    widget.setStyleSheet(button_qss(variant, size=size))
    token = control_size(size)
    widget.setMinimumHeight(token.height)
    widget.setMaximumHeight(token.height)
    return widget


def button(
    text: str,
    *,
    variant: ButtonVariant = "neutral",
    size: ControlSize = "default",
    checkable: bool = False,
):
    from PySide6.QtWidgets import QPushButton

    widget = QPushButton(text)
    widget.setCheckable(checkable)
    return apply_button_style(widget, variant=variant, size=size)


def stage_button(text: str, *, active: bool = False):
    from PySide6.QtWidgets import QSizePolicy

    widget = button(text, variant="primary" if active else "neutral", size="stage", checkable=True)
    widget.setChecked(active)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return widget


def line_edit(text: str = "", *, width: int | None = None):
    from PySide6.QtWidgets import QLineEdit

    widget = QLineEdit(text)
    _apply_control_size(widget)
    if width is not None:
        widget.setFixedWidth(width)
    return widget


def int_box(
    *,
    minimum: int = 0,
    maximum: int = 100,
    value: int = 0,
    step: int = 1,
    width: int | None = None,
):
    from PySide6.QtWidgets import QSpinBox

    widget = QSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    widget.setSingleStep(step)
    _apply_control_size(widget)
    if width is not None:
        widget.setFixedWidth(width)
    return widget


def double_box(
    *,
    minimum: float = 0.0,
    maximum: float = 100.0,
    value: float = 0.0,
    step: float = 1.0,
    decimals: int = 2,
    suffix: str = "",
    width: int | None = None,
):
    from PySide6.QtWidgets import QDoubleSpinBox

    widget = QDoubleSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    widget.setSingleStep(step)
    widget.setDecimals(decimals)
    widget.setSuffix(suffix)
    _apply_control_size(widget)
    if width is not None:
        widget.setFixedWidth(width)
    return widget


def combo_box(items=(), *, width: int | None = None):
    from PySide6.QtWidgets import QComboBox

    widget = QComboBox()
    widget.addItems([str(item) for item in items])
    _apply_control_size(widget)
    if width is not None:
        widget.setFixedWidth(width)
    return widget


def check_box(text: str, *, checked: bool = False):
    from PySide6.QtWidgets import QCheckBox

    widget = QCheckBox(text)
    widget.setChecked(checked)
    return widget


def section(title: str):
    from PySide6.QtWidgets import QGroupBox, QVBoxLayout

    group = QGroupBox(title)
    layout = QVBoxLayout(group)
    layout.setContentsMargins(*box_padding("panel"))
    layout.setSpacing(spacing("group"))
    return group, layout


def button_row(*widgets, align: str = "left"):
    from PySide6.QtWidgets import QHBoxLayout, QWidget

    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(*box_padding("none"))
    layout.setSpacing(spacing("control"))
    if align == "right":
        layout.addStretch()
    for widget in widgets:
        layout.addWidget(widget)
    if align == "left":
        layout.addStretch()
    return container


def field_row(*widgets):
    from PySide6.QtWidgets import QHBoxLayout, QWidget

    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(*box_padding("none"))
    layout.setSpacing(spacing("control"))
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch()
    return container


def control_row(label: str, control, *actions, label_width: int = 90):
    from PySide6.QtWidgets import QLabel, QHBoxLayout, QWidget

    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(*box_padding("none"))
    layout.setSpacing(spacing("control"))
    label_widget = QLabel(label)
    label_widget.setFixedWidth(label_width)
    layout.addWidget(label_widget)
    layout.addWidget(control, 1)
    for action in actions:
        layout.addWidget(action)
    return container


def toolbar(title: str):
    from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(*box_padding("none"))
    layout.setSpacing(spacing("control"))
    label = QLabel(title)
    label.setStyleSheet(text_qss("default", font_size=Theme.FONT_SIZE_TITLE, bold=True))
    layout.addWidget(label)
    layout.addStretch()
    return widget


def stylesheet() -> str:
    default = control_size()
    return f"""
QWidget {{
    background-color: {Theme.BG_DARK};
    color: {Theme.TEXT_WHITE};
    font-size: {Theme.FONT_SIZE_BODY}px;
}}
QMainWindow {{
    background-color: {Theme.BG_DARK};
}}
QLabel {{
    background: transparent;
}}
QFrame#TopPanel,
QFrame#WorkflowToc {{
    background: {Theme.BG_CONTROL};
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: {Theme.RADIUS}px;
}}
QFrame#MainPanel,
QFrame#Panel {{
    background: transparent;
    border: 0;
    border-radius: 0;
}}
QLabel#AppTitle {{
    font-size: 20px;
    font-weight: 650;
}}
QLabel#MutedText,
QLabel#StageSummary {{
    color: {Theme.TEXT_MUTED};
}}
QLabel#FieldLabel {{
    color: {Theme.TEXT_MUTED};
    font-weight: 600;
}}
QLabel#ChannelName {{
    color: {Theme.TEXT_WHITE};
    font-weight: 650;
}}
QLabel#ProjectBadge {{
    background: {Theme.BG_RAISED};
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: {Theme.RADIUS}px;
    padding: 5px 9px;
    color: {Theme.TEXT_MUTED};
}}
QLabel#StageTitle {{
    font-size: 24px;
    font-weight: 650;
}}
QLabel#PanelTitle {{
    font-size: 16px;
    font-weight: 650;
}}
QLabel#TocTitle {{
    color: {Theme.TEXT_MUTED};
    font-size: 11px;
    font-weight: 650;
}}
QFrame#TocSection,
QWidget#TocRow {{
    background: transparent;
    border: 0;
    border-radius: 6px;
}}
QFrame#InlinePanel {{
    background: transparent;
    border: 0;
    border-radius: 0;
}}
QWidget#MainDisplay {{
    background: transparent;
}}
QWidget#ChannelCard {{
    background: transparent;
    border: 0;
    border-radius: 0;
}}
QWidget#TocRow:hover {{
    background: {Theme.BG_RAISED};
}}
QLabel#TocStage {{
    background: transparent;
    font-size: 13px;
}}
QFrame#ProcessBar {{
    background: {Theme.BG_RAISED};
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: {Theme.RADIUS}px;
}}
QFrame#ProtocolStatus {{
    background: {Theme.BG_CONTROL};
    border: 0;
    border-bottom: 1px solid {Theme.BORDER_COOL};
    border-top-left-radius: {Theme.RADIUS}px;
    border-top-right-radius: {Theme.RADIUS}px;
}}
QLabel#ProtocolStatusLabel {{
    background: transparent;
    color: {Theme.TEXT_WHITE};
    font-size: 14px;
    font-weight: 650;
}}
QLabel#ProtocolConfirmLabel {{
    background: {Theme.BG_RAISED};
    border-left: 3px solid {Theme.WARNING};
    color: {Theme.TEXT_WHITE};
    padding: 6px 8px;
    font-size: 13px;
}}
QWidget#CommandRow {{
    background: transparent;
}}
QFrame#TransportButtons {{
    background: transparent;
    border: 0;
    border-radius: 0;
}}
QFrame#TransportSeparator {{
    background: {Theme.BORDER_COOL};
    border: 0;
}}
QPushButton#TransportButton {{
    background: {Theme.BG_CONTROL};
    border: 0;
    border-radius: 0;
    color: {Theme.TEXT_WHITE};
    min-height: 22px;
    max-height: 22px;
    padding: 0;
    font-weight: 500;
}}
QPushButton#TransportButton:hover {{
    background: {Theme.BG_CONTROL_HOVER};
}}
QPushButton#TransportButton:pressed {{
    background: {Theme.BG_CONTROL_PRESSED};
}}
QPushButton#TransportButton:checked {{
    background: {Theme.BG_CONTROL_PRESSED};
    color: {Theme.ACCENT};
    font-weight: 650;
}}
QPushButton#TransportButton:disabled {{
    color: {Theme.TEXT_DISABLED};
    background: {Theme.BG_CONTROL};
}}
QPushButton#TransportButtonWarning {{
    background: {Theme.WARNING};
    border: 0;
    border-radius: 0;
    color: #ffffff;
    min-height: 22px;
    max-height: 22px;
    padding: 0;
    font-weight: 700;
}}
QPushButton#TransportButtonWarning:hover {{
    background: {Theme.WARNING_DARK};
}}
QPushButton#TransportButtonWarning:pressed {{
    background: {Theme.WARNING_DARK};
}}
QLabel#ProcessAction {{
    background: transparent;
    font-weight: 600;
}}
QWidget#CameraPreview {{
    background: {Theme.BG_BLACK};
    color: #dfe7f1;
    border-radius: {Theme.RADIUS}px;
}}
QWidget#CameraSelectorRow {{
    background: transparent;
}}
QComboBox#CameraSelector {{
    min-height: 24px;
    max-height: 24px;
}}
QLabel#LogText {{
    background: {Theme.BG_RAISED};
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: {Theme.RADIUS}px;
    padding: 8px 10px;
    color: {Theme.TEXT_MUTED};
    font-family: Menlo, Consolas, monospace;
    font-size: 12px;
}}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {Theme.BG_CONTROL};
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: {Theme.RADIUS}px;
    min-height: {default.height}px;
    max-height: {default.height}px;
    padding: 0;
    color: {Theme.TEXT_WHITE};
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{
    border-color: {Theme.BORDER_HOVER};
}}
QPushButton {{
    background-color: {Theme.BG_CONTROL};
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: {Theme.RADIUS}px;
    min-height: {default.height}px;
    max-height: {default.height}px;
    padding: 0;
    color: {Theme.TEXT_WHITE};
    font-weight: 600;
}}
QPushButton:hover {{
    background-color: {Theme.BG_CONTROL_HOVER};
}}
QPushButton:pressed {{
    background-color: {Theme.BG_CONTROL_PRESSED};
}}
QPushButton:disabled {{
    color: {Theme.TEXT_DISABLED};
    background-color: {Theme.BG_MEDIUM};
}}
QCheckBox {{
    spacing: {Theme.CONTROL_GAP}px;
    background: transparent;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: 3px;
    background: {Theme.BG_CONTROL};
}}
QCheckBox::indicator:checked {{
    background: {Theme.ACCENT};
    border-color: {Theme.ACCENT};
}}
QGroupBox {{
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: {Theme.RADIUS}px;
    margin-top: {Theme.GROUP_GAP}px;
    padding-top: {Theme.SPACE_3}px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: {Theme.PANEL_PADDING}px;
    padding: 0 {Theme.CONTROL_GAP}px;
    color: {Theme.TEXT_MUTED};
}}
QProgressBar {{
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: 5px;
    background: {Theme.BG_CONTROL};
    text-align: center;
    max-height: 8px;
}}
QProgressBar#ProcessProgress {{
    border: 0;
    border-radius: 0;
    background: transparent;
    min-height: 4px;
    max-height: 4px;
}}
QProgressBar::chunk {{
    background: {Theme.ACCENT};
    border-radius: 4px;
}}
QProgressBar#ProcessProgress::chunk {{
    border-radius: 0;
}}
QProgressBar#ProtocolProgress {{
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: 4px;
    background: {Theme.BG_RAISED};
    color: {Theme.TEXT_WHITE};
    text-align: center;
    min-height: 18px;
    max-height: 18px;
    font-size: 12px;
    font-weight: 600;
}}
QProgressBar#ProtocolProgress::chunk {{
    border-radius: 3px;
}}
QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollArea#PageScroll {{
    border: none;
    background: transparent;
}}
QPlainTextEdit, QTableWidget {{
    background-color: {Theme.BG_CONTROL};
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: {Theme.RADIUS}px;
    color: {Theme.TEXT_WHITE};
    gridline-color: {Theme.BORDER_COOL};
}}
QHeaderView::section {{
    background-color: {Theme.BG_RAISED};
    color: {Theme.TEXT_MUTED};
    border: 0;
    border-bottom: 1px solid {Theme.BORDER_COOL};
    padding: 3px 6px;
}}
QTableWidget#RawConfigTable::item {{
    border-color: {Theme.BORDER_COOL};
    border-bottom: 1px solid {Theme.BORDER_COOL};
    border-right: 1px solid {Theme.BORDER_COOL};
    padding: 4px 7px;
}}
QScrollBar:horizontal,
QScrollBar:vertical {{
    width: 0;
    height: 0;
    background: transparent;
    border: 0;
}}
"""


def _apply_control_size(widget, size: ControlSize = "default") -> None:
    token = control_size(size)
    widget.setMinimumHeight(token.height)
    widget.setMaximumHeight(token.height)
