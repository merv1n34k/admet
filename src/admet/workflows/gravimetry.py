"""Gravimetric correction relative to the flow already recorded by the SDK."""

from copy import deepcopy
import math
from statistics import mean, stdev

from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNEL_LABELS
from admet.workflows.calculation_schema import measurement_binding, sample_steps, three_flow_passes
from admet.workflows.calculation_inputs import settled_after, skipped_steps, step_window, trace, volume_ul
from admet.workflows.json_protocol import per_unit


def unit_configs(config):
    """One configuration per unit the run used; {unit} in mass bindings names that unit."""
    return [per_unit({**{k: v for k, v in config.items() if k != "units"}, "channel": channel}, channel)
            for channel in config["units"]]


def normalize_calculation(entry, steps, fields):
    if set(entry) != {"type", "units", "liquid", "density", "samples"}:
        raise ValueError("gravimetry requires type, units, liquid, density and samples")
    if not isinstance(entry["liquid"], str) or not entry["liquid"].strip():
        raise ValueError("liquid must identify the measured fluid")
    measurement_binding(fields, entry["density"], {"g/mL"})
    for unit in unit_configs(entry):
        expanded = sample_steps(unit, steps, triggers=("time", "volume"), shared=True)
        for sample in unit["samples"]:
            if set(sample) != {"step", "pass", "before", "after"}:
                raise ValueError("gravimetry sample requires step, pass, before and after")
            for key in ("before", "after"):
                measurement_binding(fields, sample[key], {"mg", "g"}, sample["step"])
            if sample["before"] == sample["after"]:
                raise ValueError("before and after masses must be distinct measurements")
        three_flow_passes(unit, expanded)
    return deepcopy(entry)


def measurement(context, key):
    value = context["measurements"]["values"].get(key)
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError(f"Missing measurement: {key}")
    return value


def density(context):
    reference = context["references"].get("density")
    if reference and reference["result"].get("fluid_id") != context["config"]["liquid"]:
        raise ValueError("Density reference must identify the same fluid")
    value = reference["result"].get("density_g_ml") if reference else measurement(context, context["config"]["density"])
    if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
        raise ValueError("A positive fluid density or usable density reference is required")
    return value


def check(context):
    if context["config"].get("type") != "gravimetry":
        raise ValueError("Gravimetry is not declared for this run")
    density(context)
    # Each unit is calculated on its own; one unit with all its masses is enough to start.
    missing = []
    for unit in unit_configs(context["config"]):
        try:
            used = [s for s in unit["samples"] if not left_out(context, s, set())]
            if not used:
                raise ValueError(f"Missing measurement: {unit['samples'][0]['after']}")
            for sample in used:
                for key in ("before", "after"):
                    measurement(context, sample[key])
            return
        except ValueError as exc:
            missing.append(exc)
    raise missing[0]


def repeat_statistics(values):
    return {"mean": mean(values) if values else None,
            "sd": stdev(values) if len(values) > 1 else None,
            "sem": stdev(values) / math.sqrt(len(values)) if len(values) > 1 else None,
            "repeats": len(values)}


def calibration_context(context):
    return deepcopy(context["summary"].get("rig_fingerprint", {}).get("correction_settings"))


def calibration_identity(context, channel):
    from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNELS

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
        samples = [row for row in rows if row["target_ul_min"] == target and not row["issues"] and not row["left_out"]]
        stats = repeat_statistics([row["true_flow_ul_min"] for row in samples])
        # Three collections per target, fewer only by those left out, and never under two.
        expected = 3 - sum(bool(row["left_out"]) for row in rows if row["target_ul_min"] == target)
        if len(samples) != 3 and (len(samples) != expected or len(samples) < 2):
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
            used = [row for row in rows if not row["left_out"]]
            fit = linear_fit([row["recorded_flow_ul_min"] for row in used], [row["true_flow_ul_min"] for row in used])
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
    units = {str(config["channel"]): unit_result(context, config, rho) for config in unit_configs(context["config"])}
    return {"status": "usable" if all(u["status"] == "usable" for u in units.values()) else "inconclusive",
            "liquid": context["config"]["liquid"], "density_g_ml": rho, "units": units,
            "issues": [f"{FLUIDIC_CHANNEL_LABELS[int(ch)]}: {issue}" for ch, u in units.items() for issue in u["issues"]],
            "uncertainty": None,
            "thresholds": {"repeat_cv_max": 0.05, "minimum_repeats": 3, "flow_fit_r_squared_min": 0.95},
            "correction_settings": calibration_context(context),
            "note": "Each unit is calculated alone from its own recorded flow and vessels. "
                    "Factors compare weighed with recorded flow, after the corrections the run used. No hardware changes. "
                    "Before/after weights characterize complete dispenses, including startup and the flow that "
                    "settles after the stop. R² describes the "
                    "recorded-versus-true flow curve, not mass versus time. Per-target 95% intervals use three "
                    "independent normally distributed collections, not sensor sample count. Direction differences "
                    "are exploratory (two ascending passes, one descending). Density, balance, evaporation and "
                    "retained droplets add uncertainty. A flow-dependent correction must not be averaged away."}


def left_out(context, sample, skipped):
    """Why a collection is not used: skipped during the run, or no mass entered after it."""
    if sample["step"] in skipped:
        return "skipped in the run"
    if context["measurements"]["values"].get(sample["after"]) is None:
        return "no mass entered after it"
    return None


