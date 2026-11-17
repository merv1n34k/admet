"""Review runs: recorded pressure and flow from saved CSVs, run over run. Reads files only."""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from datetime import datetime

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from admet.core.droplets import dispersed_channels, expected_rate_hz, time_of_day
from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNEL_LABELS
from admet.ui import theme as ui
from admet.ui.tables import GridTable, fit_table_height
from admet.ui.theme import Theme
from admet.workflows.calculation_inputs import recording_path
from admet.workflows.calculations import recorded_runs

# Runs are told apart by colour, as series are in analyze; channels by line style.
# Oil L solid, Cells M long dashes, Beads M dash-dot; dash lengths are in line widths.
CHANNEL_DASHES = (None, (5.0, 2.5), (5.0, 2.0, 0.01, 2.0))
LINE_WIDTH = 2.5
# The droplet QC a recording wrote beside its video: the CV in the colour of its state.
QC_STATE_COLORS = {"STABLE": Theme.SUCCESS_HOVER, "UNSTABLE": Theme.DANGER_HOVER, "SETTLING": Theme.WARNING}
QC_LIMIT_PERCENT = 5.0


def channel_pen(color: str, channel: int) -> QPen:
    pen = QPen(QColor(color), LINE_WIDTH)
    pen.setCosmetic(True)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    if CHANNEL_DASHES[channel]:
        pen.setDashPattern(list(CHANNEL_DASHES[channel]))
    return pen


def channel_icon(channel: int) -> QIcon:
    """A short sample of the channel's line, for its toggle."""
    image = QPixmap(QSize(34, 12) * 2)
    image.setDevicePixelRatio(2)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(channel_pen(Theme.TEXT_WHITE, channel))
    painter.drawLine(3, 6, 31, 6)
    painter.end()
    return QIcon(image)


def run_color(row: int) -> str:
    return ui.CHART_COLORS[row % len(ui.CHART_COLORS)]


@dataclass
class QCSeries:
    """The run's droplet QC rows on the fluidics clock: one row a burst, each from one recording."""
    t: np.ndarray
    rate: np.ndarray
    rate_half: np.ndarray
    cv: np.ndarray
    cv_half: np.ndarray
    diameter: np.ndarray
    diameter_unit: str
    count: np.ndarray
    state: list[str]
    recording: list[str]
    expected: np.ndarray            # from the flows at that moment and the size then; NaN when unknown

    def chunks(self, mask: np.ndarray) -> list[np.ndarray]:
        """The indices in mask, one array per recording, in order."""
        indices = np.flatnonzero(mask)
        pieces: list[list[int]] = []
        for index in indices:
            if pieces and self.recording[pieces[-1][-1]] == self.recording[index]:
                pieces[-1].append(int(index))
            else:
                pieces.append([int(index)])
        return [np.array(piece) for piece in pieces]


@dataclass
class ReviewRun:
    name: str
    t: np.ndarray
    pressures: list[np.ndarray]
    flows: list[np.ndarray]
    markers: list[tuple[float, str, str]] = field(default_factory=list)
    qc: QCSeries | None = None

    @property
    def anchor(self) -> float:
        """Where the run is aligned: its first protocol step, else its first recorded row."""
        starts = [at for at, label, _color in self.markers if label == "▶ 1"]
        return starts[0] if starts else (float(self.t[0]) if len(self.t) else 0.0)


ALIGN_MODES = (("first_run", "To first run"), ("stacked", "Stacked"), ("recorded", "Recorded time"))


def segments(run: ReviewRun) -> list[tuple[float, float]]:
    """Each protocol step as (start, end), from its ▶ to its ■; a run without steps is one segment."""
    starts = {label[2:]: at for at, label, _color in run.markers if label.startswith("▶ ")}
    pieces = [(starts[label[2:]], at) for at, label, _color in run.markers
              if label.startswith("■ ") and label[2:] in starts and at > starts[label[2:]]]
    if pieces or not len(run.t):
        return pieces
    return [(float(run.t[0]), float(run.t[-1]))]


