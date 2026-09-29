"""Single-height settling and averaging-window assessment from closed recordings."""

import csv
import json
import math
from pathlib import Path
import random
from statistics import mean, stdev

from admet.workflows.oil_density import _finite, _linear_fit


WINDOWS_S = (5, 10, 20, 30)
THRESHOLDS = {
    "pressure_sd_mbar": 1.0,
    "pressure_half_drift_mbar": 0.5,
    "flow_sd_ul_min": "max(0.5, 10% of target)",
    "flow_half_drift_ul_min": "max(0.5, 5% of target)",
    "tracking_error_ul_min": "max(1, 20% of target)",
    "minimum_coverage_fraction": 0.8,
    "maximum_sample_gap_s": 1.0,
    "minimum_stable_tail_extra_s": 5,
    "fit_r_squared_min": 0.995,
    "p0_bootstrap_sd_max_mbar": 0.25,
    "p0_return_difference_max_mbar": 0.5,
    "resistance_return_difference_max_percent": 10.0,
    "bootstrap_block_s": 2,
    "minimum_bootstrap_blocks": 5,
    "minimum_recommended_averaging_s": 20,
    "successive_windows_required": 2,
    "successive_pressure_mean_difference_mbar": 0.5,
    "successive_flow_mean_difference_ul_min": "max(0.5, 5% of target)",
}


