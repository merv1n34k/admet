"""Offline calculations over archived runs, independent of the live service."""

from copy import deepcopy
from datetime import datetime, timezone
import csv
import json
from pathlib import Path
import re
from statistics import mean, stdev
import uuid

from admet.core.compat import protocol_run_id
from admet.core.protocol_store import write_json
from admet.workflows.compat import density_analysis
from admet.workflows.oil_density import _finite, analyze_density_run
from admet.workflows.flow_scout import analyze_scout
from admet.workflows.calculation_schema import declarations
from admet.workflows.calculation_inputs import load_context, fingerprint, result_current, read_json
from admet.workflows import gravimetry, dead_volume, viscosity


def available_calculations(directory):
    result = []
    try:
        document = read_json(Path(directory) / "protocol.json")
        result.extend(item["type"] for item in declarations(document) if item["type"] in CALCULATIONS)
        if not declarations(document):
            density_analysis(document)
            result.append("oil_density")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return list(dict.fromkeys([*result, "recording_summary"]))


def calculation_readiness(directory, key, references=None):
    if not directory or key not in available_calculations(directory):
        return "Choose a compatible recorded run."
    try:
        context = load_context(directory, key, CALCULATIONS[key], references)
        check = CALCULATIONS[key].get("check")
        if check:
            check(context)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return str(exc)
    return ""


def recorded_runs(project):
    if not project:
        return []
    runs = []
    for path in (Path(project) / "records" / "protocols").glob("*/summary.json"):
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
            if summary.get("state") not in {"completed", "failed", "cancelled"}:
                continue
            name = summary.get("normalized_settings", {}).get("protocol", {}).get("name", path.parent.name)
            runs.append({"directory": str(path.parent), "name": name,
                         "at": summary.get("completed_at") or summary.get("executed_at") or "",
                         "state": summary["state"]})
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return sorted(runs, key=lambda run: run["at"], reverse=True)


def _density(context):
    document = deepcopy(context["document"])
    if "parameters" in document:
        from admet.workflows.json_protocol import resolve

        document = resolve(document)
        recorded = context["summary"].get("steps", [])
        if len(recorded) != len(document["steps"]):
            raise ValueError("Parameterized density requires the recorded execution steps")
        for expected, step in zip(recorded, document["steps"]):
            if (expected.get("flow_setpoints_ul_min") != step["sensor_setpoints"]
                    or expected.get("pressure_setpoints_mbar") != step["pressure_setpoints"]
                    or expected.get("trigger_type") != step["trigger_type"]
                    or expected.get("trigger_params") != step["trigger_params"]
                    or (expected.get("confirmation") or "") != step.get("confirm_message", "")):
                raise ValueError("Recorded density execution and protocol parameters disagree")
    document["analysis"] = density_analysis(document)
    mapping = document["analysis"]["points"]
    previous = None
    for point in mapping:
        group = (point["pass"], point["height_cm"])
        if group != previous:
            message = document["steps"][point["step"] - 1].get("confirm_message", "")
            height = re.search(r"Set outlet ([0-9]+(?:\.[0-9]+)?) cm ABOVE", message)
            if height is None or float(height[1]) != point["height_cm"]:
                raise ValueError("Recorded height confirmation and calculation height disagree")
        previous = group
    result = analyze_density_run(
        document, context["csv"], context["directory"] / "events.jsonl",
        context["summary"].get("artifacts", {}).get("polling_origin_monotonic"),
        completed=context["summary"].get("state") == "completed",
    )
    result["point_mapping"] = mapping
    return result


def _recording_summary(context):
    with context["csv"].open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = [key for key in (reader.fieldnames or [])
                   if re.fullmatch(r"(?:pressure_[0-9]+_mbar|flow_[0-9]+_ul_min)", key)]
        values = {key: [] for key in columns}
        total = 0
        for row in reader:
            total += 1
            for key in columns:
                value = _finite(row.get(key))
                if value is not None:
                    values[key].append(value)
    return {"status": "complete", "rows": total,
            "note": "Whole-recording statistics include settling and operator waits; these are not density fits.",
            "statistics": {key: {"samples": len(v), "missing": total - len(v),
                                 "mean": mean(v) if v else None,
                                 "std": stdev(v) if len(v) > 1 else None,
                                 "min": min(v) if v else None, "max": max(v) if v else None}
                           for key, v in values.items()}}


