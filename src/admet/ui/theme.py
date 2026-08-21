from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ButtonVariant = Literal["neutral", "primary", "success", "danger", "warning"]
ControlSize = Literal["inline", "default", "large", "stage"]


@dataclass(frozen=True)
class Palette:
    background: str = "#f7fafc"
    background_alt: str = "#edf3f7"
    raised: str = "#f0f5f8"
    control: str = "#ffffff"
    control_hover: str = "#f0f5f8"
    control_pressed: str = "#e6eef4"
    text: str = "#16212b"
    text_muted: str = "#52677a"
    text_subtle: str = "#7b8c9a"
    text_disabled: str = "#9aa7b2"
    border: str = "#d7e2ea"
    border_hover: str = "#a9bac8"
    accent: str = "#225d82"
    accent_hover: str = "#1b4a68"
    success: str = "#1b6b53"
    success_hover: str = "#185e49"
    danger: str = "#8b2b2b"
    danger_hover: str = "#742323"
    warning: str = "#b7791f"
    warning_hover: str = "#9d661a"
    roi: str = "#e5f36a"


@dataclass(frozen=True)
class Spacing:
    none: int = 0
    tight: int = 4
    default: int = 8
    panel: int = 12
    window: int = 16


@dataclass(frozen=True)
class Typography:
    body: int = 13
    title: int = 16


@dataclass(frozen=True)
class Radii:
    control: int = 8


@dataclass(frozen=True)
class _ControlSize:
    height: int
    font_size: int


PALETTE = Palette()
SPACING = Spacing()
TYPOGRAPHY = Typography()
RADII = Radii()

STATUS_COLORS = {
    "done": PALETTE.success,
    "processing": PALETTE.warning,
    "error": PALETTE.danger,
    "inactive": PALETTE.text_subtle,
    "complete": PALETTE.success,
    "active": PALETTE.warning,
    "skipped": PALETTE.text_subtle,
    "pending": PALETTE.text_subtle,
}

BUTTON_COLORS = {
    "neutral": (PALETTE.control, PALETTE.control_hover),
    "primary": (PALETTE.accent, PALETTE.accent_hover),
    "success": (PALETTE.success, PALETTE.success_hover),
    "danger": (PALETTE.danger, PALETTE.danger_hover),
    "warning": (PALETTE.warning_hover, PALETTE.warning),
}

_WEB_TOKENS = {
    "text": PALETTE.text,
    "text-muted": PALETTE.text_muted,
    "text-disabled": PALETTE.text_disabled,
    "border": PALETTE.border,
    "accent": PALETTE.accent,
    "success-hover": PALETTE.success_hover,
    "danger-hover": PALETTE.danger_hover,
    "warning": PALETTE.warning,
    "bg-app": PALETTE.background,
    "bg-raised": PALETTE.raised,
    "bg-control": PALETTE.control,
    "bg-control-pressed": PALETTE.control_pressed,
    "roi": PALETTE.roi,
}


class Theme:
    BG_BLACK = "#101820"
    BG_DARK = PALETTE.background
    BG_MEDIUM = PALETTE.background_alt
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

    FONT_SIZE_BODY = TYPOGRAPHY.body
    FONT_SIZE_TITLE = TYPOGRAPHY.title

    SPACE_0 = SPACING.none
    SPACE_1 = SPACING.tight
    SPACE_2 = SPACING.default
    SPACE_3 = SPACING.panel
    SPACE_4 = SPACING.window
    CONTROL_GAP = SPACE_1
    GROUP_GAP = SPACE_2
    PANEL_PADDING = SPACE_3
    WINDOW_PADDING = SPACE_4
    RADIUS = RADII.control


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


def control_size(size: ControlSize = "default") -> _ControlSize:
    return _SIZES.get(size, _SIZES["default"])


def spacing(value: str | int | None = "default") -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return _SPACING.get(value, Theme.GROUP_GAP)


def css_variables() -> str:
    return "".join(f"--{name}:{value};" for name, value in _WEB_TOKENS.items())


def button_qss(kind: ButtonVariant = "neutral", *, size: ControlSize = "default") -> str:
    bg, hover = BUTTON_COLORS.get(kind, BUTTON_COLORS["neutral"])
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


def mono_family() -> str:
    """A fixed-pitch family that is actually installed.

    Qt style sheets accept a single family rather than a CSS fallback list, and
    naming one that is missing makes Qt build its font-alias table -- around 200ms
    at startup, reported as "Populating font family aliases".
    """
    from PySide6.QtGui import QFontDatabase

    installed = set(QFontDatabase.families())
    for candidate in ("Menlo", "Consolas", "DejaVu Sans Mono", "Courier New"):
        if candidate in installed:
            return candidate
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()


def stylesheet() -> str:
    default = control_size()
    mono = mono_family()
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
/* Boxes in the fluidic scheme: sources, sensors, the chip and the collection
   tube, with the tubing fields sitting on the runs between them. */
QLabel#SchemeNode {{
    background: {Theme.BG_CONTROL};
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: {Theme.RADIUS}px;
    padding: 6px 10px;
    color: {Theme.TEXT_WHITE};
    font-weight: 650;
}}
QLabel#SchemeLink {{
    color: {Theme.TEXT_MUTED};
}}
QLabel#VerdictPass, QLabel#VerdictFail {{
    border-radius: {Theme.RADIUS}px;
    padding: 7px 10px;
    font-weight: 700;
    color: #ffffff;
}}
QLabel#VerdictPass {{
    background: {Theme.SUCCESS};
}}
QLabel#VerdictFail {{
    background: {Theme.DANGER};
}}
/* Formulas are shown next to the numbers they produce, so a researcher can check
   the arithmetic. Monospaced so the terms line up between rows. */
QLabel#FormulaText {{
    color: {Theme.TEXT_MUTED};
    font-family: "{mono}";
    background: {Theme.BG_RAISED};
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: {Theme.RADIUS}px;
    padding: 7px 9px;
}}
QLabel#FieldLabel {{
    color: {Theme.TEXT_MUTED};
    font-weight: 600;
}}
QLabel#ChannelName {{
    color: {Theme.TEXT_WHITE};
    font-weight: 650;
}}
QLabel#ProjectBadge, QPushButton#ProjectBadge {{
    background: {Theme.BG_RAISED};
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: {Theme.RADIUS}px;
    padding: 5px 9px;
    color: {Theme.TEXT_MUTED};
    text-align: left;
}}
QPushButton#ProjectBadge:hover {{
    border-color: {Theme.ACCENT};
    color: {Theme.TEXT_WHITE};
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
/* The protocol is driving this channel, so it cannot be set by hand until the
   run is paused. Dim the card so the mode is obvious at a glance. */
QWidget#ChannelCard[locked="true"] QLabel {{
    color: {Theme.TEXT_DISABLED};
}}
QWidget#ChannelCard[locked="true"] QLabel#ChannelName {{
    color: {Theme.TEXT_MUTED};
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
QFrame#TransportButtons QPushButton[roundLeft="true"] {{
    border-bottom-left-radius: {Theme.RADIUS - 1}px;
}}
QFrame#TransportButtons QPushButton[roundRight="true"] {{
    border-bottom-right-radius: {Theme.RADIUS - 1}px;
}}
QFrame#TransportButtons QPushButton[roundLeft="true"][roundTop="true"] {{
    border-top-left-radius: {Theme.RADIUS - 1}px;
}}
QFrame#TransportButtons QPushButton[roundRight="true"][roundTop="true"] {{
    border-top-right-radius: {Theme.RADIUS - 1}px;
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
    font-family: "{mono}";
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
