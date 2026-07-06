from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from admet.workflows import Stage


@dataclass(frozen=True)
class PanelSpec:
    key: str
    title: str
    object_name: str = "Panel"


@dataclass(frozen=True)
class SettingsPanelState:
    kind: str
    source: str = ""
    stage_id: str = ""


@dataclass(frozen=True)
class LogState:
    lines: tuple[str, ...]
    empty_text: str = "No actions yet."


CORE_PANELS = (
    PanelSpec("action_box", "Action Box"),
    PanelSpec("action_panel", "Action Panel"),
    PanelSpec("main", "Main Window", "MainPanel"),
    PanelSpec("results", "Results"),
    PanelSpec("log", "Action Log"),
)

CONTROL_PANELS = (
    PanelSpec("action_box", "Action Box"),
    PanelSpec("action_panel", "Action Panel"),
    PanelSpec("main", "Main Window", "MainPanel"),
    PanelSpec("channel_manager", "Channel Manager"),
    PanelSpec("results", "Results"),
    PanelSpec("log", "Action Log"),
)


def panel_specs(*, channel_manager: bool = False, include_results: bool = True) -> tuple[PanelSpec, ...]:
    panels = CONTROL_PANELS if channel_manager else CORE_PANELS
    if include_results:
        return panels
    return tuple(panel for panel in panels if panel.key != "results")


def settings_panel_state(stage: Stage) -> SettingsPanelState:
    panel = stage.settings_panel
    if panel is None:
        return SettingsPanelState(kind="none", stage_id=stage.id)
    return SettingsPanelState(
        kind=panel.kind,
        source=str(panel.options.get("source") or ""),
        stage_id=stage.id,
    )


def log_state(entries: list[str] | tuple[str, ...], *, limit: int = 80, empty_text: str = "No actions yet.") -> LogState:
    return LogState(lines=tuple(entries[-limit:]), empty_text=empty_text)


def structure_signature(*parts: Any) -> tuple[Any, ...]:
    return tuple(parts)


def structure_changed(current: tuple[Any, ...] | None, next_signature: tuple[Any, ...]) -> bool:
    return current != next_signature
