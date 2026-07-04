from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class PanelSpec:
    key: str
    title: str
    object_name: str = "Panel"


@dataclass(frozen=True)
class RenderDecision:
    signature: tuple[Any, ...]
    mounted: bool = False
    remounted_shared: bool = False


@dataclass(frozen=True)
class WindowStageContext:
    key: str
    signature: tuple[Any, ...]
    mount: Callable[[], None]
    sync: Callable[[], None]
    stage_changed: bool = False
    finish: Callable[[RenderDecision], None] | None = None
    remount_shared: Callable[[], None] | None = None


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


class WindowWiring:
    def __init__(self) -> None:
        self._signatures: dict[str, tuple[Any, ...]] = {}

    def signature(self, key: str) -> tuple[Any, ...] | None:
        return self._signatures.get(key)

    def needs_mount(self, key: str, signature: tuple[Any, ...]) -> bool:
        return structure_changed(self.signature(key), signature)

    def render(
        self,
        key: str,
        signature: tuple[Any, ...],
        *,
        mount: Callable[[], None],
        sync: Callable[[], None],
        force_mount: bool = False,
        stage_changed: bool = False,
        remount_shared: Callable[[], None] | None = None,
    ) -> RenderDecision:
        mounted = False
        remounted_shared = False
        if force_mount or self.needs_mount(key, signature):
            mount()
            self._signatures[key] = signature
            mounted = True
        elif stage_changed and remount_shared is not None:
            remount_shared()
            remounted_shared = True
        sync()
        return RenderDecision(
            signature=signature,
            mounted=mounted,
            remounted_shared=remounted_shared,
        )


class WindowController:
    def __init__(self) -> None:
        self.wiring = WindowWiring()

    def render_current_stage(
        self,
        prepare: Callable[[], WindowStageContext | None],
        *,
        force_mount: bool = False,
    ) -> RenderDecision | None:
        context = prepare()
        if context is None:
            return None
        decision = self.wiring.render(
            context.key,
            context.signature,
            mount=context.mount,
            sync=context.sync,
            force_mount=force_mount,
            stage_changed=context.stage_changed,
            remount_shared=context.remount_shared,
        )
        if context.finish is not None:
            context.finish(decision)
        return decision

    def needs_mount(self, key: str, signature: tuple[Any, ...]) -> bool:
        return self.wiring.needs_mount(key, signature)
