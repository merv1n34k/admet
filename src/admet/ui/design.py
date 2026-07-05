from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    background: str = "#f7fafc"
    background_alt: str = "#edf3f7"
    raised: str = "#f0f5f8"
    control: str = "#ffffff"
    control_hover: str = "#f0f5f8"
    control_pressed: str = "#e6eef4"
    text: str = "#16212b"
    text_heading: str = "#28323f"
    text_step: str = "#334155"
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
    notice_success: str = "#9ed4b4"
    notice_warning: str = "#f4bf71"
    notice_danger: str = "#ee9b9b"
    video_placeholder: str = "#f1f4f8"
    video_caption: str = "#64748b"
    video_bubble: str = "rgba(148, 163, 184, 0.22)"
    white: str = "#ffffff"
    roi: str = "#e5f36a"


@dataclass(frozen=True)
class Spacing:
    none: int = 0
    tight: int = 4
    control: int = 4
    default: int = 8
    panel: int = 12
    window: int = 16
    body_gap: int = 14
    topbar_y: int = 10
    topbar_x: int = 14
    sidebar_width: int = 250
    surface_min_height: int = 140
    preview_min_height: int = 260


@dataclass(frozen=True)
class Typography:
    family: str = "Inter, system-ui, sans-serif"
    small: int = 11
    body: int = 13
    title: int = 16
    panel_title_weight: int = 650
    button_weight: int = 500


@dataclass(frozen=True)
class Radii:
    control: int = 8
    panel: int = 10
    round: int = 999


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

NOTICE_COLORS = {
    "primary": PALETTE.border,
    "success": PALETTE.notice_success,
    "warning": PALETTE.notice_warning,
    "danger": PALETTE.notice_danger,
}

TEXT_COLORS = {
    "default": PALETTE.text,
    "muted": PALETTE.text_muted,
    "subtle": PALETTE.text_subtle,
    "primary": PALETTE.accent,
    "success": PALETTE.success,
    "danger": PALETTE.danger_hover,
    "warning": PALETTE.warning,
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


def css_variables() -> str:
    """`--name:value;` declarations for a web `:root { ... }` block."""
    return "".join(f"--{name}:{value};" for name, value in _WEB_TOKENS.items())


def spacing_value(value: str | int | None = "default") -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return {
        "none": SPACING.none,
        "tight": SPACING.tight,
        "control": SPACING.control,
        "default": SPACING.default,
        "group": SPACING.default,
        "panel": SPACING.panel,
        "window": SPACING.window,
    }.get(value, SPACING.default)
