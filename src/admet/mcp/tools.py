"""Turning the workflow layer into tools a model can call.

An operation already declares its parameters and what has to be true before it
runs. That declaration is the tool: there is no second description to write, and
none to fall out of step with the code.

Guarded operations only. An engine action has no guards -- that is what it is
for -- and the Python binding is where it stays reachable.

Each tool's description opens with its target and what calling it does: [read]
answers a question, [write] has finished having its effect when it returns, and
[start] leaves something running after it returns.
"""

from __future__ import annotations

from typing import Any

from admet.core.engine import Param, ParamKind

_JSON_TYPES = {
    ParamKind.BOOLEAN: "boolean",
    ParamKind.INTEGER: "integer",
    ParamKind.FLOAT: "number",
    ParamKind.TEXT: "string",
    ParamKind.PATH: "string",
    ParamKind.CHOICE: "string",
}


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


def _schema(params: tuple[Param, ...], raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """The tool's input schema, from what the operation declares.

    Most settings are Params. A few are too structured for one -- a step list --
    and declare their JSON schema directly; both end up in the same properties.
    """
    return {
        "type": "object",
        "properties": {
            **{param.name: describe_param(param) for param in params},
            **(raw or {}),
        },
        "required": sorted(raw or {}),
        "additionalProperties": False,
    }


DESCRIBE_TOOL = {
    "name": "describe",
    "description": (
        "What exists and how the layers fit together. With no target: every operation "
        "that is the way in, and the engines underneath. With one: that operation's "
        "parameters and what must be true before it runs, or that engine's actions and "
        "which operations drive them."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "An operation, pipeline or engine id; omit for all three layers",
            }
        },
        "required": [],
        "additionalProperties": False,
    },
}


def operation_tools() -> list[dict[str, Any]]:
    from admet.workflows.operations import OPERATIONS

    tools = []
    for op in OPERATIONS:
        needs = f" Needs: {', '.join(op.requires)}." if op.requires else ""
        belongs = f"[{op.target}]"
        waits = " Returns once started; the protocol runs on." if op.starts_protocol else ""
        tools.append(
            {
                "name": op.id,
                "description": f"{belongs}[{op.kind}] {op.description}{needs}{waits}",
                "inputSchema": _schema(op.params, op.raw),
            }
        )
    return tools


