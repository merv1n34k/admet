"""Explicit JSON calculation schemas; never execute expressions as Python."""

from copy import deepcopy
from importlib import import_module


SCHEMAS = {
    "oil_density": ("admet.workflows.oil_density", "normalize_analysis"),
    "flow_scout": ("admet.workflows.flow_scout", "normalize_analysis"),
    "recording_summary": (None, None),
    "gravimetry": ("admet.workflows.gravimetry", "normalize_calculation"),
    "dead_volume": ("admet.workflows.dead_volume", "normalize_calculation"),
    "viscosity": ("admet.workflows.viscosity", "normalize_calculation"),
}


def declarations(document):
    if "analysis" in document and "calculations" in document:
        raise ValueError("use calculations or legacy analysis, not both")
    return document.get("calculations", [document["analysis"]] if "analysis" in document else [])


def resolve_declarations(document, values):
    from admet.workflows.json_protocol import expression, interpolate

    entries = declarations(document)
    if not isinstance(entries, list):
        raise ValueError("calculations must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") not in SCHEMAS:
            raise ValueError("unknown calculation type")
        if "liquid" in entry:
            entry["liquid"] = interpolate(entry["liquid"], values)
        if "channel" in entry:
            entry["channel"] = expression(entry["channel"], values)
        if entry["type"] == "viscosity":
            entry["path_id"] = interpolate(entry.get("path_id"), values)
            samples = entry.get("samples")
            if not isinstance(samples, list) or any(not isinstance(s, dict) for s in samples):
                raise ValueError("viscosity samples must be a list of objects")
            for sample in samples:
                sample["settle_s"] = expression(sample.get("settle_s"), values)
        if entry["type"] == "flow_scout":
            entry["height_cm"] = expression(entry.get("height_cm"), values)
        elif entry["type"] == "oil_density":
            entry["oil_id"] = interpolate(entry.get("oil_id"), values)
            points = entry.get("points")
            if not isinstance(points, list) or any(not isinstance(p, dict) for p in points):
                raise ValueError("density analysis points must be a list of objects")
            for point in points:
                for key in ("height_cm", "settle_s"):
                    point[key] = expression(point.get(key), values)


def normalize_calculations(document, steps, fields):
    entries = declarations(document)
    if not isinstance(entries, list) or len(entries) > 20:
        raise ValueError("calculations must be a list of at most 20 entries")
    result = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") not in SCHEMAS:
            raise ValueError("unknown calculation type")
        kind = entry["type"]
        if kind in seen:
            raise ValueError("duplicate calculation type")
        seen.add(kind)
        module, name = SCHEMAS[kind]
        if module is None:
            if set(entry) != {"type"}:
                raise ValueError(f"{kind}: unknown calculation fields")
            normalized = deepcopy(entry)
        else:
            normalize = getattr(import_module(module), name)
            normalized = normalize(entry, steps) if name == "normalize_analysis" else normalize(entry, steps, fields)
        result.append(normalized)
    return result


def measurement_binding(fields, key, units, step=None):
    field = fields.get(key) if isinstance(key, str) else None
    if not field or field.get("unit") not in units:
        raise ValueError(f"{key}: declare a measurement with unit {'/'.join(units)}")
    if step is not None and field.get("step") != step:
        raise ValueError(f"{key}: measurement must belong to step {step}")


def sample_steps(entry, steps, *, mode):
    from admet.engines.acquisition.pipeline import expand_protocol_steps
    from admet.workflows.operations import _step_from

    channel = entry.get("channel")
    if type(channel) is not int or channel < 0:
        raise ValueError("calculation channel must be a non-negative integer")
    expanded = expand_protocol_steps([_step_from(step, i) for i, step in enumerate(steps)])
    samples = entry.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("calculation requires sample step associations")
    seen = set()
    for sample in samples:
        index = sample.get("step") if isinstance(sample, dict) else None
        if type(index) is not int or not 1 <= index <= len(expanded) or index in seen:
            raise ValueError("sample must identify a unique expanded step")
        seen.add(index)
        step = expanded[index - 1]
        controlled = set(step.sensor_setpoints) | set(step.pressure_setpoints)
        targets = (step.sensor_setpoints or step.pressure_setpoints) if mode == "either" else (
            step.sensor_setpoints if mode == "flow" else step.pressure_setpoints)
        if controlled != {channel} or targets.get(channel, 0) <= 0 or step.trigger_type != "time":
            raise ValueError(f"sample step {index} must use positive single-channel {mode} control and time")
        if step.on_complete != "zero":
            raise ValueError("sample steps must end with zero")
    return expanded
