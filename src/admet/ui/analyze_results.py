"""Stored analysis results, summarised per file for the Results stage, and their export."""

from __future__ import annotations

import csv
import html
import io
import json
import math
import random
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any, Callable

from admet.ui import theme


@dataclass(frozen=True)
class StoredRun:
    project_path: Path
    run_id: str
    raw_path: Path
    jobs: tuple[dict[str, Any], ...]
    # Each file's settings when it was analysed (crop, frames, ...).
    matrix: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class FileResult:
    """One file's stored analysis: the numbers the Results stage shows and plots.

    Distributions hold one value per droplet (the median over the frames it was seen
    in), so a droplet seen for forty frames counts once, like one seen for four.
    """

    project: str
    sample_id: str
    engine: str
    source_path: str
    file_id: str
    analyzed_at: str
    settings: dict[str, Any]
    fps: float
    scale: float                     # µm per px
    frames: int
    droplets: int
    rate_hz: float
    speed_mm_s: float
    rate_note: str = ""                                    # how the rate was had, when not from the gate
    expected_hz: float = 0.0                               # from the dispersed flow and the droplet size
    dispersed_ul_min: float = 0.0
    arrivals_s: tuple[float, ...] = ()                     # each droplet's first sighting, s from the first frame
    diameters: tuple[float, ...] = ()                      # µm
    areas: tuple[float, ...] = ()                          # µm²
    periods_s: tuple[float, ...] = ()                      # between one droplet's arrival and the next
    speeds_mm_s: tuple[float, ...] = ()
    detections: tuple[tuple[float, float, float, float], ...] = ()   # frame, x px, y px, area µm²
    spans: tuple[tuple[float, float, float], ...] = ()               # droplet id, first frame, last frame
    inclusion_counts: tuple[int, ...] = ()
    detected_ul: tuple[tuple[float, float], ...] = ()      # s since the recording's first frame, µL so far
    delivered_ul: tuple[tuple[float, float], ...] = ()     # the same clock, what the flow units pushed (no oil)

    @property
    def detected_total_ul(self) -> float:
        return self.detected_ul[-1][1] if self.detected_ul else 0.0

    @property
    def delivered_total_ul(self) -> float:
        return self.delivered_ul[-1][1] if self.delivered_ul else 0.0

    @property
    def mean_diameter(self) -> float:
        return mean(self.diameters) if self.diameters else 0.0

    @property
    def std_diameter(self) -> float:
        return pstdev(self.diameters) if len(self.diameters) > 1 else 0.0

    @property
    def cv_percent(self) -> float:
        return self.std_diameter / self.mean_diameter * 100.0 if self.mean_diameter else 0.0

    @property
    def mean_area(self) -> float:
        return mean(self.areas) if self.areas else 0.0

    @property
    def std_area(self) -> float:
        return pstdev(self.areas) if len(self.areas) > 1 else 0.0

    @property
    def inclusions(self) -> int:
        return sum(self.inclusion_counts)


def load_stored_runs(project_paths: list[Path]) -> list[StoredRun]:
    runs = []
    for project_path in project_paths:
        metadata_path = project_path / "analysis" / "metadata.json"
        if not metadata_path.is_file():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for run in metadata.get("runs", []):
            if not isinstance(run, dict):
                continue
            raw_path = resolve_project_path(project_path, str(run.get("raw_path") or ""))
            run_metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
            jobs = tuple(job for job in run_metadata.get("jobs", ()) if isinstance(job, dict))
            settings = run.get("settings") if isinstance(run.get("settings"), dict) else {}
            runs.append(StoredRun(
                project_path=project_path,
                run_id=str(run.get("run_id") or raw_path.parent.name),
                raw_path=raw_path,
                jobs=jobs,
                matrix=tuple(row for row in settings.get("matrix", ()) if isinstance(row, dict)),
            ))
    return runs


def iter_raw_rows(project_path: Path, raw_path: Path, *, limit: int | None = None):
    """The rows of a raw file one at a time: a recording's rows run into the hundreds of thousands."""
    path = raw_path if raw_path.is_absolute() else project_path / raw_path
    if not path.is_file():
        return
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if limit is not None and count >= limit:
                return
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                count += 1
                yield value