CALCULATIONS = {
    "viscosity": {"label": "Viscosity", "version": 2, "calculate": viscosity.calculate,
                  "check": viscosity.check, "files": ("protocol.json", "events.jsonl"),
                  "references": {"calibration": "gravimetry", "reference": "viscosity"},
                  "description": "Pressure/flow resistance; same-path reference comparison for viscosity."},
    "dead_volume": {"label": "Dead volume", "version": 1, "calculate": dead_volume.calculate,
                    "check": dead_volume.check, "files": ("protocol.json", "events.jsonl"),
                    "references": {"calibration": "gravimetry"},
                    "description": "Integrate calibrated flow between observed marker injection and arrival."},
    "gravimetry": {"label": "Gravimetry", "version": 2, "calculate": gravimetry.calculate,
                   "check": gravimetry.check, "files": ("protocol.json", "events.jsonl"),
                   "references": {"density": "oil_density"},
                   "description": "Before/after weights: calibration curve, per-rate repeatability and direction differences."},
    "oil_density": {"label": "Oil density", "version": 2, "calculate": _density,
                    "files": ("protocol.json", "events.jsonl"),
                    "description": "Two height passes from a density protocol; excludes scout and settling."},
    "recording_summary": {"label": "Recording summary", "version": 1, "calculate": _recording_summary,
                          "files": (),
                          "description": "Measured pressure/flow statistics and missing-sample counts."},
}

CALCULATIONS["flow_scout"] = {
    "label": "Flow stability scout", "version": 3, "calculate": analyze_scout,
    "files": ("protocol.json", "events.jsonl"),
    "description": "Single-height flow sweep: settling, averaging windows and pressure/flow fit precision.",
}


def calculate_run(directory, calculation_id, *, references=None):
    calculation = CALCULATIONS.get(calculation_id)
    if calculation is None:
        raise ValueError("Unknown calculation")
    context = load_context(directory, calculation_id, calculation, references)
    directory = context["directory"]
    inputs = fingerprint(context["paths"], directory)
    context = load_context(directory, calculation_id, calculation, references)
    if fingerprint(context["paths"], directory) != inputs:
        raise ValueError("Recording changed during calculation; refresh and try again")
    summary = context["summary"]
    result = calculation["calculate"](context)
    if fingerprint(context["paths"], directory) != inputs:
        raise ValueError("Recording changed during calculation; refresh and try again")
    payload = {"calculation_id": calculation_id, "calculation_version": calculation["version"],
               "created_at": datetime.now(timezone.utc).isoformat(), "plan_id": summary.get("plan_id"),
               "run_id": protocol_run_id(summary),
               "inputs": inputs, "measurement_revision": context["measurements"]["revision"],
               "references": {key: value["path"] for key, value in context["references"].items()}, "result": result}
    path = directory / "calculations" / f"{calculation_id}_{uuid.uuid4().hex}.json"
    write_json(path, payload)
    return {"path": str(path), **payload}


def saved_results(directory):
    if not directory:
        return []
    entries = []
    for path in (Path(directory) / "calculations").glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(payload.get("result"), dict)
                    or not isinstance(payload.get("calculation_id"), str)
                    or not isinstance(payload.get("created_at"), str)):
                continue
            payload = {**payload, "path": str(path)}
            entries.append({**payload, "outdated": not result_current(payload)})
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return sorted(entries, key=lambda result: result.get("created_at", ""), reverse=True)


