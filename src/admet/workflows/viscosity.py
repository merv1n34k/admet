"""Same-path hydraulic resistance and reference-based viscosity estimates."""

from copy import deepcopy
import math
from statistics import mean, stdev

from admet.workflows.calculation_schema import measurement_binding, sample_steps
from admet.workflows.calculation_inputs import step_window, trace
from admet.workflows.gravimetry import measurement, calibration_context, linear_fit
from admet.workflows.dead_volume import flow_multiplier


def normalize_calculation(entry, steps, fields):
    if set(entry) != {"type", "channel", "liquid", "path_id", "flow_multiplier", "known_viscosity", "samples"}:
        raise ValueError("viscosity requires type, channel, liquid, path_id, flow_multiplier, known_viscosity and samples")
    for key in ("liquid", "path_id"):
        if not isinstance(entry[key], str) or not entry[key].strip():
            raise ValueError(f"viscosity requires a nonempty {key}")
    expanded = sample_steps(entry, steps, mode="either")
    measurement_binding(fields, entry["flow_multiplier"], {"1"})
    measurement_binding(fields, entry["known_viscosity"], {"mPa.s"})
    passes = {1: [], 2: []}
    modes = set()
    for sample in entry["samples"]:
        if (set(sample) != {"step", "pass", "settle_s"} or type(sample["pass"]) is not int
                or sample["pass"] not in passes):
            raise ValueError("viscosity sample requires step, pass (1 or 2), settle_s")
        settle = sample["settle_s"]
        step = expanded[sample["step"] - 1]
        if (type(settle) not in (int, float) or not math.isfinite(settle) or settle < 0
                or step.trigger_params["duration_s"] - settle < 5):
            raise ValueError("viscosity requires nonnegative settling and at least 5 seconds averaging")
        modes.add("flow" if step.sensor_setpoints else "pressure")
        passes[sample["pass"]].append((step.sensor_setpoints or step.pressure_setpoints)[entry["channel"]])
    if (len(modes) != 1 or len(passes[1]) < 3 or passes[2] != list(reversed(passes[1]))
            or any(b <= a for a, b in zip(passes[1], passes[1][1:]))
            or [s["step"] for s in entry["samples"]] != sorted(s["step"] for s in entry["samples"])
            or [s["pass"] for s in entry["samples"]] != sorted(s["pass"] for s in entry["samples"])):
        raise ValueError("viscosity requires one control mode, ordered increasing levels and their reverse pass")
    return deepcopy(entry)


def check(context):
    flow_multiplier(context)
    key = context["config"]["known_viscosity"]
    if context["measurements"]["values"].get(key) is not None and measurement(context, key) <= 0:
        raise ValueError("Known reference viscosity must be positive or left blank")
    reference = context["references"].get("reference")
    if reference:
        result = reference["result"]
        if result.get("path_id") != context["config"]["path_id"] or result.get("channel") != context["config"]["channel"]:
            raise ValueError("Viscosity reference must use the same identified path and channel")
        if result.get("resistance_mbar_min_ul") is None or result["resistance_mbar_min_ul"] <= 0:
            raise ValueError("Viscosity reference has no usable hydraulic resistance")


def fit(points):
    result = linear_fit([p["flow_ul_min"] for p in points], [p["pressure_mbar"] for p in points])
    return {"resistance_mbar_min_ul": result["slope"], "intercept_mbar": result["intercept"],
            "r_squared": result["r_squared"], "slope_standard_error": result["slope_standard_error"]}


def calculate(context):
    check(context)
    config = context["config"]
    factor = flow_multiplier(context)
    points, passes, issues = [], [], []
    for sample in config["samples"]:
        point = {"step": sample["step"], "pass": sample["pass"], "flow_ul_min": None,
                 "pressure_mbar": None, "issues": []}
        try:
            window = step_window(context, sample["step"], sample["settle_s"])
            rows = trace(context, config["channel"], *window, pressure=True)[1:-1]
            raw = [r[1] for r in rows]
            if window[1] - window[0] < 5 or len(rows) < 10 or min(raw) <= 0:
                raise ValueError("Insufficient settled positive-flow samples")
            point_factor = flow_multiplier(context, mean(raw))
            q, p = [v * point_factor for v in raw], [r[2] for r in rows]
            cv = stdev(q) / mean(q)
            quarter = max(2, len(q) // 4)
            drift = abs(mean(q[:quarter]) - mean(q[-quarter:])) / mean(q)
            pressure_drift = abs(mean(p[:quarter]) - mean(p[-quarter:])) / max(abs(mean(p)), 1)
            point.update(flow_ul_min=mean(q), pressure_mbar=mean(p), flow_cv=cv, flow_multiplier=point_factor,
                         flow_drift_fraction=drift, pressure_drift_fraction=pressure_drift, samples=len(rows))
            if max(cv, drift, pressure_drift) > 0.05:
                raise ValueError("Settled flow CV or pressure/flow drift exceeds 5%")
        except ValueError as exc:
            point["issues"].append(str(exc))
            issues.append(f"Step {sample['step']}: {exc}")
        points.append(point)
    for number in (1, 2):
        selected = [p for p in points if p["pass"] == number]
        try:
            if any(p["issues"] for p in selected):
                raise ValueError("Unusable points; no selective removal from the fit")
            result = fit(selected)
            if result["resistance_mbar_min_ul"] <= 0 or result["r_squared"] < 0.95:
                raise ValueError("Positive resistance and R² >= 0.95 required")
            passes.append({"pass": number, **result})
        except ValueError as exc:
            issues.append(f"Pass {number}: {exc}")
    resistance = mean(p["resistance_mbar_min_ul"] for p in passes) if len(passes) == 2 else None
    disagreement = (abs(passes[0]["resistance_mbar_min_ul"] - passes[1]["resistance_mbar_min_ul"]) / resistance
                    if resistance else None)
    if disagreement is not None and disagreement > 0.1:
        issues.append("Forward/reverse resistance disagreement exceeds 10%")
    if issues:
        resistance = None
    reference = context["references"].get("reference", {}).get("result", {})
    ratio = resistance / reference["resistance_mbar_min_ul"] if resistance and reference else None
    known = context["measurements"]["values"].get(config["known_viscosity"])
    ref_viscosity = reference.get("known_viscosity_mpa_s")
    absolute = ratio * ref_viscosity if ratio and ref_viscosity else None
    return {"status": "usable" if not issues else "inconclusive", "channel": config["channel"],
            "liquid": config["liquid"], "path_id": config["path_id"], "flow_multiplier": factor,
            "resistance_mbar_min_ul": resistance, "relative_viscosity": ratio,
            "viscosity_mpa_s": absolute, "known_viscosity_mpa_s": known,
            "samples": points, "passes": passes, "pass_disagreement_fraction": disagreement,
            "uncertainty": None, "issues": issues, "correction_settings": calibration_context(context),
            "thresholds": {"r_squared_min": 0.95, "stability_fraction_max": 0.05, "pass_difference_max": 0.1},
            "note": "P = P0 + RQ fits calibrated measured flow, excluding settling. Viscosity ratios require "
                    "unchanged geometry and comparable temperature with Newtonian laminar flow. Absolute viscosity "
                    "requires a selected reference with known viscosity. Fit errors exclude calibration, geometry "
                    "and temperature uncertainty. No hardware calibration is changed."}