def read_raw_rows(project_path: Path, raw_path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    return list(iter_raw_rows(project_path, raw_path, limit=limit))


PLOT_POINTS = 6000      # detections kept per file for the plots: a uniform sample, not the first ones


def file_results(run: StoredRun, rows, corrections: dict[tuple[str, str], dict[str, Any]] | None = None,
                 scales: dict[tuple[str, str], tuple[float, float]] | None = None,
                 deliveries: dict[tuple[str, str], Any] | None = None) -> list[FileResult]:
    """Each file's result from one run's rows, in one pass: distributions per droplet, rate and speed.

    Files are told apart by their id, never by the sample name two files may share. CORRECTIONS,
    by (engine, source path), are what the user changed by hand on imaging droplets: a droplet
    switched off is left out, an inclusion count is taken as corrected. SCALES, by the same key,
    are the µm per px and fps to read the rows with: the engine measures in pixels and frames,
    so changing either needs no new run. DELIVERIES, by the same key, are what the flow units pushed
    while the file was recorded, as (s, µL) points, read from the run's fluidics CSV.
    """
    jobs = _jobs_by_key(run)
    files: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = _row_key(row)
        kind = row.get("kind")
        acc = files.get(key)
        if acc is None:
            job = jobs.get(key, {})
            source = str(resolve_project_path(run.project_path, str(job.get("source_path") or "")))
            acc = files[key] = {"context": {}, "per_droplet": {}, "loose_d": [], "loose_a": [], "points": [],
                                "seen": 0, "inclusions": [], "frames": set(), "um": False, "job": job,
                                "source": source, "rng": random.Random(0),
                                "fixes": (corrections or {}).get((key[0], source), {}),
                                "scale": tuple((scales or {}).get((key[0], source), (0.0, 0.0))),
                                "delivery": tuple(tuple(p) for p in (deliveries or {}).get((key[0], source), ()))}
        if kind == "run_context":
            acc["context"] = _values(row)
            continue
        if kind not in {"detection", "droplet", "track"}:
            continue
        values = _values(row)
        frame = _num(values.get("frame"))
        if frame is not None:
            acc["frames"].add(int(frame))
        fix = acc["fixes"].get(correction_key(frame, values.get("droplet_id"))) if kind == "droplet" and acc["fixes"] \
            else None
        if fix and fix.get("disabled"):
            continue
        # Pixels first, so the scale of the day applies; µm only when the row has nothing else.
        diameter = _num(values.get("diameter_px", values.get("equivalent_diameter", values.get("diameter"))))
        area = _num(values.get("area_px", values.get("area")))
        if diameter is None and area is None:
            diameter, area = _num(values.get("diameter_um")), _num(values.get("area_um2"))
            if diameter is not None or area is not None:
                acc["um"] = True
        if kind == "droplet":
            acc["inclusions"].append(corrected_inclusions(values, fix))
        cx = _num(values.get("centroid_x", values.get("center_x")))
        cy = _num(values.get("centroid_y", values.get("center_y")))
        if frame is not None and cx is not None and cy is not None:
            _reservoir(acc, (frame, round(cx, 1), round(cy, 1), area or 0.0))
        # A "droplet" row is one droplet in one image; its id only counts within that image,
        # so it is not followed across frames the way a tracked detection is.
        droplet_id = None if kind == "droplet" else _num(
            values.get("droplet_id") if values.get("droplet_id") is not None else values.get("track_id"))
        if droplet_id is None:
            if diameter:
                acc["loose_d"].append(diameter)
            if area:
                acc["loose_a"].append(area)
            continue
        entry = acc["per_droplet"].setdefault(int(droplet_id), {"d": [], "a": [], "f": [], "x": []})
        if diameter:
            entry["d"].append(diameter)
        if area:
            entry["a"].append(area)
        if frame is not None:
            entry["f"].append(frame)
            entry["x"].append(cx or 0.0)
    return [_finish(run, key, acc) for key, acc in sorted(files.items())]


def _reservoir(acc: dict[str, Any], point: tuple[float, float, float, float]) -> None:
    """Keep PLOT_POINTS detections spread evenly over the file, whatever its length."""
    acc["seen"] += 1
    if len(acc["points"]) < PLOT_POINTS:
        acc["points"].append(point)
        return
    slot = acc["rng"].randrange(acc["seen"])
    if slot < PLOT_POINTS:
        acc["points"][slot] = point


def _finish(run: StoredRun, key: tuple[str, str], acc: dict[str, Any]) -> FileResult:
    engine = key[0]
    job, context = acc["job"], acc["context"]
    analysis = _as_dict(context.get("analysis"))
    meta = _as_dict(job.get("metadata"))
    scale = acc["scale"][0] or _num(analysis.get("microns_per_pixel")) or 1.0
    fps = acc["scale"][1] or _num(analysis.get("fps")) or 0.0
    to_um = 1.0 if acc["um"] else scale                  # engine rows in px are scaled; µm rows are kept
    diameters = [median(e["d"]) * to_um for e in acc["per_droplet"].values() if e["d"]] \
        + [d * to_um for d in acc["loose_d"]]
    areas = [median(e["a"]) * to_um * to_um for e in acc["per_droplet"].values() if e["a"]] \
        + [a * to_um * to_um for a in acc["loose_a"]]
    spans: list[tuple[float, float, float]] = []
    arrivals: list[float] = []
    speeds: list[float] = []
    for droplet_id, entry in sorted(acc["per_droplet"].items()):
        if not entry["f"]:
            continue
        order = sorted(range(len(entry["f"])), key=lambda i: entry["f"][i])
        first, last = entry["f"][order[0]], entry["f"][order[-1]]
        spans.append((float(droplet_id), first, last))
        arrivals.append(first)
        if last > first and fps and scale:
            travel = abs(entry["x"][order[-1]] - entry["x"][order[0]])
            speeds.append(travel / (last - first) * scale * fps / 1000.0)
    arrivals.sort()
    periods = [(b - a) / fps for a, b in zip(arrivals, arrivals[1:]) if fps and b > a]
    arrival_rate = ((len(arrivals) - 1) / ((arrivals[-1] - arrivals[0]) / fps)
                    if fps and len(arrivals) > 1 and arrivals[-1] > arrivals[0] else 0.0)
    generation = _as_dict(context.get("generation"))
    crossings = int(_num(generation.get("crossings")) or 0)
    period_frames = _num(generation.get("period_frames")) or 0.0
    gate_rate = (fps / period_frames if fps and period_frames else
                 _num(generation.get("rate_hz")) or _num(meta.get("frequency_hz")) or _num(context.get("frequency_hz")))
    rate_note = ""
    method = str(generation.get("method") or "")
    if gate_rate and (method == "spacing" or crossings >= len(arrivals) / 2 or not arrival_rate):
        rate = gate_rate
        rate_note = "speed over spacing" if method == "spacing" else ""
    elif arrival_rate:
        # The gate saw too few of the droplets to be trusted (it sits where tracks start or end):
        # the rate is then droplets seen per second of analysed video.
        rate = arrival_rate
        rate_note = f"from arrivals; gate saw {crossings} of {len(arrivals)}" if gate_rate else "from arrivals"
    else:
        rate = 1.0 / mean(periods) if periods else 0.0
    speed_px = _num(context.get("speed_px_frame")) or 0.0
    speed = speed_px * scale * fps / 1000.0 if speed_px and fps else (mean(speeds) if speeds else 0.0)
    dispersed = float(acc["scale"][2]) if len(acc["scale"]) > 2 and acc["scale"][2] else 0.0
    expected = expected_rate_hz(dispersed, median(diameters)) if dispersed and diameters else 0.0
    detected_ul = detected_volume(acc["per_droplet"], to_um, fps) if fps else ()
    analyzed_at = str(meta.get("analyzed_at") or "")
    points = sorted(acc["points"])
    settings = _run_settings(run, job, analysis, _as_dict(context.get("detection")))
    settings.update({"microns_per_pixel": scale, "fps": fps} if engine != "cellpose" else {"px_to_um": scale})
    return FileResult(
        project=run.project_path.name,
        sample_id=str(job.get("sample_id") or key[1]),
        engine=engine,
        source_path=acc["source"],
        file_id=str(job.get("file_id") or key[1]),
        analyzed_at=local_time(analyzed_at) if analyzed_at else "",
        settings=settings,
        fps=fps,
        scale=scale,
        frames=len(acc["frames"]),
        droplets=(len(acc["loose_d"]) if acc["fixes"] else
                  int(_num(meta.get("total_droplets") or meta.get("droplet_count")) or 0)
                  or len(acc["per_droplet"]) or len(acc["loose_d"])),
        rate_hz=float(rate or 0.0),
        speed_mm_s=float(speed or 0.0),
        rate_note=rate_note,
        expected_hz=expected,
        dispersed_ul_min=dispersed,
        arrivals_s=tuple((a - arrivals[0]) / fps for a in arrivals) if arrivals and fps else (),
        diameters=tuple(diameters),
        areas=tuple(areas),
        periods_s=tuple(periods),
        speeds_mm_s=tuple(speeds),
        detections=tuple((f, x, y, round(a * to_um * to_um, 2)) for f, x, y, a in points),
        spans=tuple(spans),
        inclusion_counts=tuple(acc["inclusions"]),
        detected_ul=detected_ul,
        delivered_ul=acc["delivery"],
    )


def detected_volume(per_droplet: dict[Any, dict[str, list]], to_um: float, fps: float) -> tuple[tuple[float, float], ...]:
    """The volume found so far, droplet by droplet in order of first sighting: each a sphere of its
    median diameter, as (s since the recording's first frame, µL)."""
    droplets = sorted((min(e["f"]), median(e["d"]) * to_um) for e in per_droplet.values() if e["f"] and e["d"])
    points, total = [], 0.0
    for frame, diameter in droplets:
        total += math.pi / 6 * diameter ** 3 * 1e-9                  # µm³ to µL
        points.append((round(frame / fps, 4), total))
    return tuple(points)


def dispersed_indices(record: dict[str, Any] | None) -> tuple[int, ...]:
    """The channels whose liquid becomes droplets, from a recording's record: every one but the oil."""
    return tuple(int(c["index"]) for c in (record or {}).get("channels") or []
                 if isinstance(c, dict) and "index" in c and "oil" not in str(c.get("label", "")).lower())


def fluidics_csv_path(project_path: Path, run_id: str) -> Path | None:
    """The fluidics CSV the run wrote, from the run's summary; None when the run or the file is not there."""
    if not run_id:
        return None
    summary = Path(project_path) / "records" / "protocols" / run_id / "summary.json"
    try:
        stored = json.loads(summary.read_text(encoding="utf-8")).get("artifacts", {}).get("fluidics_csv")
    except (OSError, ValueError, AttributeError):
        return None
    if not stored:
        return None
    path = resolve_project_path(Path(project_path), str(stored))
    return path if path.is_file() else None


def delivered_volume(csv_path: Path, started_at: str, stopped_at: str,
                     channels: tuple[int, ...], *, max_points: int = 400) -> tuple[tuple[float, float], ...]:
    """What the flow units pushed through the given channels while a recording ran, from the run's
    fluidics CSV: (s since the recording started, µL since then), from the volume counters.

    The CSV keeps the time of day; the recording's record keeps the date too, and both come from
    the same clock. A recording across midnight is handled."""
    start, stop = _time_of_day(started_at), _time_of_day(stopped_at)
    if start is None or not channels:
        return ()
    if stop is None or stop < start:
        stop = (stop if stop is not None else -1.0) + 86400.0 if stop is not None and stop < start else 10 ** 9
    columns = [f"volume_{index}_ul" for index in channels]
    points: list[tuple[float, float]] = []
    origin = None
    try:
        with open(csv_path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                seconds = _time_of_day(row.get("timestamp", ""))
                if seconds is None:
                    continue
                if seconds < start - 43200:            # past midnight, the day after the recording started
                    seconds += 86400.0
                if seconds < start:
                    continue
                if seconds > stop:
                    break
                try:
                    total = sum(float(row[column]) for column in columns)
                except (KeyError, ValueError):
                    continue
                if origin is None:
                    origin = total
                points.append((round(seconds - start, 3), round(total - origin, 4)))
    except OSError:
        return ()
    if len(points) > max_points:
        step = len(points) // max_points + 1
        points = points[::step] + [points[-1]]
    return tuple(points)


def _time_of_day(stamp: str) -> float | None:
    """Seconds since midnight of an ISO stamp with a date, or of a bare HH:MM:SS.mmm."""
    text = str(stamp or "").strip()
    if not text:
        return None
    clock = text.split("T", 1)[1] if "T" in text else text
    try:
        hours, minutes, seconds = clock.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None


def settings_summary(settings: dict[str, Any]) -> str:
    """The settings a result was made with, in one line."""
    parts = []
    for key, label, unit in (("microns_per_pixel", "µm/px", ""), ("fps", "fps", ""), ("k_sigma", "k", "")):
        if settings.get(key) not in {None, ""}:
            parts.append(f"{label} {_fmt(settings[key])}{unit}")
    if settings.get("start_frame") is not None or settings.get("end_frame"):
        parts.append(f"frames {int(_num(settings.get('start_frame')) or 0)}–{_fmt(settings.get('end_frame') or '')}")
    if settings.get("roi_width") or settings.get("roi_height"):
        parts.append(f"crop {int(_num(settings.get('roi_x')) or 0)},{int(_num(settings.get('roi_y')) or 0)} "
                     f"{int(_num(settings.get('roi_width')) or 0)}×{int(_num(settings.get('roi_height')) or 0)}")
    for key, label in (("skip_left_pct", "skip left"), ("skip_right_pct", "skip right"), ("gate_pct", "gate")):
        if settings.get(key) not in {None, ""}:
            parts.append(f"{label} {_fmt(settings[key])} %")
    for key, label, unit in (("satellite_px", "satellite", " px"), ("min_area", "min area", " px²"),
                             ("px_to_um", "px→µm", ""), ("frame_limit", "frame limit", "")):
        if settings.get(key) not in {None, ""}:
            parts.append(f"{label} {_fmt(settings[key])}{unit}")
    return " · ".join(parts)


def correction_key(frame: Any, droplet_id: Any) -> str:
    """How a correction names one imaging droplet: its image and its number in that image."""
    return f"{int(_num(frame) or 0)}:{int(_num(droplet_id) or 0)}"


def corrected_inclusions(values: dict[str, Any], fix: dict[str, Any] | None) -> int:
    count = int(_num(values.get("inclusions")) or 0)
    return max(0, count + int(_num((fix or {}).get("inclusions")) or 0))


def corrections_summary(fixes: dict[str, Any]) -> dict[str, int]:
    """How many droplets were switched off and how many inclusions were added or removed."""
    deltas = [int(_num(fix.get("inclusions")) or 0) for fix in fixes.values() if not fix.get("disabled")]
    return {"disabled": sum(1 for fix in fixes.values() if fix.get("disabled")),
            "added": sum(d for d in deltas if d > 0), "removed": -sum(d for d in deltas if d < 0)}


# -- the per-file table ----------------------------------------------------------------

def spark(values, width: int = 72, height: int = 16, bins: int = 24) -> str:
    """A tiny histogram of the values as inline SVG; no axes, just the shape."""
    import numpy as np

    array = np.asarray(list(values), dtype=float)
    array = array[np.isfinite(array)]
    if len(array) < 2:
        return ""
    low, high = np.percentile(array, (0.5, 99.5))
    if high <= low:
        low, high = array.min() - 0.5, array.max() + 0.5
    counts, _ = np.histogram(np.clip(array, low, high), bins=bins, range=(low, high))
    top = counts.max() or 1
    bar = width / bins
    rects = "".join(
        f'<rect x="{i * bar:.1f}" y="{height - height * c / top:.1f}" width="{bar - 0.4:.1f}" '
        f'height="{height * c / top:.1f}"/>'
        for i, c in enumerate(counts) if c)
    return f'<svg class="spark" width="{width}" height="{height}">{rects}</svg>'


RESULT_FIELDS = ("sample", "droplets", "inclusions", "diameter", "area", "cv", "rate", "volume", "speed", "analysed")
RESULT_LABELS = {"sample": "Sample", "droplets": "Droplets", "inclusions": "Inclusions", "diameter": "Diameter, µm",
                 "area": "Area, µm²", "cv": "CV, %", "rate": "Rate, /s", "volume": "Volume, µL",
                 "speed": "Speed, mm/s", "analysed": "Analysed"}


def dispersed_flow_ul_min(record: dict[str, Any] | None) -> float:
    """The flow that becomes droplets, from a recording's record: every flow-controlled channel
    but the oil, in µL/min; 0 when the record says nothing."""
    total = 0.0
    for channel in (record or {}).get("channels") or []:
        if not isinstance(channel, dict) or "oil" in str(channel.get("label", "")).lower():
            continue
        total += float(channel.get("flow_ul_min") or 0.0)
    return total


def expected_rate_hz(dispersed_ul_min: float, diameter_um: float) -> float:
    """Droplets a second the flow must make at this size: the dispersed flow over a sphere's volume."""
    if dispersed_ul_min <= 0 or diameter_um <= 0:
        return 0.0
    volume_um3 = math.pi / 6 * diameter_um ** 3
    return dispersed_ul_min / 60 * 1e9 / volume_um3


def results_cells(result: FileResult, color: str, *, active: bool = True) -> dict[str, str]:
    """One file's row of the results table, each cell as HTML: the number with its distribution."""
    off = "" if active else " results-off"

    def cell(inner: str) -> str:
        return f'<div class="results-cell{off}">{inner}</div>'

    with_inclusions = sum(1 for count in result.inclusion_counts if count)
    summary = settings_summary(result.settings)
    return {
        "sample": cell(f'<span class="swatch" style="background:{color}"></span>{html.escape(result.sample_id)}'
                       f'<div class="sub">{html.escape(result.engine)}</div>'),
        "droplets": cell(f'{result.droplets}<div class="sub">{result.frames} frames</div>'),
        "inclusions": cell(f'{result.inclusions}<div class="sub">in {with_inclusions} droplets</div>'
                           + spark(result.inclusion_counts)) if result.inclusion_counts else cell("—"),
        "diameter": cell(_distribution(result.mean_diameter, result.std_diameter, result.diameters, 1)),
        "area": cell(_distribution(result.mean_area, result.std_area, result.areas, 0)),
        "cv": cell(f"{result.cv_percent:.1f}" if result.diameters else "—"),
        "rate": cell((_distribution(result.rate_hz, 0.0, [1.0 / p for p in result.periods_s if p > 0], 1)
                      + (f'<div class="sub" title="{html.escape(result.rate_note)}">{html.escape(result.rate_note)}</div>'
                         if result.rate_note else "")
                      + (f'<div class="sub">expected {result.expected_hz:.0f} from {result.dispersed_ul_min:g} µL/min'
                         '</div>' if result.expected_hz else "")) if result.rate_hz else "—"),
        "volume": cell(_volume_cell(result)),
        "speed": cell(_distribution(result.speed_mm_s, 0.0, result.speeds_mm_s, 2) if result.speed_mm_s else "—"),
        "analysed": cell(f'{html.escape(result.analyzed_at or "earlier")}'
                         f'<div class="sub" title="{html.escape(summary)}">{html.escape(_short(summary, 48))}</div>'),
    }


def _volume_cell(result: FileResult) -> str:
    """Found in the droplets, and how much of what the flow units delivered that is."""
    if not result.detected_ul:
        return "—"
    found = f'<span class="n">{result.detected_total_ul:.3g}</span>'
    if not result.delivered_ul:
        return found + '<div class="sub">found; no fluidics record</div>'
    delivered = result.delivered_total_ul
    share = f"{100 * result.detected_total_ul / delivered:.0f} %" if delivered else "—"
    return found + f'<div class="sub">of {delivered:.3g} delivered, {share}</div>'


def results_table_html(results: list[FileResult]) -> str:
    """The results as one HTML table (what the per-file rows hold, without the Use column)."""
    if not results:
        return ""
    fields = [f for f in RESULT_FIELDS if f != "inclusions" or any(r.inclusion_counts for r in results)]
    head = "".join(f"<th>{html.escape(RESULT_LABELS[f])}</th>" for f in fields)
    body = "".join("<tr>" + "".join(f"<td>{results_cells(r, chart_color(i))[f]}</td>" for f in fields) + "</tr>"
                   for i, r in enumerate(results))
    return f'<table class="results"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _distribution(value: float, sd: float, samples, digits: int) -> str:
    if not value and not samples:
        return "—"
    text = f"{value:.{digits}f} ± {sd:.{digits}f}" if sd else f"{value:.{digits}f}"
    return f'<span class="n">{text}</span>{spark(samples)}'


# -- plots -----------------------------------------------------------------------------

def chart_color(index: int) -> str:
    return theme.CHART_COLORS[index % len(theme.CHART_COLORS)]


def short_names(names: list[str], *, keep: int = 12) -> dict[str, str]:
    """Each name without the start they all share, so plots say what differs between the samples.

    Recordings are named run_<id>_step01_<stamp>: with the run id gone, step01 and its stamp remain.
    """
    distinct = list(dict.fromkeys(names))
    if len(distinct) < 2:
        return {name: name for name in names}
    prefix = 0
    while all(len(name) > prefix and name[prefix] == distinct[0][prefix] for name in distinct):
        prefix += 1
    prefix = distinct[0][:prefix].rfind("_") + 1                    # cut at a word, not mid-way through one
    prefix = min(prefix, min(len(name) for name in distinct) - keep)
    if prefix < 4:
        return {name: name for name in names}
    return {name: name[prefix:] for name in names}


def _names(results: list[FileResult]) -> dict[str, str]:
    return short_names([result.sample_id for result in results])


def histogram_edges(values: list[float], *, max_bins: int = 40, min_bins: int = 8) -> list[float]:
    """Bin edges from the data's spread (Freedman–Diaconis), so a tight distribution still has shape."""
    import numpy as np

    array = np.asarray(values, dtype=float)
    low, high = float(array.min()), float(array.max())
    if high <= low:
        low, high = low - 0.5, high + 0.5
    q1, q3 = np.percentile(array, (25, 75))
    width = 2 * (q3 - q1) * len(array) ** (-1 / 3)
    bins = int(math.ceil((high - low) / width)) if width > 0 else max_bins
    bins = max(min_bins, min(max_bins, bins))
    return [low + (high - low) * i / bins for i in range(bins + 1)]


def bin_labels(edges: list[float]) -> list[str]:
    """Bin centres written to the precision the bin width needs, so neighbours read differently."""
    width = edges[1] - edges[0] if len(edges) > 1 else 1.0
    digits = max(0, -math.floor(math.log10(width))) if width > 0 else 0
    return [f"{(a + b) / 2:.{digits}f}" for a, b in zip(edges, edges[1:])]


def diameter_chart(results: list[FileResult]) -> dict[str, Any]:
    plotted = [result for result in results if result.diameters]
    values = [value for result in plotted for value in result.diameters]
    if not values:
        return _empty_chart("Diameter distribution (µm)")
    edges = histogram_edges(values)
    labels = bin_labels(edges)
    width = edges[1] - edges[0]
    names = _names(results)
    series = []
    for index, result in enumerate(plotted):
        counts = [0] * len(labels)
        for value in result.diameters:
            counts[min(len(labels) - 1, int((value - edges[0]) / width))] += 1
        series.append({"name": names[result.sample_id], "type": "bar", "data": counts, "barMaxWidth": 18,
                       "itemStyle": {"color": chart_color(index), "opacity": 0.82},
                       "emphasis": {"focus": "series"}})
    return {
        "title": _title("Diameter distribution (µm)"),
        "animation": False,
        "tooltip": {"trigger": "axis"},
        "legend": _legend(),
        "grid": _grid(bottom=58),
        "xAxis": {"type": "category", "data": labels,
                  "axisLabel": {"rotate": 45, "fontSize": 10, "hideOverlap": True}},
        "yAxis": _value_axis("droplets"),
        "series": series,
    }


def timeline_chart(results: list[FileResult]) -> dict[str, Any]:
    """Droplets counted against time since each file's first frame: the slope is the rate.

    Files start at different frames of different recordings, so each is laid from its own
    start; a steady train is a straight line, a change of rate a bend, a stop a plateau.
    """
    names = _names(results)
    series = []
    for index, result in enumerate(result for result in results if result.arrivals_s):
        points = [[round(t, 4), n] for n, t in enumerate(result.arrivals_s, start=1)]
        if len(points) > 4000:
            step = len(points) // 4000 + 1
            points = points[::step] + [points[-1]]
        series.append({"name": names[result.sample_id], "type": "line", "showSymbol": False, "silent": True,
                       "data": points, "lineStyle": {"width": 2, "color": chart_color(index)},
                       "itemStyle": {"color": chart_color(index)}})
    return {
        "title": _title("Droplets over time"),
        "animation": False,
        "tooltip": {"trigger": "axis"},
        "legend": _legend(),
        "grid": _grid(right=28),
        "xAxis": _value_axis("s since the first frame"),
        "yAxis": _value_axis("droplets"),
        "series": series,
    }


def volume_chart(results: list[FileResult]) -> dict[str, Any]:
    """The volume the flow units delivered against the volume found in the droplets, over the recording:
    two lines a file on one clock. Agreeing slopes mean every droplet was seen at its true size."""
    names = _names(results)
    series = []
    for index, result in enumerate(result for result in results if result.detected_ul or result.delivered_ul):
        color = chart_color(index)
        if result.delivered_ul:
            series.append({"name": f"{names[result.sample_id]} delivered", "type": "line", "showSymbol": False,
                           "silent": True, "data": [[t, round(v, 5)] for t, v in result.delivered_ul],
                           "lineStyle": {"width": 2, "type": "dashed", "color": color}, "itemStyle": {"color": color}})
        if result.detected_ul:
            points = [[t, round(v, 5)] for t, v in result.detected_ul]
            if len(points) > 4000:
                step = len(points) // 4000 + 1
                points = points[::step] + [points[-1]]
            series.append({"name": f"{names[result.sample_id]} detected", "type": "line", "showSymbol": False,
                           "silent": True, "data": points, "lineStyle": {"width": 2, "color": color},
                           "itemStyle": {"color": color}})
    return {
        "title": _title("Volume: delivered vs detected"),
        "animation": False,
        "tooltip": {"trigger": "axis"},
        "legend": _legend(),
        "grid": _grid(right=28),
        "xAxis": _value_axis("s since the recording started"),
        "yAxis": _value_axis("µL"),
        "series": series,
    }


def positions_chart(results: list[FileResult]) -> dict[str, Any]:
    return scatter_chart("Droplet positions (px)", "x", "y", results, lambda d: [d[1], d[2]], invert_y=True)


def area_x_chart(results: list[FileResult]) -> dict[str, Any]:
    return scatter_chart("Area along x (µm²)", "x px", "µm²", results, lambda d: [d[1], d[3]] if d[3] else None)


def scatter_chart(title: str, xname: str, yname: str, results: list[FileResult],
                  project: Callable[[tuple[float, float, float, float]], list[float] | None],
                  *, invert_y: bool = False) -> dict[str, Any]:
    names = _names(results)
    series = []
    for index, result in enumerate(results[:8]):
        data = [point for point in (project(detection) for detection in result.detections) if point is not None]
        if data:
            series.append({"name": names[result.sample_id], "type": "scatter", "symbolSize": 3, "data": data,
                           "itemStyle": {"color": chart_color(index)}})
    return {
        "title": _title(title),
        "animation": False,
        "tooltip": {},
        "legend": _legend(),
        "grid": _grid(),
        "xAxis": _value_axis(xname),
        "yAxis": _value_axis(yname, inverse=invert_y),
        "series": series,
    }


def cv_chart(results: list[FileResult]) -> dict[str, Any]:
    """One bar per sample: the spread of its droplet diameters, as CV in percent."""
    plotted = [result for result in results if result.diameters]
    names = _names(results)
    return {
        "title": _title("CV of diameter per sample (%)"),
        "animation": False,
        "tooltip": {"trigger": "axis"},
        "grid": _grid(bottom=48),
        "xAxis": {"type": "category", "data": [names[result.sample_id] for result in plotted],
                  "axisLabel": {"rotate": 25, "fontSize": 10, "hideOverlap": True, "width": 90,
                                "overflow": "truncate"}},
        "yAxis": _value_axis("CV %"),
        "series": [{"type": "bar", "barMaxWidth": 36,
                    "data": [{"value": round(result.cv_percent, 2), "itemStyle": {"color": chart_color(index)}}
                             for index, result in enumerate(plotted)],
                    "label": {"show": True, "position": "top", "fontSize": 10, "formatter": "{c} %"}}],
    }


def inclusions_chart(results: list[FileResult]) -> dict[str, Any]:
    """How many droplets of each sample hold 0, 1, 2 ... inclusions; empty for samples without any."""
    keys = sorted({count for result in results for count in result.inclusion_counts})
    names = _names(results)
    series = []
    for index, result in enumerate(results):
        if not result.inclusion_counts:
            continue
        counts = {key: 0 for key in keys}
        for value in result.inclusion_counts:
            counts[value] += 1
        series.append({"name": names[result.sample_id], "type": "bar", "barMaxWidth": 24,
                       "data": [counts[key] for key in keys], "itemStyle": {"color": chart_color(index)}})
    return {
        "title": _title("Inclusions per droplet"),
        "animation": False,
        "tooltip": {"trigger": "axis"},
        "legend": _legend(),
        "grid": _grid(bottom=44),
        "xAxis": {"type": "category", "data": [str(key) for key in keys], "name": "inclusions",
                  "nameLocation": "middle", "nameGap": 22, "axisLabel": {"fontSize": 10}},
        "yAxis": _value_axis("droplets"),
        "series": series,
    }


def has_inclusions(results: list[FileResult]) -> bool:
    return any(result.inclusion_counts for result in results)


def result_charts(results: list[FileResult]) -> list[tuple[str, dict[str, Any]]]:
    """The plots of the Results stage, named for export: only those the ticked files have data for.

    A video has tracks, positions and areas along the channel; an image folder has inclusions;
    both have diameters. A plot with nothing to show is not shown.
    """
    builders = (("diameter_distribution", diameter_chart), ("cv_per_sample", cv_chart),
                ("droplet_timeline", timeline_chart), ("volume_delivered_vs_detected", volume_chart),
                ("droplet_positions", positions_chart),
                ("area_along_x", area_x_chart), ("inclusions_per_droplet", inclusions_chart))
    charts = []
    for name, build in builders:
        chart = build(results)
        if any(series.get("data") for series in chart.get("series", ())):
            charts.append((name, chart))
    return charts


def _title(text: str) -> dict[str, Any]:
    return {"text": text, "left": 8, "top": 4, "textStyle": {"fontSize": 13}}


def _legend() -> dict[str, Any]:
    """Under the plot, scrolling, each name cut to fit: a sample name can be a whole file name."""
    return {"bottom": 0, "left": 8, "right": 8, "type": "scroll", "itemWidth": 12, "itemHeight": 8,
            "textStyle": {"fontSize": 9, "width": 120, "overflow": "truncate"}}


def _grid(*, left: int = 58, right: int = 18, top: int = 36, bottom: int = 60) -> dict[str, Any]:
    return {"left": left, "right": right, "top": top, "bottom": bottom, "containLabel": True}


def _value_axis(name: str, *, inverse: bool = False) -> dict[str, Any]:
    axis: dict[str, Any] = {"type": "value", "name": name, "nameLocation": "middle",
                            "nameGap": 34 if name else 22, "axisLabel": {"fontSize": 10, "hideOverlap": True}}
    if inverse:
        axis["inverse"] = True
    return axis


def _empty_chart(title: str) -> dict[str, Any]:
    return {"title": _title(title), "grid": _grid(), "xAxis": {"type": "category", "data": []},
            "yAxis": _value_axis(""), "series": []}


# -- export ----------------------------------------------------------------------------

def export_csv_zip(runs: list[StoredRun], results: list[FileResult],
                   corrections: dict[tuple[str, str], dict[str, Any]] | None = None) -> bytes:
    """A zip: summary.csv over the results, and every droplet row of each file as its own CSV.

    Imaging droplets switched off by hand are left out; corrected inclusion counts are written
    with the raw count beside them.
    """
    wanted = {(result.project, result.engine, result.source_path): result.sample_id for result in results}
    files: dict[str, bytes] = {"summary.csv": _summary_csv(results)}
    for run in runs:
        jobs = _jobs_by_key(run)
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in iter_raw_rows(run.project_path, run.raw_path):
            if row.get("kind") in {"detection", "droplet", "track", "droplet_geometry"}:
                groups.setdefault(_row_key(row), []).append(row)
        for key, rows in sorted(groups.items()):
            engine = key[0]
            source = str(resolve_project_path(run.project_path, str(jobs.get(key, {}).get("source_path") or "")))
            sample_id = wanted.get((run.project_path.name, engine, source))
            if sample_id is None:
                continue
            fixes = (corrections or {}).get((engine, source), {})
            files[f"droplets/{_safe(run.project_path.stem)}_{_safe(sample_id)}_{engine}.csv"] = _rows_csv(rows, fixes)
    return zip_bytes(files)


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _summary_csv(results: list[FileResult]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["project", "sample", "engine", "source", "analysed_at", "frames", "droplets", "inclusions",
                     "mean_diameter_um", "sd_diameter_um", "cv_percent", "mean_area_um2", "sd_area_um2",
                     "rate_hz", "expected_hz", "dispersed_ul_min", "detected_ul", "delivered_ul", "speed_mm_s",
                     "fps", "um_per_px", "settings"])
    for result in results:
        writer.writerow([result.project, result.sample_id, result.engine, result.source_path, result.analyzed_at,
                         result.frames, result.droplets, result.inclusions,
                         _fmt(result.mean_diameter, 3), _fmt(result.std_diameter, 3), _fmt(result.cv_percent, 3),
                         _fmt(result.mean_area, 2), _fmt(result.std_area, 2), _fmt(result.rate_hz, 3),
                         _fmt(result.expected_hz, 1), _fmt(result.dispersed_ul_min, 2),
                         _fmt(result.detected_total_ul, 4), _fmt(result.delivered_total_ul, 4),
                         _fmt(result.speed_mm_s, 3), _fmt(result.fps, 3), _fmt(result.scale, 4),
                         settings_summary(result.settings)])
    return out.getvalue().encode("utf-8")


def _rows_csv(rows: list[dict[str, Any]], fixes: dict[str, Any] | None = None) -> bytes:
    keys: list[str] = []
    for row in rows:
        for key in _values(row):
            if key not in keys and not key.startswith("contour"):
                keys.append(key)
    if fixes and "inclusions" in keys:
        keys.append("inclusions_raw")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["kind", *keys])
    for row in rows:
        values = dict(_values(row))
        if fixes and row.get("kind") == "droplet":
            fix = fixes.get(correction_key(values.get("frame"), values.get("droplet_id")))
            if fix and fix.get("disabled"):
                continue
            if fix:
                values["inclusions_raw"] = values.get("inclusions")
                values["inclusions"] = corrected_inclusions(values, fix)
        writer.writerow([row.get("kind", ""), *(_cell(values.get(key)) for key in keys)])
    return out.getvalue().encode("utf-8")


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value)
    return "" if value is None else value


