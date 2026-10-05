"""Offline analysis for hydrostatic density measurements of a fluid (oil, water, ...)."""

from __future__ import annotations

from dataclasses import dataclass
import math
import csv
import json
from pathlib import Path
from statistics import mean, stdev
from typing import Iterable

GRAVITY_CONVERSION_MBAR_PER_CM_PER_G_ML = 0.980665


@dataclass(frozen=True)
class FluidDensityMeasurement:
    fluid_id: str
    role: str
    height_cm: float
    pressure_mbar: float
    flow_ul_min: float
    flow_std_ul_min: float | None = None
    direction: str = ""


@dataclass(frozen=True)
class LinearFit:
    slope: float
    intercept: float
    r_squared: float
    slope_standard_error: float | None
    intercept_standard_error: float | None
    covariance: float | None
    samples: int


@dataclass(frozen=True)
class BalancePressure:
    height_cm: float
    pressure_mbar: float
    pressure_standard_error_mbar: float | None
    flow_pressure_slope: float
    r_squared: float
    samples: int
    directions: tuple[str, ...]


@dataclass(frozen=True)
class FluidDensityResult:
    fluid_id: str
    role: str
    density_g_ml: float
    density_standard_error_g_ml: float | None
    density_ci95_g_ml: float | None
    pressure_height_slope_mbar_cm: float
    intercept_mbar: float
    r_squared: float
    balances: tuple[BalancePressure, ...]
    warnings: tuple[str, ...]


def _linear_fit(xs: list[float], ys: list[float]) -> LinearFit:
    if len(xs) != len(ys) or len(xs) < 2:
        raise ValueError("a linear fit requires at least two paired values")
    if not all(math.isfinite(value) for value in (*xs, *ys)):
        raise ValueError("fit values must be finite")

    count = len(xs)
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    sxx = sum((value - mean_x) ** 2 for value in xs)
    if sxx <= 0:
        raise ValueError("fit requires at least two distinct x values")
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sxx
    intercept = mean_y - slope * mean_x
    residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys, strict=True)]
    total = sum((value - mean_y) ** 2 for value in ys)
    residual_sum = sum(value**2 for value in residuals)
    r_squared = 1.0 - residual_sum / total if total > 0 else 1.0

    slope_se = intercept_se = covariance = None
    if count > 2:
        residual_variance = residual_sum / (count - 2)
        slope_se = math.sqrt(residual_variance / sxx)
        intercept_se = math.sqrt(residual_variance * (1.0 / count + mean_x**2 / sxx))
        covariance = -mean_x * residual_variance / sxx
    return LinearFit(
        slope,
        intercept,
        r_squared,
        slope_se,
        intercept_se,
        covariance,
        count,
    )


def _balance_pressure(measurements: list[FluidDensityMeasurement]) -> BalancePressure:
    fit = _linear_fit(
        [measurement.pressure_mbar for measurement in measurements],
        [measurement.flow_ul_min for measurement in measurements],
    )
    if abs(fit.slope) < 1e-12:
        raise ValueError("flow does not change with pressure")
    pressure = -fit.intercept / fit.slope
    pressure_se = None
    if (
        fit.slope_standard_error is not None
        and fit.intercept_standard_error is not None
        and fit.covariance is not None
    ):
        variance = (
            fit.intercept_standard_error**2 / fit.slope**2
            + fit.intercept**2 * fit.slope_standard_error**2 / fit.slope**4
            - 2.0 * fit.intercept * fit.covariance / fit.slope**3
        )
        pressure_se = math.sqrt(max(0.0, variance))
    directions = tuple(sorted({item.direction for item in measurements if item.direction}))
    return BalancePressure(
        height_cm=measurements[0].height_cm,
        pressure_mbar=pressure,
        pressure_standard_error_mbar=pressure_se,
        flow_pressure_slope=fit.slope,
        r_squared=fit.r_squared,
        samples=fit.samples,
        directions=directions,
    )


