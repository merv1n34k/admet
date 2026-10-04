"""Small JSON documents compiled into the existing step vocabulary."""

from copy import deepcopy
import ast
import json
import math
from pathlib import Path
import re

from admet.engines.acquisition.triggers import VOLUME_MODES
from admet.workflows.calculation_schema import normalize_calculations, resolve_declarations


def parameter_declarations(document):
    declarations = document.get("parameters", {})
    if not isinstance(declarations, dict) or len(declarations) > 100:
        raise ValueError("parameters must be an object with at most 100 declarations")
    result = {}
    for name, declaration in declarations.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", name):
            raise ValueError(f"invalid parameter name: {name}")
        if isinstance(declaration, list) and len(declaration) == 2:
            declaration = {"type": "number", "label": declaration[0], "default": declaration[1], "min": 0}
        if not isinstance(declaration, dict):
            raise ValueError(f"{name}: expected a typed declaration or [label, numeric default]")
        declaration = deepcopy(declaration)
        kind = declaration.get("type")
        if kind not in ("text", "number", "boolean", "choice"):
            raise ValueError(f"{name}: type must be text, number, boolean or choice")
        allowed = {"type", "label", "default"} | ({"min", "max"} if kind == "number" else
                                                  {"options"} if kind == "choice" else set())
        if (set(declaration) - allowed or not {"label", "default"} <= set(declaration)
                or not isinstance(declaration["label"], str) or not declaration["label"].strip()):
            raise ValueError(f"{name}: invalid declaration fields or label")
        if kind == "number":
            for bound in ("min", "max"):
                if bound in declaration:
                    declaration[bound] = number(declaration[bound], name, minimum=-math.inf)
            if declaration.get("min", -math.inf) > declaration.get("max", math.inf):
                raise ValueError(f"{name}: min must not exceed max")
        if kind == "choice":
            options = declaration.get("options")
            if not isinstance(options, list) or not 1 <= len(options) <= 100:
                raise ValueError(f"{name}: choice needs 1–100 options")
            keys = [(_parameter_scalar_kind(value), value) for value in options]
            if len(set(keys)) != len(keys):
                raise ValueError(f"{name}: duplicate choice options")
        _parameter_value(name, declaration, declaration["default"])
        result[name] = declaration
    return result


def _parameter_scalar_kind(value):
    if isinstance(value, str) and len(value) <= 4096:
        return "text"
    if type(value) is bool:
        return "boolean"
    if type(value) in (int, float) and math.isfinite(value):
        return "number"
    raise ValueError("parameter values must be finite numbers, booleans or text up to 4096 characters")


def _parameter_value(name, declaration, value):
    kind = _parameter_scalar_kind(value)
    expected = declaration["type"]
    if expected == "choice":
        for option in declaration["options"]:
            if kind == _parameter_scalar_kind(option) and value == option:
                return option
        raise ValueError(f"{name}: value is not one of its choice options")
    elif kind != expected:
        raise ValueError(f"{name}: expected {expected}, got {kind}")
    if expected == "number" and not declaration.get("min", -math.inf) <= value <= declaration.get("max", math.inf):
        raise ValueError(f"{name}: number is outside declared bounds")
    return value


def parameter_values(document):
    declarations = parameter_declarations(document)
    overrides = document.get("parameter_values", {})
    if not isinstance(overrides, dict) or set(overrides) - set(declarations):
        raise ValueError("parameter_values must be an object containing only declared parameters")
    return {name: _parameter_value(name, declaration, overrides.get(name, declaration["default"]))
            for name, declaration in declarations.items()}


def parameter_text(value):
    if type(value) is bool:
        return "true" if value else "false"
    return f"{value:g}" if type(value) in (int, float) else value


def interpolate(value, values):
    if not isinstance(value, str):
        return value
    return re.sub(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}",
                  lambda match: parameter_text(values[match[1]]) if match[1] in values else match[0], value)


# Trigger settings that are text rather than numbers.
TEXT_TRIGGER_PARAMS = ("message", "mode")


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
                if type(result) not in (int, float):
                    raise ValueError(f"{node.id} is not numeric")
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
        result["name"] = interpolate(result.get("name"), values)
        steps = result.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 1000:
            raise ValueError("steps must contain 1–1000 steps")
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError("each step must be an object")
            for key in ("sensor_setpoints", "pressure_setpoints", "trigger_params"):
                if isinstance(step.get(key), dict):
                    compiled = {}
                    for k, v in step[key].items():
                        target_key = interpolate(k, values) if key != "trigger_params" else k
                        if target_key in compiled:
                            raise ValueError("channel parameters resolve to duplicate targets")
                        compiled[target_key] = interpolate(v, values) if k in TEXT_TRIGGER_PARAMS else expression(v, values)
                    step[key] = compiled
            for key in ("timeout_s", "repeat"):
                if key in step:
                    step[key] = expression(step[key], values)
            for key in ("name", "confirm_message", "group", "trigger_type", "on_complete"):
                if isinstance(step.get(key), str):
                    step[key] = interpolate(step[key], values)
    resolve_declarations(result, values)
    return _normalize_resolved(result)


