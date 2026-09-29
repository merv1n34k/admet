"""Small JSON documents compiled into the existing step vocabulary."""

from copy import deepcopy
import ast
import json
import math
from pathlib import Path
import re


def parameter_values(document):
    declarations = document.get("parameters", {})
    overrides = document.get("parameter_values", {})
    if not isinstance(declarations, dict) or not isinstance(overrides, dict):
        raise ValueError("parameters and parameter_values must be objects")
    if len(declarations) > 100 or set(overrides) - set(declarations):
        raise ValueError("too many parameters or undeclared parameter values")
    values = {}
    for name, declaration in declarations.items():
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", name):
            raise ValueError(f"invalid parameter name: {name}")
        if (not isinstance(declaration, list) or len(declaration) != 2
                or not isinstance(declaration[0], str) or not declaration[0].strip()):
            raise ValueError(f"{name}: expected [label, numeric default]")
        number(declaration[1], name)
        values[name] = number(overrides.get(name, declaration[1]), name)
    return values


def expression(value, values):
    if not isinstance(value, str):
        return value
    if len(value) > 256:
        raise ValueError("parameter expression is too long")
    try:
        tree = ast.parse(value, mode="eval")
        if len(list(ast.walk(tree))) > 64:
            raise ValueError("parameter expression is too complex")

        def evaluate(node):
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                result = node.value
            elif isinstance(node, ast.Name) and node.id in values:
                result = values[node.id]
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                result = evaluate(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                a, b = evaluate(node.left), evaluate(node.right)
                if isinstance(node.op, ast.Add):
                    result = a + b
                elif isinstance(node.op, ast.Sub):
                    result = a - b
                elif isinstance(node.op, ast.Mult):
                    result = a * b
                else:
                    result = a / b
            else:
                raise ValueError("only declared variables, numbers, + - * / and parentheses are allowed")
            if not math.isfinite(result):
                raise ValueError("non-finite expression result")
            return result

        result = evaluate(tree.body)
        return int(result) if result == int(result) else result
    except (SyntaxError, ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid expression {value!r}: {exc}") from exc


def resolve(document):
    """Compile declared arithmetic into ordinary validated protocol steps."""
    if not isinstance(document, dict):
        raise ValueError("protocol must be a JSON object")
    values = parameter_values(document)
    result = deepcopy(document)
    result.pop("parameters", None)
    result.pop("parameter_values", None)
    if values:
        steps = result.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 1000:
            raise ValueError("steps must contain 1–1000 steps")
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError("each step must be an object")
            for key in ("sensor_setpoints", "pressure_setpoints", "trigger_params"):
                if isinstance(step.get(key), dict):
                    step[key] = {k: v if k == "message" else expression(v, values)
                                 for k, v in step[key].items()}
            for key in ("timeout_s", "repeat"):
                if key in step:
                    step[key] = expression(step[key], values)
            for key in ("name", "confirm_message"):
                if isinstance(step.get(key), str):
                    for name, value in values.items():
                        step[key] = step[key].replace("{" + name + "}", f"{value:g}")
        if isinstance(result.get("pressure_limits_mbar"), dict):
            result["pressure_limits_mbar"] = {
                k: expression(v, values) for k, v in result["pressure_limits_mbar"].items()}
    return _normalize_resolved(result)


def normalize(document):
    resolved = resolve(document)
    if "parameters" not in document and "parameter_values" not in document:
        return resolved
    result = deepcopy(document)
    result.setdefault("pressure_limits_mbar", {})
    result["parameter_values"] = parameter_values(document)
    return result


def number(value, label, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{label} must be finite and >= {minimum}")
    return float(value)


def channel_map(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must map channel indices to numbers")
    result = {}
    for key, target in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"0|[1-9][0-9]*", key):
            raise ValueError(f"{label}: invalid channel {key!r}")
        result[key] = number(target, label)
    return result


def _normalize_resolved(document):
    from admet.workflows.operations import STEP_LIST_SCHEMA, Refused, _step_from
    from admet.engines.acquisition.pipeline import expand_protocol_steps

    if not isinstance(document, dict):
        raise ValueError("protocol must be a JSON object")
    unknown = set(document) - {"name", "steps", "pressure_limits_mbar", "analysis"}
    if unknown:
        raise ValueError(f"unknown protocol fields: {', '.join(sorted(unknown))}")
    name = document.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name):
        raise ValueError("name must contain 1–80 letters, numbers, underscores or hyphens")
    steps = document.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 1000:
        raise ValueError("steps must contain 1–1000 steps")
    limits = channel_map(document.get("pressure_limits_mbar", {}), "pressure_limits_mbar")
    if any(value <= 0 for value in limits.values()):
        raise ValueError("pressure limits must be positive")
    normalized = []
    for index, original in enumerate(steps):
        if not isinstance(original, dict):
            raise ValueError(f"step {index + 1} must be an object")
        step = deepcopy(original)
        unknown = set(step) - set(STEP_LIST_SCHEMA["items"]["properties"])
        if unknown:
            raise ValueError(f"step {index + 1}: unknown fields {sorted(unknown)}")
        for key in ("name", "confirm_message", "group"):
            if key in step and not isinstance(step[key], str):
                raise ValueError(f"{key} must be text")
        for key in ("sensor_setpoints", "pressure_setpoints"):
            step[key] = channel_map(step.get(key, {}), key)
        if set(step["sensor_setpoints"]) & set(step["pressure_setpoints"]):
            raise ValueError("a channel cannot have flow and pressure control in the same step")
        for key, value in step["pressure_setpoints"].items():
            if key in limits and value >= limits[key]:
                raise ValueError("pressure target must be below its pressure limit")
        params = step.get("trigger_params", {})
        if not isinstance(params, dict):
            raise ValueError("trigger_params must be an object")
        for key, value in params.items():
            if key == "message":
                if not isinstance(value, str):
                    raise ValueError("confirmation message must be text")
            else:
                number(value, key)
                if key == "sensor_index" and (isinstance(value, bool) or not isinstance(value, int)):
                    raise ValueError("sensor_index must be an integer")
        repeat = step.get("repeat", 1)
        if type(repeat) is not int or not 1 <= repeat <= 100:
            raise ValueError("repeat must be an integer from 1 to 100")
        trigger = step.get("trigger_type")
        if trigger == "confirmation":
            raise ValueError("use confirm_message before a step; confirmation triggers are unsupported")
        if trigger != "time" and "timeout_s" not in step:
            raise ValueError("non-time steps require timeout_s")
        if "timeout_s" in step:
            if number(step["timeout_s"], "timeout_s") <= 0:
                raise ValueError("timeout_s must be positive")
        step.setdefault("on_complete", "zero")
        try:
            _step_from(step, index)
        except Refused as exc:
            raise ValueError(str(exc)) from exc
        normalized.append(step)
    if len(expand_protocol_steps([_step_from(step, i) for i, step in enumerate(normalized)])) > 1000:
        raise ValueError("expanded protocol exceeds 1000 steps")
    result = {"name": name, "pressure_limits_mbar": limits, "steps": normalized}
    if "analysis" in document:
        from admet.workflows.oil_density import normalize_analysis

        result["analysis"] = normalize_analysis(document["analysis"], normalized)
    return result


def loads(text):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return normalize(json.loads(text, object_pairs_hook=pairs))


def load(path):
    return loads(Path(path).read_text(encoding="utf-8"))


def template_documents():
    from importlib.resources import files

    directory = files("admet.workflows").joinpath("templates")
    documents = [loads(path.read_text(encoding="utf-8")) for path in directory.iterdir()
                 if path.name.endswith(".json")]
    return {document["name"]: document for document in documents}


def validate_channels(document, channels):
    by_index = {str(channel["index"]): channel["detected"] for channel in channels}
    if not by_index:
        return
    for key, limit in document["pressure_limits_mbar"].items():
        detected = by_index.get(key)
        if detected is None:
            raise ValueError(f"channel {key} is not connected")
        maximum = detected.get("pressure_max_mbar")
        if maximum is None or not math.isfinite(maximum) or limit > maximum:
            raise ValueError(f"channel {key}: pressure limit exceeds or lacks detected maximum")
    for step in document["steps"]:
        for targets, range_key, label in (
            ("sensor_setpoints", "sensor_max_ul_min", "flow"),
            ("pressure_setpoints", "pressure_max_mbar", "pressure"),
        ):
            for key, target in step[targets].items():
                if key not in by_index:
                    raise ValueError(f"channel {key} is not connected")
                maximum = by_index[key].get(range_key)
                if maximum is None or not math.isfinite(maximum) or target > maximum:
                    raise ValueError(f"channel {key}: {label} target exceeds or lacks detected range")
        sensor = step.get("trigger_params", {}).get("sensor_index")
        if sensor is not None and str(sensor) not in by_index:
            raise ValueError(f"trigger sensor {sensor} is not connected")