def analyze_fluid_density(
    measurements: Iterable[FluidDensityMeasurement],
) -> tuple[FluidDensityResult, ...]:
    grouped: dict[tuple[str, str], dict[float, list[FluidDensityMeasurement]]] = {}
    for item in measurements:
        if not item.fluid_id.strip():
            raise ValueError("fluid name cannot be blank")
        values = (item.height_cm, item.pressure_mbar, item.flow_ul_min)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("height, pressure, and flow must be finite")
        grouped.setdefault((item.fluid_id.strip(), item.role.strip()), {}).setdefault(
            item.height_cm, []
        ).append(item)

    results: list[FluidDensityResult] = []
    for (fluid_id, role), height_groups in sorted(grouped.items()):
        warnings: list[str] = []
        balances: list[BalancePressure] = []
        for height, rows in sorted(height_groups.items()):
            try:
                balance = _balance_pressure(rows)
            except ValueError as exc:
                warnings.append(f"height {height:g} cm excluded: {exc}")
                continue
            balances.append(balance)
            if balance.samples < 4:
                warnings.append(f"height {height:g} cm has fewer than 4 pressure points")
            if balance.r_squared < 0.95:
                warnings.append(
                    f"height {height:g} cm flow-pressure fit R² is {balance.r_squared:.3f}"
                )
            if len(balance.directions) < 2:
                warnings.append(f"height {height:g} cm lacks both sweep directions")

        if len(balances) < 3:
            warnings.append("at least 3 fitted heights are required for density")
            continue
        fit = _linear_fit(
            [balance.height_cm for balance in balances],
            [balance.pressure_mbar for balance in balances],
        )
        density = fit.slope / GRAVITY_CONVERSION_MBAR_PER_CM_PER_G_ML
        density_se = (
            fit.slope_standard_error / GRAVITY_CONVERSION_MBAR_PER_CM_PER_G_ML
            if fit.slope_standard_error is not None
            else None
        )
        if fit.r_squared < 0.98:
            warnings.append(f"pressure-height fit R² is {fit.r_squared:.3f}")
        if density <= 0:
            warnings.append("density is non-positive; check the signed-height convention")
        results.append(
            FluidDensityResult(
                fluid_id=fluid_id,
                role=role,
                density_g_ml=density,
                density_standard_error_g_ml=density_se,
                density_ci95_g_ml=1.96 * density_se if density_se is not None else None,
                pressure_height_slope_mbar_cm=fit.slope,
                intercept_mbar=fit.intercept,
                r_squared=fit.r_squared,
                balances=tuple(balances),
                warnings=tuple(warnings),
            )
        )
    return tuple(results)


def relative_density(
    candidate: FluidDensityResult, reference: FluidDensityResult
) -> tuple[float, float | None]:
    if reference.density_g_ml == 0:
        raise ValueError("reference density is zero")
    ratio = candidate.density_g_ml / reference.density_g_ml
    if (
        candidate.density_standard_error_g_ml is None
        or reference.density_standard_error_g_ml is None
        or candidate.density_g_ml == 0
    ):
        return ratio, None
    relative_variance = (
        (candidate.density_standard_error_g_ml / candidate.density_g_ml) ** 2
        + (reference.density_standard_error_g_ml / reference.density_g_ml) ** 2
    )
    return ratio, abs(ratio) * math.sqrt(relative_variance)


