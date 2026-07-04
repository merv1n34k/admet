from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PanelSpec:
    key: str
    title: str
    object_name: str = "Panel"


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


def panel_specs(*, channel_manager: bool = False) -> tuple[PanelSpec, ...]:
    return CONTROL_PANELS if channel_manager else CORE_PANELS


def structure_signature(*parts: Any) -> tuple[Any, ...]:
    return tuple(parts)


def structure_changed(current: tuple[Any, ...] | None, next_signature: tuple[Any, ...]) -> bool:
    return current != next_signature