def unit_result(context, config, rho):
    rows, issues, factors = [], [], []
    skipped = skipped_steps(context)
    for sample in config["samples"]:
        step = context["summary"]["steps"][sample["step"] - 1]
        result = {"step": sample["step"], "pass": sample.get("pass"),
                  "target_ul_min": step["flow_setpoints_ul_min"][str(config["channel"])],
                  "true_flow_ul_min": None, "recorded_flow_ul_min": None,
                  "true_volume_ul": None, "recorded_volume_ul": None,
                  "multiplier": None, "left_out": left_out(context, sample, skipped), "issues": []}
        if result["left_out"]:
            # Left out and listed, not held against the result.
            rows.append(result)
            continue
        try:
            masses = [measurement(context, sample[key]) *
                      (1000 if context["measurements"]["fields"][sample[key]]["unit"] == "g" else 1)
                      for key in ("before", "after")]
            true_volume = (masses[1] - masses[0]) / rho
            window = step_window(context, sample["step"])
            # The vessel also catches what flows after the stop, so the recorded
            # volume runs on until that tail has settled.
            settled = settled_after(context, config["channel"], sample["step"], window[1], result["target_ul_min"])
            during = volume_ul(trace(context, config["channel"], *window))
            recorded = volume_ul(trace(context, config["channel"], window[0], settled))
            if not all(math.isfinite(v) and v > 0 for v in (true_volume, recorded, during)):
                raise ValueError("Collection mass and recorded volume must be positive")
            factor = true_volume / recorded
            factors.append(factor)
            recorded_flow = during * 60 / (window[1] - window[0])
            result.update(true_volume_ul=true_volume, recorded_volume_ul=recorded, multiplier=factor,
                          tail_volume_ul=recorded - during, window_elapsed_s=window, settled_elapsed_s=settled,
                          true_flow_ul_min=recorded_flow * factor, recorded_flow_ul_min=recorded_flow)
            if abs(result["recorded_flow_ul_min"] - result["target_ul_min"]) > 0.2 * result["target_ul_min"]:
                raise ValueError("Recorded mean flow differs from target by more than 20%; investigate capacity/settling")
        except ValueError as exc:
            result["issues"].append(str(exc))
            issues.append(f"Step {sample['step']}: {exc}")
        rows.append(result)
    stats = repeat_statistics(factors)
    curve = flow_curve(rows, issues)
    scalar_ok = factors and max(factors) - min(factors) <= 0.05 * stats["mean"]
    usable = len(factors) >= 2 and not issues
    return {"status": "usable" if usable else "inconclusive",
            "channel": config["channel"], "samples": rows,
            "multiplier": stats["mean"] if usable and scalar_ok else None,
            **curve, "issues": issues, "calibration_identity": calibration_identity(context, config["channel"]),
            "suggested_correction": suggest_correction(context, config["channel"], rows) if usable else None}


def raw_reading(value, terms):
    """The table reading the unit turned into value with its terms a·x + b·x² + c·x³ (x ≥ 0)."""
    a, b, c = terms
    if not b and not c:
        return value / a
    reported = lambda x: a * x + b * x * x + c * x ** 3
    low, high = 0.0, max(1.0, abs(value))
    for _ in range(60):
        if reported(high) >= value:
            break
        high *= 2
    for _ in range(100):
        middle = (low + high) / 2
        low, high = (middle, high) if reported(middle) < value else (low, middle)
    return (low + high) / 2


def suggest_correction(context, channel, rows):
    """Terms for the unit's own correction, so it reports the weighed flow over the range measured."""
    from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNELS

    settings = calibration_context(context) or {}
    prefix = FLUIDIC_CHANNELS[channel][0] if channel < len(FLUIDIC_CHANNELS) else None
    keys = [f"{prefix}_{key}" for key in ("scale", "offset", "quadratic")]
    if prefix is None or any(type(settings.get(key)) not in (int, float) for key in keys) or not settings[keys[0]]:
        return None
    terms = [settings[key] for key in keys]
    points = [(raw_reading(row["recorded_flow_ul_min"], terms), row["true_flow_ul_min"])
              for row in rows if not row["left_out"] and not row["issues"]]
    if len(points) < 3 or any(x <= 0 or y <= 0 for x, y in points):
        return None

    def worst(model):
        return max(abs(model(x) - y) / y for x, y in points)

    sxx = sum(x * x for x, _ in points)
    scale = sum(x * y for x, y in points) / sxx
    s3, s4 = sum(x ** 3 for x, _ in points), sum(x ** 4 for x, _ in points)
    sxy, sxxy = sum(x * y for x, y in points), sum(x * x * y for x, y in points)
    det = sxx * s4 - s3 * s3
    options = {"scale": {"scale": scale, "square": 0.0, "cube": 0.0, "worst_error": worst(lambda x: scale * x)}}
    if det > 0:
        a, b = (sxy * s4 - sxxy * s3) / det, (sxx * sxxy - s3 * sxy) / det
        options["square"] = {"scale": a, "square": b, "cube": 0.0, "worst_error": worst(lambda x: a * x + b * x * x)}
    a0, b0, c0 = terms
    options["current"] = {"scale": a0, "square": b0, "cube": c0,
                          "worst_error": worst(lambda x: a0 * x + b0 * x * x + c0 * x ** 3)}
    # Keep what the run used unless a change is clearly better; over a narrow range many
    # scale/square pairs draw nearly the same curve. A curve only when one scale misses
    # by over 2 % somewhere and the curve does clearly better.
    best = min(option["worst_error"] for option in options.values())
    curve = options.get("square")
    if options["current"]["worst_error"] <= best + 0.01:
        recommended = "current"
    elif (curve and options["scale"]["worst_error"] > 0.02
          and curve["worst_error"] < options["scale"]["worst_error"] - 0.005):
        recommended = "square"
    else:
        recommended = "scale"
    return {"table": settings.get(f"{prefix}_calibration"), "recommended": recommended, "options": options,
            "range_ul_min": [min(y for _, y in points), max(y for _, y in points)],
            "from_terms": dict(zip(("scale", "square", "cube"), terms))}
