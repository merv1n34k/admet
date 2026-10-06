"""Offline calculations over archived runs, independent of the live service."""

from copy import deepcopy
from datetime import datetime, timezone
import csv
import json
from pathlib import Path
import re
from statistics import mean, stdev
import uuid

from admet.core.protocol_store import write_json
from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNEL_LABELS
from admet.workflows.fluid_density import _finite, analyze_density_run
from admet.workflows.flow_scout import analyze_scout
from admet.workflows.calculation_schema import declarations
from admet.workflows.calculation_inputs import load_context, fingerprint, result_current, read_json
from admet.workflows import gravimetry, dead_volume, viscosity


def available_calculations(directory):
    result = []
    try:
        document = read_json(Path(directory) / "protocol.json")
        result.extend(item["type"] for item in declarations(document) if item["type"] in CALCULATIONS)
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
    mapping = next(item for item in declarations(document) if item["type"] == "fluid_density")["points"]
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
                  "references": {"reference": "viscosity"},
                  "description": "Pressure/flow resistance; same-path reference comparison for viscosity."},
    "dead_volume": {"label": "Dead volume", "version": 3, "calculate": dead_volume.calculate,
                    "check": dead_volume.check, "files": ("protocol.json",),
                    "description": "Mean and spread of the dead volumes measured by hand."},
    "gravimetry": {"label": "Gravimetry", "version": 2, "calculate": gravimetry.calculate,
                   "check": gravimetry.check, "files": ("protocol.json", "events.jsonl"),
                   "references": {"density": "fluid_density"},
                   "description": "Before/after weights: calibration curve, per-rate repeatability and direction differences."},
    "fluid_density": {"label": "Fluid density", "version": 2, "calculate": _density,
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
               "run_id": summary["run_id"],
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


def _number(value, digits=5):
    return f"{value:.{digits}g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "—"


def _fixed(value, places=2):
    return f"{value:.{places}f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "—"


def _unit(text, unit):
    return text if text == "—" else f"{text} {unit}"


def _percent(value, places=1):
    return f"{100 * value:.{places}f} %" if isinstance(value, (int, float)) else "—"


def _table(title, headers, rows, highlight=None):
    return {"title": title, "headers": list(headers), "rows": [list(row) for row in rows], "highlight": highlight}


def _section(title, status=None, facts=(), tables=(), notes=(), warnings=()):
    return {"title": title, "status": status, "facts": [list(fact) for fact in facts], "tables": list(tables),
            "notes": list(notes), "warnings": list(warnings)}


def _line(slope, intercept):
    sign = "−" if intercept < 0 else "+"
    return f"true = {_number(slope)} × recorded {sign} {_number(abs(intercept), 4)} µL/min"


def _gravimetry_unit(channel, unit):
    title = f"{FLUIDIC_CHANNEL_LABELS[int(channel)]} (channel {channel})"
    fit = unit.get("flow_fit") or {}
    facts = [("Flow curve", f"{_line(fit['slope'], fit['intercept'])} · R² {_fixed(fit.get('r_squared'), 4)}"
              if fit.get("slope") is not None else "—")]
    tables = []
    suggestion = unit.get("suggested_correction")
    if suggestion:
        names = {"current": "Keep current", "scale": "Scale only", "square": "Scale + square"}
        order = [suggestion["recommended"], *(k for k in ("current", "scale", "square")
                                              if k in suggestion["options"] and k != suggestion["recommended"])]
        low, high = suggestion["range_ul_min"]
        tables.append(_table(
            f"Correction for the Rig table · {suggestion['table']} table · {_number(low, 4)}–{_number(high, 4)} µL/min",
            ("Option", "Scale", "Square (x²)", "Cube (x³)", "Worst error"),
            [((names[name] + " (suggested)") if name == suggestion["recommended"] else names[name],
              _number(o["scale"]), _number(o["square"]), _number(o["cube"]), _percent(o["worst_error"]))
             for name in order for o in [suggestion["options"][name]]], highlight=0))
    tables.append(_table("Collections", ("#", "Target µL/min", "Weighed µL", "Recorded µL", "Factor", "Note"),
                         [(s["step"], _number(s["target_ul_min"], 4), _fixed(s["true_volume_ul"], 1),
                           _fixed(s["recorded_volume_ul"], 1), _fixed(s["multiplier"], 4),
                           f"left out: {s['left_out']}" if s.get("left_out") else "; ".join(s["issues"]))
                          for s in unit["samples"]]))
    if unit.get("targets"):
        tables.append(_table("Per flow", ("Target µL/min", "True flow µL/min", "N", "Factor", "CV", "Up/down", "95 % CI µL/min"),
                             [(_number(t["target_ul_min"], 4),
                               f"{_fixed(t['true_flow']['mean'])} ± {_fixed(t['true_flow']['sd'])}",
                               t["true_flow"]["repeats"], _fixed(t["multiplier"], 4), _percent(t["repeat_cv"]),
                               _percent(t["up_down_difference_percent"] / 100 if t["up_down_difference_percent"] is not None else None),
                               "–".join(_fixed(v) for v in t["true_flow_repeat_ci95_ul_min"])
                               if t["true_flow_repeat_ci95_ul_min"] else "—")
                              for t in unit["targets"]]))
    return _section(title, unit["status"], facts, tables, unit["issues"])


def result_view(payload):
    """A saved result as sections of facts, tables and notes, for showing rather than reading as text."""
    result, kind = payload["result"], payload["calculation_id"]
    label = CALCULATIONS.get(kind, {}).get("label", kind)
    sections = []
    if kind == "gravimetry":
        sections.append(_section(label, result["status"],
                                 [("Liquid", result.get("liquid", "—")),
                                  ("Density", _unit(_fixed(result.get('density_g_ml'), 4), "g/mL"))]))
        sections += [_gravimetry_unit(channel, unit) for channel, unit in result["units"].items()]
    elif kind == "fluid_density":
        passes = {entry["pass"]: entry for entry in result.get("passes", [])}
        sections.append(_section(label, result["status"],
                                 [("Fluid", result.get("fluid_id", "—")),
                                  ("Density", _unit(_fixed(result.get('density_g_ml'), 4), "g/mL")),
                                  ("Passes differ", _percent((result.get("repeat_difference_percent") or 0) / 100)
                                   if result.get("repeat_difference_percent") is not None else "—")],
                                 [_table("Passes", ("Pass", "Density g/mL", "R²"),
                                         [(n, _fixed(e.get("density_g_ml"), 4), _fixed(e.get("r_squared"), 4))
                                          for n, e in sorted(passes.items())]),
                                  _table("Points", ("Step", "Pass", "Height cm", "Flow µL/min", "Pressure mbar",
                                                    "Flow SD", "Pressure drift", "Issue"),
                                         [(p.get("step"), p.get("pass"), _number(p.get("height_cm"), 3),
                                           _fixed(p.get("flow_mean_ul_min")), _fixed(p.get("pressure_mean_mbar")),
                                           _fixed(p.get("flow_std_ul_min")), _fixed(p.get("pressure_drift_mbar")),
                                           "; ".join(p.get("issues", [])))
                                          for p in result.get("points", [])])],
                                 result.get("issues", []), result.get("warnings", [])))
    elif kind == "dead_volume":
        interval = result.get("ci95_ul") or [None, None]
        sections.append(_section(label, result["status"],
                                 [("Dead volume", f"{_fixed(result.get('volume_ul'), 1)} ± {_fixed(result.get('sd_ul'), 1)} µL"),
                                  ("CV", _percent(result.get("cv"))),
                                  ("95 % CI", f"{_fixed(interval[0], 1)}–{_fixed(interval[1], 1)} µL")],
                                 [_table("Entered", ("#", "Volume µL"),
                                         [(i + 1, _fixed(v, 1)) for i, v in enumerate(result.get("volumes_ul", []))])],
                                 result.get("issues", [])))
    elif kind == "viscosity":
        sections.append(_section(label, result["status"],
                                 [("Resistance", _unit(_number(result.get('resistance_mbar_min_ul')), "mbar·min/µL")),
                                  ("Relative viscosity", _number(result.get("relative_viscosity"))),
                                  ("Viscosity", _unit(_number(result.get('viscosity_mpa_s')), "mPa·s"))],
                                 [_table("Passes", ("Pass", "Resistance mbar·min/µL", "P0 mbar", "R²", "Slope SE"),
                                         [(p["pass"], _number(p["resistance_mbar_min_ul"]), _fixed(p["intercept_mbar"]),
                                           _fixed(p["r_squared"], 4), _number(p["slope_standard_error"], 3))
                                          for p in result.get("passes", [])]),
                                  _table("Points", ("Step", "Flow µL/min", "Pressure mbar"),
                                         [(s["step"], _fixed(s["flow_ul_min"]), _fixed(s["pressure_mbar"]))
                                          for s in result.get("samples", [])])],
                                 result.get("issues", []), result.get("warnings", [])))
    elif kind == "flow_scout":
        recommendation = result.get("recommendation")
        facts = [("Suggested flows", ", ".join(f"{v:g}" for v in recommendation["targets_ul_min"]) + " µL/min"),
                 ("Settling / averaging", f"{recommendation['settling_s']:g} s / {recommendation['averaging_s']:g} s")] \
            if recommendation else [("Suggested flows", "—")]
        sections.append(_section(label, result["status"], facts,
                                 [_table("Averaging windows", ("Window s", "Usable flows µL/min", "P0 SD mbar (fwd / rev)", "Result"),
                                         [(f"{fit['averaging_s']:g}",
                                           ", ".join(f"{q:g}" for q in fit["usable_targets_ul_min"]) or "none",
                                           " / ".join(f"{p['p0_bootstrap_sd_mbar']:.3f}" for p in fit["passes"]) or "—",
                                           "; ".join(fit["issues"]) or "usable")
                                          for fit in result.get("fits", [])])],
                                 result.get("issues", []), result.get("warnings", [])))
    elif kind == "recording_summary":
        sections.append(_section(label, result["status"], [("Rows", result.get("rows", "—"))],
                                 [_table("Columns", ("Column", "Samples", "Missing", "Mean", "SD", "Min", "Max"),
                                         [(name, stats["samples"], stats["missing"], _number(stats["mean"]),
                                           _number(stats["std"]), _number(stats["min"]), _number(stats["max"]))
                                          for name, stats in result.get("statistics", {}).items()])]))
    else:
        sections.append(_section(label, result.get("status"), [(key, str(value)) for key, value in result.items()
                                                               if not isinstance(value, (dict, list))]))
    return {"outdated": bool(payload.get("outdated")), "saved": payload.get("path", ""),
            "note": result.get("note", ""), "sections": sections}
