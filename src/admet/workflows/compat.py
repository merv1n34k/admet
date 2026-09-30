"""Read historical density protocols without changing their saved definitions."""

import re


def density_analysis(document):
    if "analysis" in document:
        return document["analysis"]
    # The protocol-only density templates store geometry in their explicit step labels.
    # Never infer heights from step order or quietly assume an arbitrary run is density.
    points = []
    pattern = r"(Scout|Pass ([12])) ([0-9]+(?:\.[0-9]+)?) cm / ([0-9]+(?:\.[0-9]+)?) uL-min"
    for index, step in enumerate(document["steps"], 1):
        match = re.fullmatch(pattern, step.get("name", ""))
        if match is None:
            if any(step.get("sensor_setpoints", {}).values()):
                raise ValueError("This protocol has no recognized density height/pass labels")
            continue
        if step.get("sensor_setpoints") != {"1": float(match[4])}:
            raise ValueError(f"Step {index}: density label and M1 flow target disagree")
        if step.get("trigger_params", {}).get("duration_s") != 20:
            raise ValueError("Protocol-only density recordings require the declared 20-second points")
        points.append({"step": index, "pass": int(match[2]) if match[2] else 0,
                       "height_cm": float(match[3]), "settle_s": 10})
    if not points:
        raise ValueError("Choose a density protocol run; heights cannot be recovered from a bare CSV")
    return {"type": "oil_density", "oil_id": document["name"], "points": points}
