"""Gravimetric correction relative to the flow already recorded by the SDK."""

from copy import deepcopy
import math
from statistics import mean, stdev

from admet.workflows.calculation_schema import measurement_binding, sample_steps
from admet.workflows.calculation_inputs import step_window, trace, volume_ul


def normalize_calculation(entry, steps, fields):
    if set(entry) != {"type", "channel", "liquid", "density", "samples"}:
        raise ValueError("gravimetry requires type, channel, liquid, density and samples")
    if not isinstance(entry["liquid"], str) or not entry["liquid"].strip():
        raise ValueError("liquid must identify the measured oil")
    sample_steps(entry, steps, mode="flow")
    measurement_binding(fields, entry["density"], {"g/mL"})
    for sample in entry["samples"]:
        if set(sample) != {"step", "before", "after"}:
            raise ValueError("gravimetry sample requires step, before and after")
        for key in ("before", "after"):
            measurement_binding(fields, sample[key], {"mg", "g"}, sample["step"])
        if sample["before"] == sample["after"]:
            raise ValueError("before and after masses must be distinct measurements")
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


def calculate(context):
    check(context)
    rho = density(context)
    config = context["config"]
    rows, issues, factors = [], [], []
    for sample in config["samples"]:
        result = {"step": sample["step"], "true_volume_ul": None, "recorded_volume_ul": None,
                  "multiplier": None, "issues": []}
        try:
            masses = [measurement(context, sample[key]) *
                      (1000 if context["measurements"]["fields"][sample[key]]["unit"] == "g" else 1)
                      for key in ("before", "after")]
            true_volume = (masses[1] - masses[0]) / rho
            window = step_window(context, sample["step"])
            recorded = volume_ul(trace(context, config["channel"], *window))
            if true_volume <= 0 or recorded <= 0:
                raise ValueError("Collection mass and recorded volume must be positive")
            factor = true_volume / recorded
            factors.append(factor)
            result.update(true_volume_ul=true_volume, recorded_volume_ul=recorded, multiplier=factor,
                          window_elapsed_s=window)
        except ValueError as exc:
            result["issues"].append(str(exc))
            issues.append(f"Step {sample['step']}: {exc}")
        rows.append(result)
    stats = repeat_statistics(factors)
    if stats["sd"] is not None and stats["sd"] > 0.1 * stats["mean"]:
        issues.append("Correction repeat CV exceeds 10%; investigate collection consistency")
    return {"status": "usable" if len(factors) >= 2 and not issues else "inconclusive",
            "channel": config["channel"], "liquid": config["liquid"], "density_g_ml": rho, "samples": rows,
            "multiplier": stats["mean"] if len(factors) >= 2 and not issues else None,
            "repeat_statistics": stats, "uncertainty": None, "issues": issues,
            "thresholds": {"repeat_cv_max": 0.1, "minimum_repeats": 2},
            "correction_settings": calibration_context(context),
            "calibration_identity": calibration_identity(context),
            "note": "Multiplier applies to recorded flow, not raw sensor readings. No hardware changes. "
                    "SEM describes repeatability only; density, balance, timing and retained droplets add uncertainty."}