def normalize_analysis(value, steps):
    """Validate explicit step associations and derive gates from the same heights."""
    from copy import deepcopy
    from admet.workflows.json_protocol import number

    from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNEL_LABELS

    if not isinstance(value, dict) or set(value) != {"type", "fluid_id", "channel", "points"}:
        raise ValueError("density requires only type, fluid_id, channel and points")
    result = deepcopy(value)
    if result["type"] != "fluid_density":
        raise ValueError("unsupported analysis type")
    fluid = result["fluid_id"]
    if not isinstance(fluid, str) or not fluid.strip() or len(fluid) > 80:
        raise ValueError("the fluid name must contain 1–80 characters")
    channel = result["channel"]
    if type(channel) is not int or not 0 <= channel < len(FLUIDIC_CHANNEL_LABELS):
        raise ValueError("density channel must be one of the fluidic channels")
    c, label = str(channel), FLUIDIC_CHANNEL_LABELS[channel]
    points = result["points"]
    if not isinstance(points, list) or len(points) != 18:
        raise ValueError("density needs two passes of 3 heights × 3 points")

    def zero_step(step):
        return (step["trigger_params"].get("duration_s") == 0
                and step["sensor_setpoints"] == {c: 0} and not step["pressure_setpoints"])

    for step in steps:
        if step.get("repeat", 1) != 1 or step.get("group"):
            raise ValueError("density steps must be explicit, without repeat/group expansion")
        if step.get("on_complete") != "zero" or step.get("trigger_type") != "time":
            raise ValueError("density steps must use time and end with zero")
        if step["sensor_setpoints"]:
            if set(step["sensor_setpoints"]) != {c} or step["pressure_setpoints"]:
                raise ValueError(f"density may control channel {c} only")
        elif not zero_step(step):
            raise ValueError(f"density non-sampling steps must zero only channel {c}")
    first = steps[0]
    if not zero_step(first):
        raise ValueError("density must start with a zero-output step")
    acquisition_steps = [i + 1 for i, step in enumerate(steps) if not zero_step(step)]
    if len(acquisition_steps) != len(points) or not zero_step(steps[-1]):
        raise ValueError(f"density must map every sample and finish with channel {c} at zero")
    for index, point in enumerate(points):
        if not isinstance(point, dict) or set(point) != {"step", "pass", "height_cm", "settle_s"}:
            raise ValueError("density point requires step, pass, height_cm and settle_s")
        if type(point["step"]) is not int or point["step"] != acquisition_steps[index]:
            raise ValueError("density points must reference every acquisition step in order (1-based)")
        expected_pass = 1 if index < 9 else 2
        if type(point["pass"]) is not int or point["pass"] != expected_pass:
            raise ValueError("density requires passes 1 and 2")
        point["height_cm"] = number(point["height_cm"], "height_cm")
        point["settle_s"] = number(point["settle_s"], "settle_s", minimum=1)
        step = steps[point["step"] - 1]
        if (set(step["sensor_setpoints"]) != {c} or step["sensor_setpoints"][c] <= 0
                or step["pressure_setpoints"]):
            raise ValueError(f"density uses positive flow on channel {c} only; no other channel is controlled")
        duration = step["trigger_params"].get("duration_s", 0)
        if duration - point["settle_s"] < 5:
            raise ValueError("density needs at least 5 seconds of sampling after settling")
        if step.get("timeout_s", 0) <= duration:
            raise ValueError("density timeout must exceed the timed step duration")
    groups = [points[i:i + 3] for i in range(0, len(points), 3)]
    reference_targets = None
    for group in groups:
        if len({p["height_cm"] for p in group}) != 1:
            raise ValueError("each density sweep must have one confirmed height")
        targets = [steps[p["step"] - 1]["sensor_setpoints"][c] for p in group]
        if len(set(targets)) != 3 or targets != sorted(targets, reverse=group[0]["pass"] == 2):
            raise ValueError("density sweep needs three distinct ordered flow targets")
        if reference_targets is not None and sorted(targets) != reference_targets:
            raise ValueError("density requires identical flow targets at every height and pass")
        reference_targets = sorted(targets)
        point = group[0]
        if not zero_step(steps[point["step"] - 2]):
            raise ValueError(f"density height gates require channel {c} at zero immediately beforehand")
        steps[point["step"] - 1]["confirm_message"] = (
            f"{fluid}: confirm the fluid is routed through {label} (channel {c}). "
            f"Set outlet {point['height_cm']:g} cm ABOVE the current reservoir fluid surface "
            f"(pass {point['pass']}). "
            "Confirm only when this height is measured and correct; keep it constant during this sweep. "
            "Keep the receiving container open to atmosphere and the outlet above collected liquid. "
            "Abort for unexpected pressure or unstable flow. Zero flow may retain pressure; verify no fluid-column retreat."
        )
    forward = [group[0]["height_cm"] for group in groups[:3]]
    reverse = [group[0]["height_cm"] for group in groups[3:]]
    if len(set(forward)) != 3 or forward != sorted(forward) or reverse != list(reversed(forward)):
        raise ValueError("density needs three increasing heights, then the same heights in reverse")
    return result


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _point_statistics(point, step, rows, events, origin, channel):
    result = {**point, "samples": 0, "flow_mean_ul_min": None, "flow_std_ul_min": None,
              "pressure_mean_mbar": None, "pressure_std_mbar": None, "issues": []}
    relevant = [e for e in events if e.get("step_name") and e.get("step_index") == point["step"] - 1]
    starts = [e for e in relevant if e.get("outcome") == "running"
              and not e.get("confirmation_message") and e.get("state") == "running"]
    ends = [e for e in relevant if e.get("outcome") == "completed"]
    if not starts or len(ends) != 1 or any(e.get("state") == "paused" for e in relevant):
        result["issues"].append("missing, paused or incomplete step")
        return result
    start = _finite(starts[0].get("monotonic"))
    end = _finite(ends[0].get("monotonic"))
    duration = step["trigger_params"]["duration_s"]
    if start is None or end is None or end - start < duration - 0.1:
        result["issues"].append("invalid step timing")
        return result
    lower = start - origin + point["settle_s"]
    upper = start - origin + duration - 0.1
    selected = [row for row in rows if row[0] is not None and lower <= row[0] < upper]
    valid = [row for row in selected if row[1] is not None and row[2] is not None]
    result["samples"] = len(valid)
    if len(valid) != len(selected):
        result["issues"].append("missing pressure/flow readings")
    times = [row[0] for row in valid]
    window = duration - point["settle_s"]
    if (len(valid) < 10 or times[-1] - times[0] < 0.8 * window
            or max(b - a for a, b in zip(times, times[1:])) > 1.0):
        result["issues"].append("insufficient sampling coverage")
        return result
    for column, name, unit in ((1, "pressure", "mbar"), (2, "flow", "ul_min")):
        values = [row[column] for row in valid]
        average, deviation = mean(values), stdev(values)
        result[f"{name}_mean_{unit}"] = average
        result[f"{name}_std_{unit}"] = deviation
        tolerance = 1.0 if name == "pressure" else max(0.5, 0.1 * abs(average))
        midpoint = (lower + upper) / 2
        early = [row[column] for row in valid if row[0] < midpoint]
        late = [row[column] for row in valid if row[0] >= midpoint]
        drift = abs(mean(late) - mean(early)) if early and late else None
        result[f"{name}_drift_{unit}"] = drift
        if deviation > tolerance or drift is None or drift > tolerance:
            result["issues"].append(f"unsettled {name}")
    target = step["sensor_setpoints"][str(channel)]
    if abs(result["flow_mean_ul_min"] - target) > max(1, 0.2 * target):
        result["issues"].append("requested flow not reached")
    result["window_elapsed_s"] = [lower, upper]
    return result


