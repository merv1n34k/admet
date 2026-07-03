from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from admet.core.schema import ParamSchema


@dataclass(frozen=True)
class ActionSpec:
    id: str
    label: str
    category: str
    params: tuple[str, ...] = ()
    destructive: bool = False
    description: str = ""


def action_spec(actions: Iterable[ActionSpec], action_id: str) -> ActionSpec:
    for action in actions:
        if action.id == action_id:
            return action
    raise ValueError(f"unsupported action: {action_id}")


def validate_action_settings(
    schema: ParamSchema,
    actions: Iterable[ActionSpec],
    action_id: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    action = action_spec(actions, action_id)
    by_name = {param.name: param for param in schema.params}

    unknown_settings = set(settings) - set(by_name)
    if unknown_settings:
        raise KeyError(f"unknown settings: {sorted(unknown_settings)!r}")

    undeclared_settings = set(settings) - set(action.params)
    if undeclared_settings:
        raise KeyError(
            f"settings not declared by action {action.id!r}: {sorted(undeclared_settings)!r}"
        )

    missing_params = set(action.params) - set(by_name)
    if missing_params:
        raise KeyError(f"action {action.id!r} references unknown settings: {sorted(missing_params)!r}")

    defaults = schema.defaults()
    return {
        name: by_name[name].validate(settings.get(name, defaults[name]))
        for name in action.params
    }
