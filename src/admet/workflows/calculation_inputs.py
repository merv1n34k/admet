"""Read-only run inputs, event windows and trace integration."""

import csv
import hashlib
import json
import math
import os
from pathlib import Path
from copy import deepcopy

from admet.core.compat import recording_path
from admet.workflows.calculation_schema import declarations
from admet.workflows.json_protocol import resolve, validate_measurement


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def fingerprint(paths, directory):
    return [{"path": os.path.relpath(path, directory), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in dict.fromkeys(paths)]


def input_path(directory, value):
    path = Path(value)
    normalized = str(value).replace("\\", "/")
    if path.is_absolute() or (len(normalized) > 2 and normalized[1:3] == ":/"):
        if "/records/" in normalized:
            candidate = directory.parents[2] / ("records/" + normalized.rsplit("/records/", 1)[1])
            if candidate.is_file():
                return candidate
        return path
    return directory / path


def result_current(payload):
    directory = Path(payload["path"]).parent.parent
    try:
        return bool(payload["inputs"]) and all(hashlib.sha256(input_path(directory, item["path"]).read_bytes()).hexdigest() == item["sha256"]
                   for item in payload["inputs"])
    except (OSError, KeyError, TypeError):
        return False


def load_context(directory, calculation_id, entry, references=None):
    directory = Path(directory).resolve()
    summary_path = directory / "summary.json"
    summary = read_json(summary_path)
    if summary.get("state") not in {"completed", "failed", "cancelled"}:
        raise ValueError("Wait until the recording has finished before calculating")
    if summary.get("artifacts", {}).get("recording_closed") is False:
        raise ValueError("This recording was not successfully closed; do not analyze an active file")
    csv_path = recording_path(directory, summary)
    paths = [summary_path, csv_path]
    document = None
    if "protocol.json" in entry["files"] or (directory / "protocol.json").is_file():
        document = read_json(directory / "protocol.json")
        paths.append(directory / "protocol.json")
    paths.extend(directory / name for name in entry["files"])
    config = next((item for item in declarations(document or {}) if item.get("type") == calculation_id), {})
    if document and "parameters" in document:
        resolved = resolve(document)
        config = next((item for item in declarations(resolved) if item["type"] == calculation_id), config)
    if calculation_id in {"gravimetry", "dead_volume", "viscosity"}:
        verify_execution(document, summary)
    measurements = {"values": {}, "fields": {}, "revision": None}
    measurement_path = directory / "measurements.json"
    if document and document.get("measurements"):
        measurements = read_json(measurement_path)
        if measurements["fields"] != document["measurements"]:
            raise ValueError("Saved measurement declarations disagree with executed protocol")
        paths.append(measurement_path)
        for key, field in measurements["fields"].items():
            value = measurements["values"].get(key)
            validate_measurement(value, field)
            if field.get("required") and value is None and calculation_id != "recording_summary":
                raise ValueError(f"Missing measurement: {field['label']}")
    if document and "calculations" in document and calculation_id in {"oil_density", "flow_scout"}:
        document = deepcopy(document)
        document.pop("calculations")
        document["analysis"] = config
    loaded_references = {}
    for role, value in (references or {}).items():
        expected = entry.get("references", {}).get(role)
        if expected is None:
            raise ValueError(f"Unsupported reference: {role}")
        path = Path(value).resolve()
        payload = {**read_json(path), "path": str(path)}
        if payload.get("calculation_id") != expected or not result_current(payload):
            raise ValueError(f"{role}: incompatible or outdated reference result")
        if payload["result"].get("status") not in {"complete", "consistent", "usable"}:
            raise ValueError(f"{role}: reference result is inconclusive")
        paths.append(path)
        paths.extend(input_path(path.parent.parent, item["path"]) for item in payload["inputs"])
        loaded_references[role] = payload
    return {"directory": directory, "summary": summary, "document": document, "csv": csv_path,
            "config": config, "measurements": measurements, "references": loaded_references,
            "paths": paths}


def verify_execution(document, summary):
    from admet.engines.acquisition.pipeline import expand_protocol_steps
    from admet.workflows.operations import _step_from

    resolved = resolve(document)
    expanded = expand_protocol_steps([_step_from(step, i) for i, step in enumerate(resolved["steps"])])
    recorded = summary.get("steps", [])
    if len(recorded) != len(expanded):
        raise ValueError("Recorded execution steps are missing or disagree with protocol")
    for saved, step in zip(recorded, expanded):
        expected = {"flow_setpoints_ul_min": {str(k): v for k, v in step.sensor_setpoints.items()},
                    "pressure_setpoints_mbar": {str(k): v for k, v in step.pressure_setpoints.items()},
                    "trigger_type": step.trigger_type, "trigger_params": step.trigger_params,
                    "timeout_s": step.timeout_s if step.timeout_s is not None else step.trigger_params.get("timeout_s"),
                    "on_complete": step.on_complete, "confirmation": step.confirm_message or None}
        if any(saved.get(key) != value for key, value in expected.items()):
            raise ValueError("Recorded execution and protocol parameters disagree")
    archived = summary.get("normalized_settings", {}).get("protocol")
    if archived is None or resolve(archived) != resolved:
        raise ValueError("Calculation declarations disagree with executed protocol")


def step_window(context, step, settle_s=0):
    if context["summary"]["state"] != "completed":
        raise ValueError("Protocol did not complete")
    origin = context["summary"].get("artifacts", {}).get("polling_origin_monotonic")
    if not isinstance(origin, (int, float)) or not math.isfinite(origin) or origin <= 0:
        raise ValueError("Missing recording clock origin")
    events = [json.loads(line) for line in (context["directory"] / "events.jsonl").read_text().splitlines() if line]
    events = [e for e in events if e.get("step_name") and e.get("step_index") == step - 1]
    starts = [e for e in events if e.get("outcome") == "running" and e.get("state") == "running"
              and not e.get("confirmation_message")]
    ends = [e for e in events if e.get("outcome") == "completed"]
    if (not starts or len(ends) != 1 or any(e.get("state") == "paused"
            or e.get("outcome") in {"skipped", "cancelled", "error"} for e in events)):
        raise ValueError(f"Step {step}: missing, paused, skipped or incomplete event window")
    start, end = starts[0].get("monotonic"), ends[0].get("monotonic")
    if any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end)) or end <= start + settle_s:
        raise ValueError(f"Step {step}: invalid timing")
    return start - origin + settle_s, end - origin


