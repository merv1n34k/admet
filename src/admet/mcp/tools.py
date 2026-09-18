"""Turning the workflow layer into tools a model can call.

An operation already declares its parameters and what has to be true before it
runs. That declaration is the tool: there is no second description to write, and
none to fall out of step with the code.

Three levels are offered, and the name says which:

    <engine>_<action>   low   -- one call to a device, no guards
    <operation>         med   -- the same call with the conditions under which
                                 using it is not a mistake
    pipeline_<id>       high  -- several operations in order

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
        "What exists and how the layers fit together. With no target: the operations "
        "and pipelines that are the way in, and the engines underneath. With one: that "
        "operation's parameters and what must be true before it runs, that pipeline's "
        "stages, or that engine's actions and which operations drive them."
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


def pipeline_tools() -> list[dict[str, Any]]:
    """One tool per pipeline, plus the plan that shows what it would do."""
    from admet.workflows.pipelines import PIPELINES

    tools = []
    for line in PIPELINES:
        tools.append(
            {
                "name": f"pipeline_{line.id}",
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


def action_tools(admet: Any) -> list[dict[str, Any]]:
    """One typed tool per engine action: the low level, in full.

    These are the device's own vocabulary -- every camera setting, every channel
    knob -- with no guards on them. An operation is the guarded way to do the
    same thing; this is the way to configure what no operation covers.
    """
    from admet.workflows.operations import OPERATIONS

    driven: dict[str, list[str]] = {}
    for op in OPERATIONS:
        for action in op.uses:
            driven.setdefault(action, []).append(op.id)

    tools = []
    for engine_id in admet.engine_ids():
        engine = admet.engine(engine_id)
        by_name = {param.name: param for param in engine.settings.params}
        for action in engine.actions:
            guarded = driven.get(action.id)
            instead = (
                f" Unguarded; the operation {guarded[0]} does this with its guards."
                if guarded
                else ""
            )
            params = tuple(by_name[name] for name in action.params if name in by_name)
            tools.append(
                {
                    "name": f"{engine_id}_{action.id}",
                    "description": (
                        f"[{engine_id}][{action.kind}] {action.description or action.label}."
                        f"{instead}"
                    ),
                    "inputSchema": _schema(params),
                }
            )
    return tools