def analyze_density_run(document, csv_path, events_path, polling_origin, *, completed):
    """Read closed artifacts only. Sensor samples are not independent replicates."""
    from admet.workflows.json_protocol import resolve
    from admet.workflows.calculation_schema import declarations

    document = resolve(document)
    config = next(item for item in declarations(document) if item["type"] == "fluid_density")
    result = {"type": "fluid_density", "fluid_id": config["fluid_id"], "status": "inconclusive",
              "density_g_ml": None, "repeat_difference_percent": None, "ci95_g_ml": None,
              "passes": [], "points": [], "issues": [], "warnings": [],
              "thresholds": {"pressure_flow_r_squared_min": 0.95, "pressure_height_r_squared_min": 0.95,
                             "repeat_difference_max_percent": 10},
              "note": "Two passes assess repeatability, not a robust confidence interval or absolute accuracy."}
    if not completed:
        result["issues"].append("protocol did not complete")
    origin = _finite(polling_origin)
    if origin is None or origin <= 0:
        result["issues"].append("missing recording clock origin")
        return result
    try:
        with Path(csv_path).open(newline="", encoding="utf-8") as handle:
            channel = config["channel"]
            rows = [tuple(_finite(row.get(key)) for key in
                          ("elapsed_s", f"pressure_{channel}_mbar", f"flow_{channel}_ul_min"))
                    for row in csv.DictReader(handle)]
        events = [json.loads(line) for line in Path(events_path).read_text().splitlines() if line.strip()]
    except (OSError, ValueError, TypeError) as exc:
        result["issues"].append(f"cannot read recording: {exc}")
        return result
    if any(row[0] is None for row in rows) or any(
        b[0] <= a[0] for a, b in zip(rows, rows[1:]) if a[0] is not None and b[0] is not None
    ):
        result["issues"].append("missing or non-monotonic recording times")
    if any(e.get("state") == "paused" or e.get("outcome") in
           {"skipped", "timed_out", "cancelled", "error"} for e in events):
        result["issues"].append("run contains a pause, skip or failed step; repeat the run")
    for point in config["points"]:
        stats = _point_statistics(point, document["steps"][point["step"] - 1], rows, events, origin,
                                  config["channel"])
        result["points"].append(stats)
        result["issues"].extend(f"step {point['step']}: {issue}" for issue in stats["issues"])
    for repeat in (1, 2):
        balances = []
        points = [p for p in result["points"] if p["pass"] == repeat]
        for height in sorted({p["height_cm"] for p in points}):
            group = [p for p in points if p["height_cm"] == height]
            if any(p["issues"] for p in group):
                continue
            try:
                fit = _linear_fit([p["flow_mean_ul_min"] for p in group],
                                  [p["pressure_mean_mbar"] for p in group])
            except ValueError as exc:
                result["issues"].append(f"pass {repeat}, {height:g} cm: {exc}")
                continue
            balances.append({"height_cm": height, "p0_mbar": fit.intercept,
                             "resistance_mbar_min_ul": fit.slope, "r_squared": fit.r_squared})
            if fit.slope <= 0 or fit.r_squared < 0.95:
                result["issues"].append(f"pass {repeat}, {height:g} cm: poor pressure/flow fit")
            elif fit.r_squared < 0.995:
                result["warnings"].append(f"pass {repeat}, {height:g} cm: mild pressure/flow curvature; R²={fit.r_squared:.4f}")
        entry = {"pass": repeat, "density_g_ml": None, "r_squared": None, "balances": balances}
        if len(balances) == 3:
            fit = _linear_fit([b["height_cm"] for b in balances], [b["p0_mbar"] for b in balances])
            entry.update(density_g_ml=fit.slope / GRAVITY_CONVERSION_MBAR_PER_CM_PER_G_ML,
                         r_squared=fit.r_squared)
            if fit.slope <= 0 or fit.r_squared < 0.95:
                result["issues"].append(f"pass {repeat}: poor pressure/height fit")
            elif fit.r_squared < 0.98:
                result["warnings"].append(f"pass {repeat}: pressure/height R²={fit.r_squared:.4f}; review fitted points")
        else:
            result["issues"].append(f"pass {repeat}: fewer than three usable heights")
        result["passes"].append(entry)
    densities = [p["density_g_ml"] for p in result["passes"]]
    if all(d is not None and d > 0 for d in densities):
        average = mean(densities)
        difference = 100 * abs(densities[0] - densities[1]) / average
        result["repeat_difference_percent"] = difference
        if difference > 10:
            result["issues"].append("density passes disagree by more than 10%")
        if not result["issues"]:
            result.update(status="consistent", density_g_ml=average)
    return result