def trace(context, channel, lower, upper, *, pressure=False):
    columns = [f"flow_{channel}_ul_min"] + ([f"pressure_{channel}_mbar"] if pressure else [])
    rows = []
    with context["csv"].open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                t = float(row["elapsed_s"])
                values = [float(row.get(column, "")) for column in columns]
            except (ValueError, TypeError, KeyError):
                try:
                    t = float(row["elapsed_s"])
                except (ValueError, TypeError, KeyError):
                    raise ValueError("Recording has invalid timestamps") from None
                values = [math.nan] * len(columns)
            rows.append((t, *values))
    if any(not math.isfinite(row[0]) for row in rows) or any(b[0] <= a[0] for a, b in zip(rows, rows[1:])):
        raise ValueError("Recording timestamps are not strictly increasing")
    inside = [i for i, row in enumerate(rows) if lower <= row[0] <= upper]
    if not inside:
        raise ValueError("No recorded samples in measurement window")
    first, last = inside[0], inside[-1]
    if rows[first][0] > lower:
        first = max(0, first - 1)
    if rows[last][0] < upper:
        last = min(len(rows) - 1, last + 1)
    selected = rows[first:last + 1]
    if (selected[0][0] > lower or selected[-1][0] < upper
            or any(not all(math.isfinite(v) for v in row) for row in selected)
            or any(b[0] - a[0] > 1 for a, b in zip(selected, selected[1:]))):
        raise ValueError("Missing samples or insufficient coverage of measurement window")
    def interpolate(t):
        for a, b in zip(selected, selected[1:]):
            if a[0] <= t <= b[0]:
                return (t, *(x + (y - x) * (t - a[0]) / (b[0] - a[0]) for x, y in zip(a[1:], b[1:])))
        raise ValueError("Cannot align recording boundary")
    return [interpolate(lower), *(row for row in selected if lower < row[0] < upper), interpolate(upper)]


def volume_ul(rows):
    return sum((b[0] - a[0]) * (a[1] + b[1]) / 120 for a, b in zip(rows, rows[1:]))