def placements(runs: list[ReviewRun], mode: str) -> list[list[tuple[float, float, float]]]:
    """For each run, the parts to draw as (from, to, seconds to add).

    first_run: whole runs, each moved so its first step lands on the first run's, on that run's clock.
    stacked: every step of every run on its own, starting at 0, so steps and runs overlay.
    recorded: whole runs as recorded, in seconds since polling began.
    """
    whole = (-math.inf, math.inf)
    if mode == "stacked":
        return [[(start, end, -start) for start, end in segments(run)] for run in runs]
    if mode == "first_run" and runs:
        return [[(*whole, runs[0].anchor - run.anchor)] for run in runs]
    return [[(*whole, 0.0)] for run in runs]


def read_fluidics_csv(path: Path) -> tuple[np.ndarray, list[np.ndarray], list[np.ndarray]]:
    """elapsed_s and every channel's pressure and flow, by channel number; missing or unreadable is NaN."""
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        if "elapsed_s" not in columns:
            raise ValueError(f"{Path(path).name}: no elapsed_s column")
        channels = range(len(FLUIDIC_CHANNEL_LABELS))
        rows = []
        for row in reader:
            rows.append([_number(row.get("elapsed_s"))]
                        + [_number(row.get(f"pressure_{i}_mbar")) for i in channels]
                        + [_number(row.get(f"flow_{i}_ul_min")) for i in channels])
    data = np.array(rows, dtype=float).reshape(-1, 1 + 2 * len(channels))
    data = data[np.isfinite(data[:, 0])]
    count = len(channels)
    return data[:, 0], [data[:, 1 + i] for i in range(count)], [data[:, 1 + count + i] for i in range(count)]


def _number(text) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return math.nan


def fluidics_anchor(path: Path) -> tuple[float, float] | None:
    """The first row's time of day and elapsed_s, which put a wall-clock stamp on the CSV's clock."""
    try:
        with Path(path).open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                stamp, elapsed = time_of_day(row.get("timestamp")), _number(row.get("elapsed_s"))
                if stamp is not None and math.isfinite(elapsed):
                    return stamp, elapsed
                return None
    except OSError:
        return None
    return None


def read_qc_csv(path: Path, anchor: tuple[float, float]) -> QCSeries | None:
    """The QC rows of a run on the fluidics clock: each row's wall-clock stamp against the fluidics
    CSV's first row. A row past midnight is placed on the next day."""
    anchor_stamp, anchor_elapsed = anchor
    columns: dict[str, list] = {key: [] for key in ("t", "rate", "rate_half", "cv", "cv_half", "diameter", "count")}
    states, recordings, units = [], [], []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            stamp = time_of_day(row.get("timestamp"))
            if stamp is None:
                continue
            if stamp < anchor_stamp - 43200:
                stamp += 86400.0
            columns["t"].append(anchor_elapsed + stamp - anchor_stamp)
            for key, column in (("rate", "rate_hz"), ("rate_half", "rate_half"), ("cv", "cv_percent"),
                                ("cv_half", "cv_half"), ("diameter", "diameter"), ("count", "count")):
                columns[key].append(_number(row.get(column)))
            states.append(str(row.get("state") or ""))
            recordings.append(str(row.get("recording_id") or ""))
            units.append(str(row.get("diameter_unit") or ""))
    if not columns["t"]:
        return None
    arrays = {key: np.array(values, dtype=float) for key, values in columns.items()}
    unit = next((u for u in units if u), "")
    return QCSeries(arrays["t"], arrays["rate"], arrays["rate_half"], arrays["cv"], arrays["cv_half"],
                    arrays["diameter"], unit, arrays["count"], states, recordings,
                    np.full(len(states), math.nan))


def expected_rates(qc: QCSeries, t: np.ndarray, flows: list[np.ndarray]) -> np.ndarray:
    """At each QC row, the rate the flows then measured must make at the size then: every channel
    but the oil over a sphere of the row's diameter. NaN until the size is in µm."""
    if qc.diameter_unit != "um" or not len(t):
        return np.full(len(qc.t), math.nan)
    dispersed = np.zeros(len(qc.t))
    for channel in dispersed_channels(FLUIDIC_CHANNEL_LABELS):
        if channel < len(flows):
            dispersed += np.interp(qc.t, t, np.nan_to_num(flows[channel]))
    return np.array([expected_rate_hz(float(q), float(d)) if q > 0 and d > 0 else math.nan
                     for q, d in zip(dispersed, qc.diameter)])