def normalize_analysis(value, steps):
    from admet.workflows.json_protocol import number

    if set(value) != {"type", "height_cm", "points"}:
        raise ValueError("flow_scout analysis requires type, height_cm and points")
    height = number(value["height_cm"], "height_cm")
    points = value["points"]
    if not isinstance(points, list) or not 6 <= len(points) <= 40:
        raise ValueError("scout needs 3–20 flow targets in two passes")
    acquisition = []
    for index, step in enumerate(steps, 1):
        if (set(step["sensor_setpoints"]) != {"1"} or step["pressure_setpoints"]
                or step["trigger_type"] != "time" or step.get("repeat", 1) != 1 or step.get("group")
                or step["on_complete"] != "zero"):
            raise ValueError("scout requires explicit timed M1 flow steps ending at zero")
        flow = step["sensor_setpoints"]["1"]
        duration = step["trigger_params"]["duration_s"]
        if flow > 0:
            if duration < 15 or step.get("timeout_s", 0) <= duration:
                raise ValueError("scout points need >=15 seconds and a longer timeout")
            acquisition.append(index)
        elif duration != 0:
            raise ValueError("scout zero-flow steps must have zero duration")
    if (steps[0]["sensor_setpoints"]["1"] != 0 or steps[-1]["sensor_setpoints"]["1"] != 0
            or len(points) != len(acquisition)):
        raise ValueError("scout must start/end at zero and map every acquisition step")
    for index, point in enumerate(points):
        if (not isinstance(point, dict) or set(point) != {"step", "pass"}
                or type(point["step"]) is not int or point["step"] != acquisition[index]
                or type(point["pass"]) is not int or point["pass"] != (1 if index < len(points) // 2 else 2)):
            raise ValueError("scout points must map ordered steps to passes 1 and 2")
    targets = [[steps[p["step"] - 1]["sensor_setpoints"]["1"] for p in points if p["pass"] == repeat]
               for repeat in (1, 2)]
    if len(set(targets[0])) != len(targets[0]) or targets[0] != sorted(targets[0]) or targets[1] != targets[0][::-1]:
        raise ValueError("scout requires distinct increasing flows then the same flows in reverse")
    steps[points[0]["step"] - 1]["confirm_message"] = (
        f"Confirm M1 (channel 1), outlet {height:g} cm above the reservoir oil surface. "
        "Keep this height unchanged throughout both flow sweeps. Confirm oil reaches the outlet. "
        "This is a scout only; review its calculation before any density experiment."
    )
    return {"type": "flow_scout", "height_cm": height, "points": points}


def _window(rows, lower, width, target):
    selected = [r for r in rows if lower <= r[0] < lower + width]
    valid = [r for r in selected if r[1] is not None and r[2] is not None]
    issues = []
    result = {"samples": len(valid), "pressure_mean_mbar": None, "flow_mean_ul_min": None,
              "pressure_sd_mbar": None, "flow_sd_ul_min": None,
              "pressure_half_drift_mbar": None, "flow_half_drift_ul_min": None}
    if (len(valid) != len(selected) or len(valid) < 10
            or valid[-1][0] - valid[0][0] < width * THRESHOLDS["minimum_coverage_fraction"]
            or max(b[0] - a[0] for a, b in zip(valid, valid[1:])) > THRESHOLDS["maximum_sample_gap_s"]):
        return {**result, "issues": ["missing data or insufficient coverage"], "blocks": []}
    for column, name, unit, sd_limit, drift_limit in (
        (1, "pressure", "mbar", THRESHOLDS["pressure_sd_mbar"], THRESHOLDS["pressure_half_drift_mbar"]),
        (2, "flow", "ul_min", max(0.5, 0.1 * target), max(0.5, 0.05 * target)),
    ):
        values = [r[column] for r in valid]
        early = [r[column] for r in valid if r[0] < lower + width / 2]
        late = [r[column] for r in valid if r[0] >= lower + width / 2]
        drift = abs(mean(late) - mean(early)) if early and late else None
        result.update({f"{name}_mean_{unit}": mean(values), f"{name}_sd_{unit}": stdev(values),
                       f"{name}_half_drift_{unit}": drift})
        if stdev(values) > sd_limit or drift is None or drift > drift_limit:
            issues.append(f"unstable {name}")
    if abs(result["flow_mean_ul_min"] - target) > max(1, 0.2 * target):
        issues.append("target not reached")
    blocks = []
    for offset in range(0, int(width) - 1, THRESHOLDS["bootstrap_block_s"]):
        block = [r for r in valid if lower + offset <= r[0] < lower + offset + 2]
        if block:
            blocks.append([mean(r[1] for r in block), mean(r[2] for r in block)])
    return {**result, "issues": issues, "blocks": blocks}


def _fit(points):
    fit = _linear_fit([p["flow_mean_ul_min"] for p in points], [p["pressure_mean_mbar"] for p in points])
    rng = random.Random(16000)
    intercepts = []
    for _ in range(200):
        sampled = [rng.choices(p["blocks"], k=len(p["blocks"])) for p in points]
        trial = _linear_fit([mean(b[1] for b in group) for group in sampled],
                            [mean(b[0] for b in group) for group in sampled])
        intercepts.append(trial.intercept)
    return {"p0_mbar": fit.intercept, "resistance_mbar_min_ul": fit.slope,
            "r_squared": fit.r_squared, "p0_bootstrap_sd_mbar": stdev(intercepts)}


def analyze_scout(context):
    from admet.workflows.json_protocol import resolve

    document = resolve(context["document"])
    config = document.get("analysis", {})
    if config.get("type") != "flow_scout":
        raise ValueError("Choose a single-height flow scout recording")
    result = {"type": "flow_scout", "status": "inconclusive", "density_g_ml": None,
              "thresholds": dict(THRESHOLDS), "points": [], "fits": [], "recommendation": None,
              "issues": [], "note": "Single height cannot measure density. Bootstrap describes random "
              "precision conditional on 2-second blocks, not sensor accuracy or slow drift. "
              "Review and approve settings before a separate density run."}
    summary = context["summary"]
    corrections = summary.get("rig_fingerprint", {}).get("correction_settings") or {}
    scale = _finite(corrections.get("cells_m_scale"))
    linear = (scale is not None and scale > 0 and corrections.get("cells_m_offset") == 0
              and corrections.get("cells_m_quadratic") == 0)
    result["height_cm"] = config["height_cm"]
    result["calibration"] = {"name": corrections.get("cells_m_calibration"), "scale": scale,
                             "raw_equivalent_available": linear}
    origin = _finite(summary.get("artifacts", {}).get("polling_origin_monotonic"))
    if summary.get("state") != "completed" or origin is None:
        result["issues"].append("completed run and recording clock origin required")
        return result
    with Path(context["csv"]).open(newline="", encoding="utf-8") as handle:
        rows = [tuple(_finite(row.get(k)) for k in ("elapsed_s", "pressure_1_mbar", "flow_1_ul_min"))
                for row in csv.DictReader(handle)]
    events = [json.loads(line) for line in (context["directory"] / "events.jsonl").read_text().splitlines() if line]
    if (any(r[0] is None for r in rows) or any(b[0] <= a[0] for a, b in zip(rows, rows[1:]))
            or any(e.get("state") == "paused" or e.get("outcome") in
                   {"skipped", "timed_out", "cancelled", "error"} for e in events)):
        result["issues"].append("invalid timestamps, pause or failed step")
        return result
    accepted = {}
    traces = {}
    for point in config["points"]:
        index = point["step"] - 1
        step = document["steps"][index]
        target = step["sensor_setpoints"]["1"]
        duration = step["trigger_params"]["duration_s"]
        relevant = [e for e in events if e.get("step_index") == index]
        starts = [e for e in relevant if e.get("outcome") == "running" and e.get("state") == "running"
                  and not e.get("confirmation_message")]
        ends = [e for e in relevant if e.get("outcome") == "completed"]
        entry = {**point, "target_ul_min": target,
                 "unscaled_target_ul_min": target / scale if linear else None,
                 "windows": [], "issues": []}
        result["points"].append(entry)
        start = _finite(starts[0].get("monotonic")) if starts else None
        end = _finite(ends[0].get("monotonic")) if len(ends) == 1 else None
        if start is None or end is None or end - start < duration - 0.1:
            entry["issues"].append("incomplete step timing")
            result["issues"].append(f"Step {point['step']}: incomplete timing")
            continue
        relative = [(t - (start - origin), p, q) for t, p, q in rows if start - origin <= t < end - origin]
        traces[(point["pass"], target)] = (relative, duration)
        for width in WINDOWS_S:
            last = math.floor(duration - width - 0.2)
            candidates = [(offset, _window(relative, offset, width, target)) for offset in range(max(0, last + 1))]
            trailing = []
            for candidate in reversed(candidates):
                if candidate[1]["issues"]:
                    break
                trailing.append(candidate)
            settle = trailing[-1][0] if len(trailing) >= 6 else None
            stats = candidates[-1][1] if candidates else {"issues": ["point too short"]}
            entry["windows"].append({"averaging_s": width, "settling_s": settle,
                                     **{k: v for k, v in stats.items() if k != "blocks"}})
            if settle is not None:
                accepted[(point["pass"], target, width)] = {**stats, "settling_s": settle}
    targets = sorted({p["target_ul_min"] for p in result["points"]})
    for width in WINDOWS_S:
        usable = [q for q in targets if all((repeat, q, width) in accepted for repeat in (1, 2))]
        fit_result = {"averaging_s": width, "usable_targets_ul_min": usable, "passes": [], "issues": []}
        result["fits"].append(fit_result)
        if len(usable) < 3:
            fit_result["issues"].append("fewer than three repeatable flow targets")
            continue
        settling = max(accepted[(repeat, q, width)]["settling_s"] for repeat in (1, 2) for q in usable) + 5
        fit_result["settling_s"] = settling
        proposed = {}
        verification = {}
        for repeat in (1, 2):
            for target in usable:
                trace, duration = traces[(repeat, target)]
                stats = _window(trace, settling, width, target)
                following = _window(trace, settling + width, width, target)
                if settling + 2 * width > duration - 0.2:
                    fit_result["issues"].append("longer acquisition required to verify two successive windows")
                elif stats["issues"] or following["issues"]:
                    fit_result["issues"].append("successive measurement windows are not both stable")
                elif (abs(stats["pressure_mean_mbar"] - following["pressure_mean_mbar"])
                      > THRESHOLDS["successive_pressure_mean_difference_mbar"]
                      or abs(stats["flow_mean_ul_min"] - following["flow_mean_ul_min"]) > max(0.5, 0.05 * target)):
                    fit_result["issues"].append("successive pressure/flow window means disagree")
                proposed[(repeat, target)] = stats
                verification[(repeat, target)] = following
        fit_result["issues"] = list(dict.fromkeys(fit_result["issues"]))
        if fit_result["issues"]:
            continue
        if any(len(stats["blocks"]) < THRESHOLDS["minimum_bootstrap_blocks"] for stats in proposed.values()):
            fit_result["issues"].append("too few blocks for a precision recommendation")
            continue
        try:
            fits = [_fit([proposed[(repeat, q)] for q in usable]) for repeat in (1, 2)]
            verification_fits = [_fit([verification[(repeat, q)] for q in usable]) for repeat in (1, 2)]
        except ValueError as exc:
            fit_result["issues"].append(str(exc))
            continue
        fit_result["passes"] = fits
        fit_result["verification_passes"] = verification_fits
        if any(f["r_squared"] < THRESHOLDS["fit_r_squared_min"] or f["resistance_mbar_min_ul"] <= 0
               or f["p0_bootstrap_sd_mbar"] > THRESHOLDS["p0_bootstrap_sd_max_mbar"]
               for f in [*fits, *verification_fits]):
            fit_result["issues"].append("insufficient fit precision or linearity")
        p0_difference = abs(fits[0]["p0_mbar"] - fits[1]["p0_mbar"])
        resistance_mean = mean(f["resistance_mbar_min_ul"] for f in fits)
        resistance_difference = (100 * abs(fits[0]["resistance_mbar_min_ul"] - fits[1]["resistance_mbar_min_ul"])
                                 / resistance_mean) if resistance_mean > 0 else None
        fit_result.update(p0_return_difference_mbar=p0_difference,
                          resistance_return_difference_percent=resistance_difference)
        if (p0_difference > THRESHOLDS["p0_return_difference_max_mbar"] or resistance_difference is None
                or resistance_difference > THRESHOLDS["resistance_return_difference_max_percent"]):
            fit_result["issues"].append("forward/reverse fits disagree")
        comparisons = [*zip(fits, verification_fits), tuple(verification_fits)]
        for first, second in comparisons:
            resistance = mean(f["resistance_mbar_min_ul"] for f in (first, second))
            if (abs(first["p0_mbar"] - second["p0_mbar"]) > THRESHOLDS["p0_return_difference_max_mbar"]
                    or resistance <= 0 or 100 * abs(first["resistance_mbar_min_ul"]
                                                   - second["resistance_mbar_min_ul"]) / resistance
                    > THRESHOLDS["resistance_return_difference_max_percent"]):
                fit_result["issues"].append("successive-window or verification return fits disagree")
                break
        if width < THRESHOLDS["minimum_recommended_averaging_s"]:
            fit_result["issues"].append("diagnostic only: minimum recommended averaging is 20 seconds")
        if not fit_result["issues"] and not result["issues"] and result["recommendation"] is None:
            result["recommendation"] = {"targets_ul_min": usable, "settling_s": settling,
                                        "averaging_s": width, "requires_operator_approval": True}
            result["status"] = "usable"
    if result["recommendation"] is None:
        result["issues"].append("No tested averaging window meets all fit and repeatability thresholds")
        result["issues"].append("Longer acquisition required to establish stability; inspect drift or oil retreat "
                                "before repeating the affected flow range. No automatic retry.")
    return result
