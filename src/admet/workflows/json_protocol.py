"""Small JSON documents compiled into the existing step vocabulary."""

from copy import deepcopy
import json
import math
from pathlib import Path
import re


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


def normalize(document):
    from admet.workflows.operations import STEP_LIST_SCHEMA, Refused, _step_from
    from admet.engines.acquisition.pipeline import expand_protocol_steps

    if not isinstance(document, dict):
        raise ValueError("protocol must be a JSON object")
    unknown = set(document) - {"name", "steps", "pressure_limits_mbar"}
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
        active = set(step["sensor_setpoints"]) | set(step["pressure_setpoints"])
        if active - set(limits):
            raise ValueError("each controlled channel requires a pressure limit")
        for key, value in step["pressure_setpoints"].items():
            if value >= limits[key]:
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
    return {"name": name, "pressure_limits_mbar": limits, "steps": normalized}


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
        if maximum is None or limit >= maximum:
            raise ValueError(f"channel {key}: pressure limit must be below detected maximum")
    for step in document["steps"]:
        for key, flow in step["sensor_setpoints"].items():
            maximum = by_index[key].get("sensor_max_ul_min")
            if maximum is None or flow > maximum:
                raise ValueError(f"channel {key}: flow target exceeds detected range")
        sensor = step.get("trigger_params", {}).get("sensor_index")
        if sensor is not None and str(sensor) not in by_index:
            raise ValueError(f"trigger sensor {sensor} is not connected")