def normalize(document):
    resolved = resolve(document)
    if "parameters" not in document and "parameter_values" not in document:
        return resolved
    result = deepcopy(document)
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


def measurement_fields(value, step_count):
    if not isinstance(value, dict) or len(value) > 1000:
        raise ValueError("measurements must be an object with at most 1000 fields")
    for key, field in value.items():
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", key):
            raise ValueError("invalid measurement key")
        if (not isinstance(field, dict) or set(field) - {"label", "step", "unit", "min", "max", "required"}
                or not isinstance(field.get("label"), str) or not field["label"].strip()):
            raise ValueError(f"measurement {key}: expected label and optional step")
        if "step" in field and (type(field["step"]) is not int or not 1 <= field["step"] <= step_count):
            raise ValueError(f"measurement {key}: step must identify an expanded step")
        if "unit" in field and field["unit"] not in {"mg", "g", "s", "uL", "g/mL", "mPa.s", "mm", "cm", "1"}:
            raise ValueError(f"measurement {key}: unsupported unit")
        if "required" in field and type(field["required"]) is not bool:
            raise ValueError(f"measurement {key}: required must be boolean")
        for bound in ("min", "max"):
            if bound in field:
                number(field[bound], f"measurement {key} {bound}", minimum=-math.inf)
        if field.get("min", -math.inf) > field.get("max", math.inf):
            raise ValueError(f"measurement {key}: min exceeds max")
    return deepcopy(value)


def validate_measurement(value, field):
    if value is None:
        return
    number(value, "measurement", minimum=-math.inf)
    if not field.get("min", -math.inf) <= value <= field.get("max", math.inf):
        raise ValueError("measurement is outside declared bounds")


def _normalize_resolved(document):
    from admet.workflows.operations import STEP_LIST_SCHEMA, Refused, _step_from
    from admet.engines.acquisition.pipeline import expand_protocol_steps

    if not isinstance(document, dict):
        raise ValueError("protocol must be a JSON object")
    unknown = set(document) - {"name", "steps", "calculations", "measurements"}
    if unknown:
        raise ValueError(f"unknown protocol fields: {', '.join(sorted(unknown))}")
    name = document.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name):
        raise ValueError("name must contain 1–80 letters, numbers, underscores or hyphens")
    steps = document.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 1000:
        raise ValueError("steps must contain 1–1000 steps")
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
        params = step.get("trigger_params", {})
        if not isinstance(params, dict):
            raise ValueError("trigger_params must be an object")
        for key, value in params.items():
            if key == "message":
                if not isinstance(value, str):
                    raise ValueError("confirmation message must be text")
            elif key == "mode":
                if value not in VOLUME_MODES:
                    raise ValueError(f"volume mode must be one of: {', '.join(VOLUME_MODES)}")
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
            if trigger == "time" and step["timeout_s"] <= number(params.get("duration_s", 0), "duration_s"):
                raise ValueError("a time step's timeout_s must be longer than its duration_s")
        step.setdefault("on_complete", "zero")
        try:
            _step_from(step, index)
        except Refused as exc:
            raise ValueError(str(exc)) from exc
        normalized.append(step)
    expanded = expand_protocol_steps([_step_from(step, i) for i, step in enumerate(normalized)])
    if len(expanded) > 1000:
        raise ValueError("expanded protocol exceeds 1000 steps")
    result = {"name": name, "steps": normalized}
    if "measurements" in document:
        result["measurements"] = measurement_fields(document["measurements"], len(expanded))
    calculations = normalize_calculations(document, normalized, result.get("measurements", {}))
    if "calculations" in document:
        result["calculations"] = calculations
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

    directory = files("admet").joinpath("templates")
    if not directory.is_dir():
        directory = Path(__file__).resolve().parents[3] / "templates"
    documents = [loads(path.read_text(encoding="utf-8")) for path in directory.iterdir()
                 if path.name.endswith(".json")]
    return {document["name"]: document for document in documents}


def validate_channels(document, channels):
    by_index = {str(channel["index"]): channel["detected"] for channel in channels}
    if not by_index:
        return
    for step_number, step in enumerate(document["steps"], 1):
        for targets, range_key, label in (
            ("sensor_setpoints", "sensor_max_ul_min", "flow"),
            ("pressure_setpoints", "pressure_max_mbar", "pressure"),
        ):
            for key, target in step[targets].items():
                if key not in by_index:
                    raise ValueError(f"channel {key} is not connected")
                maximum = by_index[key].get(range_key)
                unit = "µL/min" if label == "flow" else "mbar"
                context = f"Step {step_number} ({step.get('name', '')}), channel {key}: {label} target {target:g} {unit}"
                if maximum is None or not math.isfinite(maximum):
                    raise ValueError(f"{context}; detected range unavailable. Reconnect/apply corrections to refresh it.")
                if target > maximum:
                    raise ValueError(f"{context} exceeds detected range maximum {maximum:g} {unit}. "
                                     "Check all parameter multipliers, not only the base value.")
        sensor = step.get("trigger_params", {}).get("sensor_index")
        if sensor is not None and str(sensor) not in by_index:
            raise ValueError(f"trigger sensor {sensor} is not connected")