def step_markers(events: list[dict], origin: float) -> list[tuple[float, str, str]]:
    """Step starts and ends on the CSV's clock, marked as on the live plots."""
    markers, current = [], None
    for event in events:
        if not event.get("step_name") or type(event.get("monotonic")) not in (int, float):
            current = None
            continue
        at, number, outcome = event["monotonic"] - origin, int(event.get("step_index", 0)) + 1, event.get("outcome")
        if outcome == "running" and not event.get("confirmation_message"):
            if current != number:
                current = number
                markers.append((at, f"▶ {number}", Theme.SUCCESS_HOVER))
        elif outcome != "running":
            current = None
            markers.append((at, f"■ {number}", Theme.TEXT_MUTED if outcome == "completed" else Theme.DANGER_HOVER))
    return markers


def project_run(directory: Path, name: str) -> ReviewRun:
    directory = Path(directory)
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    fluidics = recording_path(directory, summary)
    t, pressures, flows = read_fluidics_csv(fluidics)
    origin = summary.get("artifacts", {}).get("polling_origin_monotonic")
    events = []
    if type(origin) in (int, float) and (directory / "events.jsonl").is_file():
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
                  if line.strip()]
    return ReviewRun(name, t, pressures, flows, step_markers(events, origin) if events else [],
                     qc=run_qc(directory, summary, fluidics, t, flows))


def run_qc(directory: Path, summary: dict, fluidics: Path, t: np.ndarray, flows: list[np.ndarray]) -> QCSeries | None:
    """The run's droplet QC, when it wrote one and the fluidics CSV carries the clock to place it."""
    stored = summary.get("artifacts", {}).get("qc_csv")
    if not isinstance(stored, str) or not stored:
        return None
    path = Path(stored) if Path(stored).is_absolute() else directory.parents[2] / stored
    anchor = fluidics_anchor(fluidics)
    if not path.is_file() or anchor is None:
        return None
    try:
        qc = read_qc_csv(path, anchor)
    except (OSError, ValueError):
        return None
    if qc is not None:
        qc.expected = expected_rates(qc, t, flows)
    return qc


def file_run(path: Path) -> ReviewRun:
    t, pressures, flows = read_fluidics_csv(path)
    return ReviewRun(Path(path).stem, t, pressures, flows)


def _bin(t: np.ndarray, y: np.ndarray, size: float) -> tuple[np.ndarray, np.ndarray]:
    keep = np.isfinite(y)
    t, y = t[keep], y[keep]
    if size <= 0 or len(t) < 2:
        return t, y
    index = ((t - t[0]) / size).astype(np.intp)
    counts = np.bincount(index)
    used = counts > 0
    return np.bincount(index, weights=t)[used] / counts[used], np.bincount(index, weights=y)[used] / counts[used]


COLUMNS = ("", "Colour", "Run", "Recorded", "State", "Duration", "Droplets", "Rate, /s", "CV, %")