# -- small helpers ---------------------------------------------------------------------

def resolve_project_path(project_path: Path, stored_path: str) -> Path:
    path = Path(stored_path)
    return path if path.is_absolute() else project_path / path


def local_time(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return stamp


def _row_key(row: dict[str, Any]) -> tuple[str, str]:
    """A row's file: its id, or the sample it was written for when rows carry no file id."""
    return (str(row.get("engine") or ""),
            str(row.get("file_id") or row.get("item_id") or row.get("sample_id") or "sample"))


def _jobs_by_key(run: StoredRun) -> dict[tuple[str, str], dict[str, Any]]:
    jobs: dict[tuple[str, str], dict[str, Any]] = {}
    for job in run.jobs:
        engine = str(job.get("engine") or "")
        jobs.setdefault((engine, str(job.get("sample_id") or "sample")), job)
        if job.get("file_id"):
            jobs[(engine, str(job["file_id"]))] = job
    return jobs


def _run_settings(run: StoredRun, job: dict[str, Any], analysis: dict[str, Any],
                  detection: dict[str, Any]) -> dict[str, Any]:
    file_id = str(job.get("file_id") or "")
    settings = dict(next((_as_dict(entry.get("settings")) for entry in run.matrix
                          if entry.get("file_id") == file_id), {}))
    for key in ("microns_per_pixel", "fps"):
        if analysis.get(key) not in {None, ""}:
            settings[key] = analysis[key]
    for key, name in (("k_sigma", "k_sigma"), ("satellite", "satellite_px"), ("min_area", "min_area"),
                      ("skip_left_pct", "skip_left_pct"), ("skip_right_pct", "skip_right_pct"),
                      ("gate_pct", "gate_pct")):
        if detection.get(key) not in {None, ""}:
            settings.setdefault(name, detection[key])
    return settings


def _values(row: dict[str, Any]) -> dict[str, Any]:
    values = row.get("values")
    return values if isinstance(values, dict) else {}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt(value: Any, digits: int = 2) -> str:
    number = _num(value)
    if number is None:
        return str(value)
    if number == int(number) and digits <= 2:
        return str(int(number))
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value) or "file"
