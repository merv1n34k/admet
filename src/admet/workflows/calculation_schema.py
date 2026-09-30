"""Explicit JSON calculation schemas; never execute expressions as Python."""

from copy import deepcopy
from importlib import import_module


SCHEMAS = {
    "oil_density": ("admet.workflows.oil_density", "normalize_analysis"),
    "flow_scout": ("admet.workflows.flow_scout", "normalize_analysis"),
    "recording_summary": (None, None),
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
            normalized = normalize(entry, steps)
        result.append(normalized)
    return result
