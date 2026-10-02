"""Gravimetric correction relative to the flow already recorded by the SDK."""

from copy import deepcopy
import math
from statistics import mean, stdev

from admet.workflows.calculation_schema import measurement_binding, sample_steps, three_flow_passes
from admet.workflows.calculation_inputs import step_window, trace, volume_ul


def normalize_calculation(entry, steps, fields):
    if set(entry) != {"type", "channel", "liquid", "density", "samples"}:
        raise ValueError("gravimetry requires type, channel, liquid, density and samples")
    if not isinstance(entry["liquid"], str) or not entry["liquid"].strip():
        raise ValueError("liquid must identify the measured oil")
    expanded = sample_steps(entry, steps, mode="flow", triggers=("time", "volume"))
    measurement_binding(fields, entry["density"], {"g/mL"})
    for sample in entry["samples"]:
        if set(sample) not in ({"step", "before", "after"}, {"step", "pass", "before", "after"}):
            raise ValueError("gravimetry sample requires step, before, after and optional pass")
        for key in ("before", "after"):
            measurement_binding(fields, sample[key], {"mg", "g"}, sample["step"])
        if sample["before"] == sample["after"]:
            raise ValueError("before and after masses must be distinct measurements")
    if any("pass" in sample for sample in entry["samples"]):
        three_flow_passes(entry, expanded)
    return deepcopy(entry)


def measurement(context, key):
    value = context["measurements"]["values"].get(key)
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError(f"Missing measurement: {key}")
    return value


def density(context):
    reference = context["references"].get("density")
    if reference and reference["result"].get("oil_id") != context["config"]["liquid"]:
        raise ValueError("Density reference must identify the same oil")
    value = reference["result"].get("density_g_ml") if reference else measurement(context, context["config"]["density"])
    if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
        raise ValueError("A positive oil density or usable density reference is required")
    return value


def check(context):
    if context["config"].get("type") != "gravimetry":
        raise ValueError("Gravimetry is not declared for this run")
    density(context)
    for sample in context["config"]["samples"]:
        for key in ("before", "after"):
            measurement(context, sample[key])


def repeat_statistics(values):
    return {"mean": mean(values) if values else None,
            "sd": stdev(values) if len(values) > 1 else None,
            "sem": stdev(values) / math.sqrt(len(values)) if len(values) > 1 else None,
            "repeats": len(values)}


def calibration_context(context):
    return deepcopy(context["summary"].get("rig_fingerprint", {}).get("correction_settings"))


def calibration_identity(context):
    from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNELS

    channel = context["config"]["channel"]
    rig = context["summary"].get("rig_fingerprint", {})
    mapping = rig.get("channel_mapping", [])
    if channel >= len(mapping) or channel >= len(FLUIDIC_CHANNELS) or not mapping[channel]:
        return None
    detected = mapping[channel]
    keys = ("sensor_index", "sensor_device_sn", "sensor_type", "controller_sn")
    if any(detected.get(key) is None for key in keys):
        return None
    prefix = FLUIDIC_CHANNELS[channel][0]
    settings = calibration_context(context) or {}
    corrections = {key: settings.get(f"{prefix}_{key}") for key in ("calibration", "scale", "offset", "quadratic")}
    if any(value is None for value in corrections.values()):
        return None
    return {"sensor": {key: detected[key] for key in keys}, "corrections": corrections,
            "simulated": rig.get("simulated")}


def linear_fit(x, y):
    if len(x) != len(y) or len(x) < 3:
        raise ValueError("At least three independent points are required for a fit")
    mx, my = mean(x), mean(y)
    xx, yy = sum((v - mx) ** 2 for v in x), sum((v - my) ** 2 for v in y)
    if xx <= 0 or yy <= 0:
        raise ValueError("A fit requires distinct measured values")
    slope = sum((a - mx) * (b - my) for a, b in zip(x, y)) / xx
    intercept = my - slope * mx
    residual = sum((b - intercept - slope * a) ** 2 for a, b in zip(x, y))
    return {"slope": slope, "intercept": intercept, "r_squared": 1 - residual / yy,
            "slope_standard_error": math.sqrt(residual / (len(x) - 2) / xx)}


