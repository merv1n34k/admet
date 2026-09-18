"""Turning an engine's actions into tools a model can call.

An engine already declares what it can do and what each action takes. That
declaration is the tool list -- there is no second description to write, and none
to fall out of step with the code.
"""

from __future__ import annotations

from typing import Any

from admet.core.engine import Param, ParamKind, ParamSchema

# Actions that reach the instrument itself. In simulated mode these are the ones
# that must be proven harmless before they are allowed through.
HARDWARE_ACTIONS = frozenset(
    {
        "connect_fluidics",
        "verify_fluigent",
        "connect_camera",
        "start_camera_live",
        "apply_camera_settings",
        "start_recording",
        "apply_corrections",
        "set_channel_flow",
        "set_channel_pressure",
        "stop_channel",
        "set_channel_response",
        "run_protocol",
        "wash",
        "calibrate",
    }
)

_JSON_TYPES = {
    ParamKind.BOOLEAN: "boolean",
    ParamKind.INTEGER: "integer",
    ParamKind.FLOAT: "number",
    ParamKind.TEXT: "string",
    ParamKind.PATH: "string",
    ParamKind.CHOICE: "string",
}


def tool_name(engine_id: str, action_id: str) -> str:
    return f"{engine_id}_{action_id}"


def describe_param(param: Param) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": _JSON_TYPES.get(param.kind, "string")}
    description = param.description or param.label
    if param.kind is ParamKind.CHOICE and param.options:
        schema["enum"] = [option.value for option in param.options]
    if param.minimum is not None:
        schema["minimum"] = param.minimum
    if param.maximum is not None:
        schema["maximum"] = param.maximum
    if param.default is not None:
        schema["default"] = param.default
        description = f"{description} (default {param.default})"
    schema["description"] = description
    return schema


def tools_for(engine: Any) -> list[dict[str, Any]]:
    """One tool per action, with the parameters the action actually accepts."""
    schema: ParamSchema = engine.settings
    by_name = {param.name: param for param in schema.params}
    tools = []
    for action in engine.actions:
        properties = {
            name: describe_param(by_name[name]) for name in action.params if name in by_name
        }
        required = [
            name
            for name in action.params
            if name in by_name and by_name[name].required and by_name[name].default is None
        ]
        tools.append(
            {
                "name": tool_name(engine.id, action.id),
                "description": _describe_action(engine, action),
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            }
        )
    return tools


def _describe_action(engine: Any, action: Any) -> str:
    parts = [action.description or f"{action.label} on the {engine.name} engine."]
    if action.category:
        parts.append(f"Category: {action.category}.")
    if action.destructive:
        parts.append("Destructive.")
    if action.id in HARDWARE_ACTIONS:
        parts.append("Reaches the instrument.")
    return " ".join(parts)
