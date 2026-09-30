from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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


ASSETS = Path(__file__).with_name("assets")


def asset(name: str) -> str:
    """Absolute path to a shipped asset, in the form a stylesheet can use.

    Qt draws a combo box's arrow from an image; without one it draws nothing at
    all once the drop-down itself is styled. The chevrons are shipped rather than
    generated so a read-only install still has them.
    """
    return ASSETS.joinpath(name).as_posix()


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
/* A tag, sized to its one word, with the reasoning set beside it. */
QLabel#VerdictPass, QLabel#VerdictFail {{
    border-radius: {Theme.RADIUS}px;
    padding: 4px 11px;
    font-weight: 700;
    letter-spacing: 0.4px;
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
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {Theme.ACCENT};
}}
/* Styling a combo box without styling its drop-down leaves Qt drawing the
   sub-control unstyled -- a boxed button with its own border and background,
   sitting inside a field that no longer matches it. The button is part of the
   field: no border, no fill of its own, just the arrow. */
QComboBox {{
    combobox-popup: 0;
    selection-background-color: {Theme.ACCENT};
    selection-color: #ffffff;
    padding-left: 7px;
    padding-right: 0;
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 20px;
    border: 0;
    background: transparent;
}}
QComboBox::down-arrow {{
    image: url("{asset("chevron-down.svg")}");
    width: 10px;
    height: 6px;
    margin-right: 7px;
}}
QComboBox::down-arrow:hover, QComboBox::down-arrow:on {{
    image: url("{asset("chevron-down-strong.svg")}");
}}
QComboBox::down-arrow:disabled {{
    image: url("{asset("chevron-down-muted.svg")}");
}}
/* Spin buttons get the same treatment as the drop-down: no frame, no fill, the
   field's own background, and a chevron of the right weight. Left unstyled they
   are a pair of boxed buttons wedged into the field's right edge. */
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    width: 16px;
    height: 12px;
    border: 0;
    background: transparent;
    margin-right: 3px;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-position: top right;
    margin-top: 1px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-position: bottom right;
    margin-bottom: 1px;
}}
/* Smaller than the combo box chevron: two of these stack inside one field, and
   at full size they meet in the middle and read as a single diamond. */
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url("{asset("chevron-up.svg")}");
    width: 8px;
    height: 4px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url("{asset("chevron-down.svg")}");
    width: 8px;
    height: 4px;
}}
QSpinBox::up-arrow:hover, QDoubleSpinBox::up-arrow:hover {{
    image: url("{asset("chevron-up-strong.svg")}");
}}
QSpinBox::down-arrow:hover, QDoubleSpinBox::down-arrow:hover {{
    image: url("{asset("chevron-down-strong.svg")}");
}}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled,
QSpinBox::up-arrow:off, QDoubleSpinBox::up-arrow:off {{
    image: url("{asset("chevron-up-muted.svg")}");
}}
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled,
QSpinBox::down-arrow:off, QDoubleSpinBox::down-arrow:off {{
    image: url("{asset("chevron-down-muted.svg")}");
}}
QSpinBox, QDoubleSpinBox {{
    padding-left: 7px;
    padding-right: 22px;
}}
/* The popup is a view, not a field: it takes the raised surface and keeps the
   selected row readable against the accent. */
QComboBox QAbstractItemView {{
    background-color: {Theme.BG_RAISED};
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: {Theme.RADIUS}px;
    padding: 3px;
    outline: 0;
    color: {Theme.TEXT_WHITE};
    selection-background-color: {Theme.ACCENT};
    selection-color: #ffffff;
}}
QComboBox QAbstractItemView::item {{
    min-height: 22px;
    padding: 0 6px;
    border-radius: 3px;
}}
QComboBox QAbstractItemView::item:selected {{
    background-color: {Theme.ACCENT};
    color: #ffffff;
}}
QComboBox QAbstractItemView::item:hover {{
    background-color: {Theme.ACCENT_HOVER};
    color: #ffffff;
}}
/* An editor dropped into a table cell is the cell. Rounded corners and an inset
   frame made it read as a small box floating inside a larger one, so in a table
   the editor loses its own frame and fills the cell the grid already draws. */
