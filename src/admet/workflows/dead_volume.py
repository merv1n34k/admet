"""Effective displacement volume from manually observed marker transit."""

from copy import deepcopy
import math

from admet.workflows.calculation_schema import measurement_binding, sample_steps
from admet.workflows.calculation_inputs import step_window, trace, volume_ul
from admet.workflows.gravimetry import measurement, repeat_statistics, calibration_identity


def flow_multiplier(context, measured_flow=None):
    reference = context["references"].get("calibration")
    if reference:
        result = reference["result"]
        config = context["config"]
        if result.get("liquid") != config["liquid"] or result.get("channel") != config["channel"]:
            raise ValueError("Flow calibration must use the same liquid and channel")
        identity = calibration_identity(context)
        if not identity or result.get("calibration_identity") != identity:
            raise ValueError("Flow calibration requires matching recorded sensor identity and correction settings")
        curve = result.get("calibration_curve")
        if curve:
            points = [(point.get("recorded_flow_ul_min"), point.get("true_flow_ul_min")) for point in curve]
            if (len(points) < 3 or any(type(v) not in (float, int) or not math.isfinite(v) or v <= 0
                                       for point in points for v in point)
                    or any(b[0] <= a[0] or b[1] <= a[1] for a, b in zip(points, points[1:]))):
                raise ValueError("Calibration curve must have increasing positive recorded and true flows")
            if measured_flow is None:
                return None
            if not math.isfinite(measured_flow) or not points[0][0] * 0.95 <= measured_flow <= points[-1][0] * 1.05:
                raise ValueError("Measured flow is outside the calibrated range (5% endpoint tolerance)")
            if measured_flow <= points[0][0]:
                return points[0][1] / points[0][0]
            if measured_flow >= points[-1][0]:
                return points[-1][1] / points[-1][0]
            for a, b in zip(points, points[1:]):
                if a[0] <= measured_flow <= b[0]:
                    true_flow = a[1] + (b[1] - a[1]) * (measured_flow - a[0]) / (b[0] - a[0])
                    return true_flow / measured_flow
        value = result.get("multiplier")
    else:
        value = measurement(context, context["config"]["flow_multiplier"])
    if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
        raise ValueError("Choose a flow calibration or enter a positive multiplier for recorded flow")
    return value


def normalize_calculation(entry, steps, fields):
    if set(entry) != {"type", "channel", "liquid", "flow_multiplier", "samples"}:
        raise ValueError("dead_volume requires type, channel, liquid, flow_multiplier and samples")
    if not isinstance(entry["liquid"], str) or not entry["liquid"].strip():
        raise ValueError("liquid must identify the measured oil")
    sample_steps(entry, steps, mode="flow")
    measurement_binding(fields, entry["flow_multiplier"], {"1"})
    for sample in entry["samples"]:
        if set(sample) != {"step", "injection", "arrival", "timing_uncertainty"}:
            raise ValueError("dead_volume sample requires step, injection, arrival and timing_uncertainty")
        for key in ("injection", "arrival", "timing_uncertainty"):
            measurement_binding(fields, sample[key], {"s"}, sample["step"])
        if len({sample[key] for key in ("injection", "arrival", "timing_uncertainty")}) != 3:
            raise ValueError("Marker times and uncertainty require distinct measurements")
    return deepcopy(entry)


def check(context):
    flow_multiplier(context)
    for sample in context["config"]["samples"]:
        for key in ("injection", "arrival", "timing_uncertainty"):
            measurement(context, sample[key])


def calculate(context):
    check(context)
    factor = flow_multiplier(context)
    config = context["config"]
    rows, values, issues = [], [], []
    for sample in config["samples"]:
        row = {"step": sample["step"], "volume_ul": None, "timing_uncertainty_ul": None, "issues": []}
        try:
            injection, arrival, sigma = [measurement(context, sample[key])
                                          for key in ("injection", "arrival", "timing_uncertainty")]
            start, end = step_window(context, sample["step"])
            if not 0 <= injection < arrival <= end - start or sigma < 0:
                raise ValueError("Marker arrival must follow injection inside the running step; uncertainty must be nonnegative")
            readings = trace(context, config["channel"], start + injection, start + arrival)
            if any(r[1] <= 0 for r in readings):
                raise ValueError("Marker transit requires positive measured flow throughout")
            corrected = [(row[0], row[1] * flow_multiplier(context, row[1])) for row in readings]
            volume = volume_ul(corrected)
            values.append(volume)
            row.update(volume_ul=volume, transit_s=arrival - injection,
                       timing_uncertainty_ul=math.hypot(corrected[0][1], corrected[-1][1]) * sigma / 60)
        except ValueError as exc:
            row["issues"].append(str(exc))
            issues.append(f"Step {sample['step']}: {exc}")
        rows.append(row)
    stats = repeat_statistics(values)
    if len(values) < 2:
        issues.append("At least two valid marker passes are required")
    if stats["sd"] is not None and stats["sd"] > 0.1 * stats["mean"]:
        issues.append("Marker volume repeat CV exceeds 10%")
    return {"status": "usable" if not issues else "inconclusive", "channel": config["channel"],
            "liquid": config["liquid"], "volume_ul": stats["mean"] if not issues else None,
            "flow_multiplier": factor, "samples": rows, "repeat_statistics": stats,
            "uncertainty": None, "issues": issues,
            "thresholds": {"minimum_repeats": 2, "repeat_cv_max": 0.1},
            "note": "Effective marker-displacement volume, not pressure startup delay. Times are relative to actual "
                    "step start. Timing uncertainty assumes independent injection/arrival errors; repeat SEM and "
                    "timing uncertainty exclude calibration and marker-dispersion bias."}