class _Crosshair:
    """DropleGen's crosshair on one plot: dotted lines and a readout at the cursor, a pinned reference."""

    def __init__(self, pg, plot, unit: str):
        self.plot, self.unit = plot, unit
        dotted = pg.mkPen(Theme.TEXT_MUTED, width=1, style=Qt.PenStyle.DotLine)
        self.vline = pg.InfiniteLine(angle=90, pen=dotted)
        self.hline = pg.InfiniteLine(angle=0, pen=dotted)
        self.label = pg.TextItem(anchor=(0, 1), color=Theme.TEXT_WHITE)
        self.ref = None
        self.ref_marker = pg.ScatterPlotItem([], [], pen=pg.mkPen(Theme.WARNING), brush=pg.mkBrush(Theme.WARNING),
                                             size=10, symbol="+")
        self.ref_label = pg.TextItem(anchor=(0, 0), color=Theme.WARNING)
        for item in self.items():
            plot.addItem(item, ignoreBounds=True)
        for item in (self.vline, self.hline, self.label):
            item.setVisible(False)      # until the cursor is over a plot

    def items(self):
        return (self.vline, self.hline, self.label, self.ref_marker, self.ref_label)

    def view_point(self, scene_pos):
        box = self.plot.getPlotItem().vb
        return box.mapSceneToView(scene_pos) if box.sceneBoundingRect().contains(scene_pos) else None

    def hover(self, x: float, y: float | None) -> None:
        """At (x, y) on this plot; y None follows the time only, for the plot not under the cursor."""
        self.vline.setPos(x)
        self.vline.setVisible(True)
        self.hline.setVisible(y is not None)
        self.label.setVisible(y is not None)
        if y is None:
            return
        self.hline.setPos(y)
        text = f"t={x:.2f} s  {y:.2f} {self.unit}"
        if self.ref is not None:
            text += f"\nΔt={x - self.ref[0]:.2f} s  Δ={y - self.ref[1]:.2f} {self.unit}"
        self.label.setText(text)
        self.label.setPos(x, y)

    def pin(self, x: float, y: float) -> None:
        self.ref = (x, y)
        self.ref_marker.setData([x], [y])
        self.ref_label.setText(f"ref {x:.2f} s, {y:.2f} {self.unit}")
        self.ref_label.setPos(x, y)

    def clear(self) -> None:
        self.ref = None
        self.ref_marker.setData([], [])
        self.ref_label.setText("")