def result_text(payload):
    result = payload["result"]
    kind = payload["calculation_id"]
    def number(value):
        return f"{value:.5g}" if value is not None else "unavailable"

    if kind in {"gravimetry", "dead_volume", "viscosity"}:
        lines = [f"{CALCULATIONS[kind]['label']}: {result['status']}"]
        if kind == "gravimetry":
            lines += [f"Recorded-flow multiplier: {number(result['multiplier'])}",
                      "STEP | MASS-DERIVED (µL) | RECORDED (µL) | MULTIPLIER"]
            lines += [f"{s['step']} | {number(s['true_volume_ul'])} | {number(s['recorded_volume_ul'])} | "
                      f"{number(s['multiplier'])}" for s in result["samples"]]
            if result.get("targets"):
                fit = result.get("flow_fit") or {}
                lines += [f"Flow-curve R²: {number(fit.get('r_squared'))}",
                          f"True Q = {number(fit.get('slope'))} × recorded Q + {number(fit.get('intercept'))} µL/min",
                          "TARGET | TRUE Q ± REPEAT SD (µL/min) | N | FACTOR | UP/DOWN Δ (%)"]
                for row in result["targets"]:
                    stats = row["true_flow"]
                    lines.append(f"{number(row['target_ul_min'])} | {number(stats['mean'])} ± {number(stats['sd'])} | "
                                 f"{stats['repeats']} | {number(row['multiplier'])} | "
                                 f"{number(row['up_down_difference_percent'])}")
                    interval = row["true_flow_repeat_ci95_ul_min"]
                    if interval:
                        lines.append(f"  Repeatability 95% CI: {number(interval[0])}–{number(interval[1])} µL/min")
        elif kind == "dead_volume":
            lines += [f"Effective displacement volume: {number(result['volume_ul'])} µL",
                      "STEP | TRANSIT (s) | VOLUME (µL) | TIMING UNCERTAINTY (µL)"]
            lines += [f"{s['step']} | {number(s.get('transit_s'))} | {number(s['volume_ul'])} | "
                      f"{number(s['timing_uncertainty_ul'])}" for s in result["samples"]]
        else:
            lines += [f"Hydraulic resistance: {number(result['resistance_mbar_min_ul'])} mbar·min/µL",
                      f"Relative viscosity: {number(result['relative_viscosity'])}",
                      f"Absolute viscosity: {number(result['viscosity_mpa_s'])} mPa·s",
                      "PASS | RESISTANCE (mbar·min/µL) | P0 (mbar) | R² | SLOPE SE"]
            lines += [f"{p['pass']} | {number(p['resistance_mbar_min_ul'])} | {number(p['intercept_mbar'])} | "
                      f"{number(p['r_squared'])} | {number(p['slope_standard_error'])}" for p in result["passes"]]
            lines += ["STEP | FLOW (µL/min) | PRESSURE (mbar)"]
            lines += [f"{s['step']} | {number(s['flow_ul_min'])} | {number(s['pressure_mbar'])}"
                      for s in result["samples"]]
        if "repeat_statistics" in result:
            stats = result["repeat_statistics"]
            lines += [f"Repeat SD: {number(stats['sd'])}; SEM: {number(stats['sem'])} (repeatability only)"]
        lines += ["Total uncertainty: not established", result["note"], *result["issues"],
                  "Thresholds: " + json.dumps(result["thresholds"])]
    elif kind == "oil_density":
        value = result.get("density_g_ml")
        lines = [f"Density: {value:.4f} g/mL" if value is not None else "Density: inconclusive"]
        for entry in result.get("passes", []):
            density = entry.get("density_g_ml")
            lines.append(f"Pass {entry['pass']}: " + (f"{density:.4f} g/mL" if density is not None else "unavailable"))
        difference = result.get("repeat_difference_percent")
        if difference is not None:
            lines.append(f"Pass disagreement: {difference:.2f}%")
        lines.append(result.get("note", ""))
        lines.extend(result.get("issues", []))
        lines.extend("Warning: " + warning for warning in result.get("warnings", []))
    elif payload["calculation_id"] == "flow_scout":
        lines = ["Flow scout: " + result["status"], result["note"]]
        recommendation = result.get("recommendation")
        if recommendation:
            lines += ["Suggested flows (µL/min): " + ", ".join(f"{v:g}" for v in recommendation["targets_ul_min"]),
                      f"Settling: {recommendation['settling_s']:g} s; averaging: {recommendation['averaging_s']:g} s",
                      "Operator approval required; no density protocol is started automatically."]
        lines += ["\nWINDOW (s) | FLOWS (µL/min) | P0 SD (mbar, forward/reverse) | RESULT"]
        for fit in result["fits"]:
            flows = ", ".join(f"{q:g}" for q in fit["usable_targets_ul_min"])
            errors = "/".join(f"{p['p0_bootstrap_sd_mbar']:.3f}" for p in fit["passes"]) or "unavailable"
            lines.append(f"{fit['averaging_s']:g} | {flows or 'none'} | {errors} | "
                         + ("; ".join(fit["issues"]) or "usable"))
        lines += ["Warning: " + warning for warning in result.get("warnings", [])]
        lines += ["\nThresholds: " + json.dumps(result["thresholds"], indent=2), *result["issues"]]
    else:
        lines = [result.get("note", ""), json.dumps(result, indent=2, ensure_ascii=False)]
    return ("OUTDATED: saved inputs have changed; calculate again.\n\n" if payload.get("outdated") else "") + "\n".join(lines) + "\n\nSaved: " + payload["path"]