def flow_curve(rows, issues):
    groups = []
    for target in sorted({row["target_ul_min"] for row in rows}):
        samples = [row for row in rows if row["target_ul_min"] == target and not row["issues"]]
        stats = repeat_statistics([row["true_flow_ul_min"] for row in samples])
        if len(samples) != 3:
            issues.append(f"{target:g} µL/min: three valid independent collections required")
        cv = stats["sd"] / stats["mean"] if stats["sd"] is not None and stats["mean"] > 0 else None
        if cv is not None and cv > 0.05:
            issues.append(f"{target:g} µL/min: true-flow repeat CV exceeds 5%")
        up = [row["multiplier"] for row in samples if row["pass"] != 2]
        down = [row["multiplier"] for row in samples if row["pass"] == 2]
        hysteresis = (100 * (mean(up) - mean(down)) / mean(up) if up and down else None)
        groups.append({"target_ul_min": target, "true_flow": stats, "repeat_cv": cv,
                       "recorded_flow_ul_min": mean(row["recorded_flow_ul_min"] for row in samples) if samples else None,
                       "multiplier": mean(row["multiplier"] for row in samples) if samples else None,
                       "true_flow_repeat_ci95_ul_min": [stats["mean"] - 4.302653 * stats["sem"],
                                                         stats["mean"] + 4.302653 * stats["sem"]]
                       if len(samples) == 3 else None,
                       "up_down_difference_percent": hysteresis})
    fit = None
    if not issues:
        try:
            fit = linear_fit([row["recorded_flow_ul_min"] for row in rows], [row["true_flow_ul_min"] for row in rows])
            if fit["slope"] <= 0 or fit["r_squared"] < 0.95:
                issues.append("Calibration curve requires positive slope and R² >= 0.95")
        except ValueError as exc:
            issues.append(str(exc))
    return {"targets": groups, "flow_fit": fit,
            "calibration_curve": [{"recorded_flow_ul_min": group["recorded_flow_ul_min"],
                                    "true_flow_ul_min": group["true_flow"]["mean"]} for group in groups]
            if not issues else None}


def calculate(context):
    check(context)
    rho = density(context)
    config = context["config"]
    rows, issues, factors = [], [], []
    for sample in config["samples"]:
        step = context["summary"]["steps"][sample["step"] - 1]
        result = {"step": sample["step"], "pass": sample.get("pass"),
                  "target_ul_min": step["flow_setpoints_ul_min"][str(config["channel"])],
                  "true_flow_ul_min": None, "recorded_flow_ul_min": None,
                  "true_volume_ul": None, "recorded_volume_ul": None,
                  "multiplier": None, "issues": []}
        try:
            masses = [measurement(context, sample[key]) *
                      (1000 if context["measurements"]["fields"][sample[key]]["unit"] == "g" else 1)
                      for key in ("before", "after")]
            true_volume = (masses[1] - masses[0]) / rho
            window = step_window(context, sample["step"])
            recorded = volume_ul(trace(context, config["channel"], *window))
            if not all(math.isfinite(v) and v > 0 for v in (true_volume, recorded)):
                raise ValueError("Collection mass and recorded volume must be positive")
            factor = true_volume / recorded
            factors.append(factor)
            result.update(true_volume_ul=true_volume, recorded_volume_ul=recorded, multiplier=factor,
                          window_elapsed_s=window, true_flow_ul_min=true_volume * 60 / (window[1] - window[0]),
                          recorded_flow_ul_min=recorded * 60 / (window[1] - window[0]))
            if abs(result["recorded_flow_ul_min"] - result["target_ul_min"]) > 0.2 * result["target_ul_min"]:
                raise ValueError("Recorded mean flow differs from target by more than 20%; investigate capacity/settling")
        except ValueError as exc:
            result["issues"].append(str(exc))
            issues.append(f"Step {sample['step']}: {exc}")
        rows.append(result)
    stats = repeat_statistics(factors)
    curve = flow_curve(rows, issues) if any("pass" in s for s in config["samples"]) else {}
    if not curve and stats["sd"] is not None and stats["sd"] > 0.1 * stats["mean"]:
        issues.append("Correction repeat CV exceeds 10%; investigate collection consistency")
    scalar_ok = not curve or (factors and max(factors) - min(factors) <= 0.05 * stats["mean"])
    return {"status": "usable" if len(factors) >= 2 and not issues else "inconclusive",
            "channel": config["channel"], "liquid": config["liquid"], "density_g_ml": rho, "samples": rows,
            "multiplier": stats["mean"] if len(factors) >= 2 and not issues and scalar_ok else None,
            **curve,
            **({"repeat_statistics": stats} if not curve else {}), "uncertainty": None, "issues": issues,
            "thresholds": {"repeat_cv_max": 0.05 if curve else 0.1, "minimum_repeats": 3 if curve else 2,
                           "flow_fit_r_squared_min": 0.95 if curve else None},
            "correction_settings": calibration_context(context),
            "calibration_identity": calibration_identity(context),
            "note": "Multiplier applies to recorded flow, not raw sensor readings. No hardware changes. "
                    "Before/after weights characterize complete dispenses, including startup. R² describes the "
                    "recorded-versus-true flow curve, not mass versus time. Per-target 95% intervals use three "
                    "independent normally distributed collections, not sensor sample count. Direction differences "
                    "are exploratory (two ascending passes, one descending). Density, balance, evaporation and "
                    "retained droplets add uncertainty. A flow-dependent correction must not be averaged away."}