QTableWidget QComboBox,
QTableWidget QLineEdit,
QTableWidget QSpinBox,
QTableWidget QDoubleSpinBox {{
    border: 0;
    border-radius: 0;
    background-color: transparent;
    /* Flush with the labels beside them: an editor indented by its own padding
       does not line up with the cell text in the next column. Room is still kept
       on the right, where the arrows are drawn. */
    padding-left: 0;
    min-height: {default.height}px;
    max-height: {default.height + 4}px;
}}
QTableWidget QComboBox:hover,
QTableWidget QLineEdit:hover,
QTableWidget QSpinBox:hover,
QTableWidget QDoubleSpinBox:hover {{
    background-color: {Theme.BG_CONTROL_HOVER};
}}
QTableWidget QComboBox:focus,
QTableWidget QLineEdit:focus,
QTableWidget QSpinBox:focus,
QTableWidget QDoubleSpinBox:focus {{
    background-color: {Theme.BG_CONTROL};
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
QPlainTextEdit {{
    background-color: {Theme.BG_CONTROL};
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: {Theme.RADIUS}px;
    color: {Theme.TEXT_WHITE};
}}
/* The frame is the table's only boundary. Separators between cells are painted
   by GridTable, which stops before the last row and column, so no edge is drawn
   twice and no straight line runs out through a rounded corner. */
QTableWidget {{
    background-color: {Theme.BG_CONTROL};
    border: 1px solid {Theme.BORDER_COOL};
    border-radius: {Theme.RADIUS}px;
    color: {Theme.TEXT_WHITE};
    gridline-color: {Theme.BORDER_COOL};
    /* Without this the current cell keeps a focus rectangle drawn inside its own
       borders -- the same box-inside-a-box the cell editors used to show. */
    outline: 0;
}}
/* The viewport is a plain child widget filling the frame's contents, and it
   paints its own square background straight over the rounded corners. Letting
   the table's background show through is what makes the radius visible. */
QAbstractScrollArea > QWidget#qt_scrollarea_viewport {{
    background: transparent;
}}
QTableWidget::item {{
    padding: 0;
}}
/* Selecting a cell should mark it, not invert it: the default highlight fills
   the cell with the accent and takes the text with it. */
QTableWidget::item:selected {{
    background-color: {Theme.BG_CONTROL_PRESSED};
    color: {Theme.TEXT_WHITE};
}}
QTableWidget::item:focus {{
    background-color: {Theme.BG_CONTROL_PRESSED};
    color: {Theme.TEXT_WHITE};
}}
/* The header fills the top of the table, so its corners are the table's corners.
   The view itself must not paint -- only its sections -- or its square backdrop
   covers the rounded frame behind it. */
QHeaderView {{
    background: transparent;
    border: 0;
}}
QHeaderView::section {{
    background-color: {Theme.BG_RAISED};
    color: {Theme.TEXT_MUTED};
    border: 0;
    border-right: 1px solid {Theme.BORDER_COOL};
    border-bottom: 1px solid {Theme.BORDER_COOL};
    padding: 0;
}}
QHeaderView::section:first {{
    border-top-left-radius: {Theme.RADIUS}px;
}}
QHeaderView::section:last {{
    border-right: 0;
    border-top-right-radius: {Theme.RADIUS}px;
}}
QHeaderView::section:only-one {{
    border-right: 0;
    border-top-left-radius: {Theme.RADIUS}px;
    border-top-right-radius: {Theme.RADIUS}px;
}}
/* The grid draws these edges; drawing them again per item thickened every line
   and the padding pushed the cell wider than the value inside it. */
QScrollBar:horizontal,
QScrollBar:vertical {{
    width: 0;
    height: 0;
    background: transparent;
    border: 0;
}}
"""
