"""Turning the workflow layer into tools a model can call.

An operation already declares its parameters and what has to be true before it
runs. That declaration is the tool: there is no second description to write, and
none to fall out of step with the code.

Engine actions are not offered here. They are primitives with no guards, and the
whole point of an operation is the guard.
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


def _schema(params: tuple[Param, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {param.name: describe_param(param) for param in params},
        "required": [],
        "additionalProperties": False,
    }


def operation_tools() -> list[dict[str, Any]]:
    from admet.workflows.operations import OPERATIONS

    tools = []
    for op in OPERATIONS:
        needs = f" Needs: {', '.join(op.requires)}." if op.requires else ""
        waits = " Returns once started; the protocol runs on." if op.starts_protocol else ""
        tools.append(
            {
                "name": op.id,
                "description": f"{op.description}{needs}{waits}",
                "inputSchema": _schema(op.params),
            }
        )
    return tools


def pipeline_tools() -> list[dict[str, Any]]:
    """One tool per pipeline, plus the plan that shows what it would do."""
    from admet.workflows.pipelines import PIPELINES

    tools = []
    for line in PIPELINES:
        tools.append(
            {
                "name": f"run_{line.id}",
                "description": f"{line.description} Runs its stages in order, stopping if one is "
                "refused or needs the operator.",
                "inputSchema": _schema(line.params),
            }
        )
        tools.append(
            {
                "name": f"plan_{line.id}",
                "description": f"The stages of: {line.description} Does not run anything.",
                "inputSchema": _schema(line.params),
            }
        )
    return tools