class ReviewPanel(QWidget):
    """The runs table on top, one row of view controls, then four plots in a grid sharing the time axis:
    pressure and the droplet CV above, flow and the droplet rate below, from the QC each recording wrote."""

    def __init__(self):
        super().__init__()
        import pyqtgraph as pg

        self._pg = pg
        self.project = None
        self._runs: dict[str, ReviewRun] = {}
        # key -> (kind, run name, recorded, state, duration): project runs, then files loaded by hand
        self._sources: dict[str, tuple[str, str, str, str, str]] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(ui.spacing("default"))

        self.table = GridTable(0, len(COLUMNS))
        self.table.setObjectName("RawConfigTable")
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(GridTable.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(GridTable.SelectionMode.NoSelection)
        self.table.itemChanged.connect(self._run_toggled)
        root.addWidget(self.table)

        actions = QHBoxLayout()
        actions.setSpacing(ui.spacing("control"))
        load = ui.button("Load file…")
        load.clicked.connect(self._load_files)
        actions.addWidget(load)
        reset = ui.button("Reset view")
        reset.clicked.connect(self._reset_view)
        actions.addWidget(reset)
        actions.addSpacing(ui.spacing("default"))
        actions.addWidget(QLabel("Bin"))
        self.bin = QDoubleSpinBox()
        self.bin.setRange(0.0, 60.0)
        self.bin.setSingleStep(0.5)
        self.bin.setSuffix(" s")
        self.bin.setSpecialValueText("off")
        self.bin.setMinimumWidth(110)
        self.bin.valueChanged.connect(self.redraw)
        actions.addWidget(self.bin)
        actions.addSpacing(ui.spacing("default"))
        actions.addWidget(QLabel("Align"))
        self.align = QComboBox()
        for value, label in ALIGN_MODES:
            self.align.addItem(label, value)
        self.align.setToolTip("Runs are aligned on their first protocol step; a loaded file on its first row.")
        self.align.currentIndexChanged.connect(self.redraw)
        actions.addWidget(self.align)
        actions.addStretch(1)
        root.addLayout(actions)

        channels = QHBoxLayout()
        channels.setSpacing(ui.spacing("default"))
        channels.addWidget(QLabel("Channels"))
        self.channel_boxes = []
        for index, label in enumerate(FLUIDIC_CHANNEL_LABELS):
            box = QCheckBox(label)
            box.setIcon(channel_icon(index))
            box.setIconSize(QSize(34, 12))
            box.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            box.setChecked(True)
            box.toggled.connect(self.redraw)
            channels.addWidget(box)
            self.channel_boxes.append(box)
        channels.addStretch(1)
        root.addLayout(channels)

        self.pressure_plot = self._plot("Pressure", "mbar")
        self.flow_plot = self._plot("Flow", "µL/min")
        self.droplet_plot = self._plot("Droplets", "/s")
        self.cv_plot = self._plot("Droplet CV", "%")
        for plot in (self.flow_plot, self.droplet_plot, self.cv_plot):
            plot.setXLink(self.pressure_plot)
        limit = pg.InfiniteLine(QC_LIMIT_PERCENT, angle=0, pen=pg.mkPen(Theme.DANGER, width=1, style=Qt.PenStyle.DashLine),
                                label=f"{QC_LIMIT_PERCENT:g} %", labelOpts={"position": 0.02, "color": Theme.DANGER})
        self.cv_plot.addItem(limit)                                  # in the range, so the limit is always in view
        self.crosshairs = {self.pressure_plot: _Crosshair(pg, self.pressure_plot, "mbar"),
                           self.flow_plot: _Crosshair(pg, self.flow_plot, "µL/min"),
                           self.droplet_plot: _Crosshair(pg, self.droplet_plot, "/s"),
                           self.cv_plot: _Crosshair(pg, self.cv_plot, "%")}
        grid = QGridLayout()
        grid.setSpacing(ui.spacing("default"))
        # A legend a plot: a run's curves by name, the CV's states once; it can be dragged off a curve.
        self.legends = {plot: plot.getPlotItem().addLegend(offset=(10, 10), labelTextColor=Theme.TEXT_WHITE)
                        for plot in self.crosshairs}
        for state, color in QC_STATE_COLORS.items():
            self.legends[self.cv_plot].addItem(pg.PlotDataItem(pen=pg.mkPen(color, width=LINE_WIDTH)), state)
        self.legends[self.cv_plot].addItem(pg.PlotDataItem(pen=limit.pen), f"{QC_LIMIT_PERCENT:g} % limit")
        placed = ((self.pressure_plot, 0, 0), (self.cv_plot, 0, 1), (self.flow_plot, 1, 0), (self.droplet_plot, 1, 1))
        for plot, row, column in placed:                      # all four in view at once
            plot.scene().sigMouseMoved.connect(self._hover)
            plot.scene().sigMouseClicked.connect(lambda event, plot=plot: self._click(plot, event))
            grid.addWidget(plot, row, column)
        root.addLayout(grid)
        self.status = QLabel()
        self.status.setObjectName("MutedText")
        root.addWidget(self.status)

    def _plot(self, title: str, unit: str):
        plot = self._pg.PlotWidget(title=title, labels={"left": unit, "bottom": "s"})
        plot.setBackground(Theme.BG_DARK)
        plot.showGrid(x=True, y=True, alpha=0.15)
        plot.setMinimumHeight(260)
        return plot

    def set_project(self, project) -> None:
        if project == self.project and self.table.rowCount():
            return
        self.project = project
        self._runs.clear()
        self._sources = {key: value for key, value in self._sources.items() if value[0] == "file"}
        self.refresh()

    def refresh(self) -> None:
        """List the project's recorded runs, newest first, then any files loaded by hand."""
        checked = set(self.checked_keys())
        project = {}
        for run in recorded_runs(self.project):
            project[run["directory"]] = ("project", run["name"], run["at"][:16].replace("T", " "), run["state"],
                                         _duration(Path(run["directory"])))
        self._sources = project | {k: v for k, v in self._sources.items() if v[0] == "file"}
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._sources))
        for row, (key, (kind, name, recorded, state, duration)) in enumerate(self._sources.items()):
            tick = QTableWidgetItem()
            tick.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            tick.setCheckState(Qt.CheckState.Checked if key in checked else Qt.CheckState.Unchecked)
            tick.setData(Qt.ItemDataRole.UserRole, key)
            self.table.setItem(row, 0, tick)
            for column, text in enumerate(("■", name, recorded or "—", state, duration, "", "", ""), 1):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip(key)
                self.table.setItem(row, column, item)
            swatch = self.table.item(row, 1)
            swatch.setForeground(QColor(run_color(row)))
            swatch.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.blockSignals(False)
        fit_table_height(self.table, max_rows=10)
        self.redraw()

    def checked_keys(self) -> list[str]:
        return [self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in range(self.table.rowCount())
                if self.table.item(row, 0) and self.table.item(row, 0).checkState() == Qt.CheckState.Checked]

    def _load_files(self) -> None:
        paths, _filter = QFileDialog.getOpenFileNames(self, "Load fluidics CSV", "", "CSV files (*.csv)")
        self.add_files(paths)

    def add_files(self, paths) -> None:
        keys = [str(Path(path).resolve()) for path in paths]
        for key in keys:
            modified = datetime.fromtimestamp(Path(key).stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            self._sources.setdefault(key, ("file", Path(key).name, modified, "file", ""))
        self.refresh()
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) in keys:
                self.table.item(row, 0).setCheckState(Qt.CheckState.Checked)

    def _run_toggled(self, item) -> None:
        if item.column() != 0:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked and key not in self._runs:
            try:
                kind, name, *_rest = self._sources[key]
                self._runs[key] = project_run(Path(key), name) if kind == "project" else file_run(Path(key))
            except (OSError, ValueError, KeyError) as exc:
                self.table.blockSignals(True)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.table.blockSignals(False)
                self.status.setText(f"Could not read {self._sources.get(key, (None, key))[1]}: {exc}")
                return
        self.redraw()

    def shown_runs(self) -> list[tuple[str, ReviewRun]]:
        return [(key, self._runs[key]) for key in self.checked_keys() if key in self._runs]

    def redraw(self, *_args) -> None:
        pg = self._pg
        for plot in self.crosshairs:
            for item in list(plot.listDataItems()) + [i for i in plot.items()
                                                     if getattr(i, "step_marker", False) or getattr(i, "qc_item", False)]:
                plot.removeItem(item)
            if plot is not self.cv_plot:
                self.legends[plot].clear()                    # the CV legend is the states, drawn once
        shown, points = self.shown_runs(), 0
        rows = {self.table.item(row, 0).data(Qt.ItemDataRole.UserRole): row for row in range(self.table.rowCount())}
        mode = self.align.currentData()
        parts = placements([run for _key, run in shown], mode)
        for (key, run), pieces in zip(shown, parts):
            color = run_color(rows[key])
            for plot, series in ((self.pressure_plot, run.pressures), (self.flow_plot, run.flows)):
                for channel, values in enumerate(series):
                    if not self.channel_boxes[channel].isChecked():
                        continue
                    name = f"{run.name} · {FLUIDIC_CHANNEL_LABELS[channel]}"     # one entry a run, whatever the mode
                    for start, end, shift in pieces:
                        inside = (run.t >= start) & (run.t <= end)
                        t, y = _bin(run.t[inside] + shift, values[inside], self.bin.value())
                        if not len(t):
                            continue
                        points += len(t)
                        curve = plot.plot(t, y, pen=channel_pen(color, channel), name=name)
                        name = None
                        curve.setDownsampling(auto=True, method="peak")
                        curve.setClipToView(True)
            if run.qc is not None:
                points += self._draw_qc(run.qc, pieces, color, run.name)
            if mode == "stacked":
                continue    # every step starts at 0; its curve ends where the step ended
            shift = pieces[0][2]
            for plot in self.crosshairs:
                for at, label, mark in run.markers:
                    line = pg.InfiniteLine(at + shift, pen=pg.mkPen(mark, width=1, style=Qt.PenStyle.DashLine),
                                           label=label if plot is self.flow_plot else None,
                                           labelOpts={"position": 0.92, "color": mark})
                    line.step_marker = True
                    plot.addItem(line)
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            key = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if key not in self._runs:
                continue
            run = self._runs[key]
            if not self._sources[key][4]:
                self.table.item(row, 5).setText(f"{run.t[-1] - run.t[0]:.0f} s" if len(run.t) > 1 else "")
            for column, text in zip((6, 7, 8), qc_cells(run.qc)):
                self.table.item(row, column).setText(text)
        self.table.blockSignals(False)
        bin_text = "off" if self.bin.value() <= 0 else f"{self.bin.value():g} s"
        hint = "Ctrl + wheel zooms · click pins a reference · double-click clears it"
        self.status.setText(f"{len(shown)} run(s) · {points:,} points · bin {bin_text} · {hint}"
                            if shown else "Tick a run to plot it.")

    def _draw_qc(self, qc: QCSeries, pieces: list[tuple[float, float, float]], color: str, name: str) -> int:
        """The rate with its interval and the expected rate, and the CV in the state's colour, one
        curve a recording so nothing is drawn across the gaps between them; the points drawn."""
        pg = self._pg
        points = 0
        rate_name, expected_name = f"{name} · rate ± 95 %", f"{name} · expected from the flows"
        for start, end, shift in pieces:
            for chunk in qc.chunks((qc.t >= start) & (qc.t <= end)):
                t = qc.t[chunk] + shift
                rate, half = qc.rate[chunk], qc.rate_half[chunk]
                known = np.isfinite(half)
                if known.any():
                    low = pg.PlotDataItem(t[known], (rate - half)[known])
                    high = pg.PlotDataItem(t[known], (rate + half)[known])
                    band = pg.FillBetweenItem(low, high, brush=pg.mkBrush(QColor(color).red(), QColor(color).green(),
                                                                         QColor(color).blue(), 45))
                    band.qc_item = True
                    self.droplet_plot.addItem(band)
                self.droplet_plot.plot(t, rate, pen=pg.mkPen(color, width=LINE_WIDTH), name=rate_name)
                rate_name = None
                expected = qc.expected[chunk]
                if np.isfinite(expected).any():
                    self.droplet_plot.plot(t, expected, pen=pg.mkPen(color, width=1.2, style=Qt.PenStyle.DashLine),
                                           connect="finite", name=expected_name)
                    expected_name = None
                points += len(t)
                # The CV, each stretch of one state in that state's colour, joined to the next.
                states = [qc.state[i] for i in chunk]
                begin = 0
                for i in range(1, len(chunk) + 1):
                    if i == len(chunk) or states[i] != states[begin]:
                        stop = min(i + 1, len(chunk))
                        pen = pg.mkPen(QC_STATE_COLORS.get(states[begin], Theme.TEXT_MUTED), width=LINE_WIDTH)
                        self.cv_plot.plot(t[begin:stop], qc.cv[chunk][begin:stop], pen=pen)
                        begin = i
        return points

    def _reset_view(self) -> None:
        for plot in self.crosshairs:
            plot.enableAutoRange()

    def _hover(self, pos) -> None:
        for plot, crosshair in self.crosshairs.items():
            point = crosshair.view_point(pos)
            if point is None:
                continue
            for other in self.crosshairs.values():
                other.hover(point.x(), point.y() if other is crosshair else None)
            return

    def _click(self, plot, event) -> None:
        crosshair = self.crosshairs[plot]
        point = crosshair.view_point(event.scenePos())
        if point is None:
            return
        if event.double():
            crosshair.clear()
        elif event.button() == Qt.MouseButton.LeftButton:
            crosshair.pin(point.x(), point.y())


def qc_cells(qc: QCSeries | None) -> tuple[str, str, str]:
    """Droplets made over the run's recordings, the median rate, the median CV with the state at the end."""
    if qc is None or not len(qc.t):
        return "", "", ""
    made = 0.0
    for chunk in qc.chunks(np.ones(len(qc.t), dtype=bool)):
        counts = qc.count[chunk]
        made += float(np.nanmax(counts)) if np.isfinite(counts).any() else 0.0
    rates, cvs = qc.rate[np.isfinite(qc.rate) & (qc.rate > 0)], qc.cv[np.isfinite(qc.cv) & (qc.cv > 0)]
    rate = f"{np.median(rates):.0f}" if len(rates) else "—"
    cv = f"{np.median(cvs):.1f} {qc.state[-1]}" if len(cvs) else "—"
    return f"{made:,.0f}".replace(",", " "), rate, cv


def _duration(directory: Path) -> str:
    try:
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        start = datetime.fromisoformat(summary.get("executed_at"))
        end = datetime.fromisoformat(summary.get("completed_at"))
        return f"{(end - start).total_seconds():.0f} s"
    except (OSError, ValueError, TypeError):
        return ""
