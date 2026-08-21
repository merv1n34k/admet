"""Pre-flight planning section.

Replaces the planning spreadsheet: work out the flow split, how much oil, water
and IPA an experiment will consume, and the correction factor a weighed dispense
implies. It sits in the control window beside the workflow stages but is not one
of them -- it holds no workflow state and never touches hardware.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from admet.ui import theme as ui
from admet.ui.theme import Theme
from admet.workflows.preflight import (
    REFERENCE_FLOWS,
    FlowSetup,
    GravimetricRun,
    LiquidVolumes,
    dispense_time_s,
    estimate_consumption,
    gravimetric_factors,
    ChannelPath,
    Segment,
    assess_feasibility,
    fit_system_resistance,
    layout_back_pressure,
    solve_flows,
)

GRAVIMETRIC_ROWS = 3
SYSTEM_SWEEP_ROWS = 5

# Inner diameters, which is what sets the resistance. Tubing is usually quoted by
# outer diameter -- 1/32" and 1/16" are ODs, not bores -- so the labels carry the
# usual pairings to stop the OD being entered here by mistake.
TUBING_BORES = (
    ("0.25 mm (0.010\", 1/32\" OD)", 0.25),
    ("0.30 mm (1/32\" OD)", 0.30),
    ("0.125 mm (0.005\")", 0.125),
    ("0.50 mm (0.020\")", 0.50),
    ("0.75 mm (1/16\" OD)", 0.75),
    ("1.00 mm (1/16\" OD)", 1.00),
)


def _panel(title: str) -> tuple[QFrame, QVBoxLayout]:
    panel = QFrame()
    panel.setObjectName("Panel")
    root = QVBoxLayout(panel)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(6)
    label = QLabel(title)
    label.setObjectName("PanelTitle")
    root.addWidget(label)
    body = QVBoxLayout()
    body.setContentsMargins(0, 0, 0, 0)
    body.setSpacing(8)
    root.addLayout(body)
    return panel, body


def _spin(minimum: float, maximum: float, value: float, suffix: str, decimals: int = 1) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setValue(value)
    box.setSuffix(suffix)
    box.setDecimals(decimals)
    box.setKeyboardTracking(False)
    return box


def _field(layout: QGridLayout, row: int, column: int, label: str, widget: QWidget) -> None:
    name = QLabel(label)
    name.setObjectName("FieldLabel")
    layout.addWidget(name, row, column)
    layout.addWidget(widget, row, column + 1)


def _value_label(text: str = "-") -> QLabel:
    label = QLabel(text)
    label.setObjectName("MutedText")
    return label


def _safe_at(values: list[float], index: int) -> float:
    try:
        return float(values[index])
    except (IndexError, TypeError, ValueError):
        return 0.0


def _node(text: str) -> QLabel:
    """A box in the scheme: a source, a sensor, the chip or the collection tube."""
    label = QLabel(text)
    label.setObjectName("SchemeNode")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return label


def _formula(*lines: str) -> QLabel:
    """Show the arithmetic behind a section, so the numbers can be checked by hand."""
    label = QLabel("\n".join(lines))
    label.setObjectName("FormulaText")
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class LayoutScheme(QWidget):
    """The fluidic layout drawn as plumbing, with each run's inputs on its pipe.

    Pipe thickness follows the bore and colour follows how much of the pressure
    budget that run costs, so a line that is too narrow or too long is visible as
    a picture rather than only as a number.
    """

    NODE_W = 64
    NODE_H = 30
    CELL_W = 116
    CELL_H = 46
    ROW_H = 96
    TOP_PAD = 4

    def __init__(
        self,
        channel_labels: tuple[str, ...],
        run_cells: list[list[QWidget]],
        outlet_cell: QWidget,
        run_style: Callable[[int, int], tuple[float, float]],
        outlet_style: Callable[[], tuple[float, float]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._labels = channel_labels
        self._runs = run_cells
        self._outlet = outlet_cell
        self._run_style = run_style
        self._outlet_style = outlet_style
        for row in run_cells:
            for cell in row:
                cell.setParent(self)
                cell.setFixedSize(self.CELL_W, self.CELL_H)
        outlet_cell.setParent(self)
        outlet_cell.setFixedSize(self.CELL_W, self.CELL_H)
        rows = max(1, len(channel_labels))
        self.setFixedHeight(
            int(self.TOP_PAD + self.CELL_H + 6 + self.NODE_H + 18 + (rows - 1) * self.ROW_H)
        )
        self.setMinimumWidth(760)

    # -- geometry ----------------------------------------------------------
    def _columns(self) -> tuple[float, float, float, float]:
        margin = 8.0
        span = max(1.0, self.width() - 2 * margin - self.NODE_W)
        return (
            margin,                      # source
            margin + span * 0.26,        # flow unit
            margin + span * 0.72,        # chip inlets
            margin + span,               # collection tube
        )

    def _spans(self, row: int) -> list[tuple[float, float]]:
        """Where each run of a channel starts and ends.

        The first run always reaches the flow unit; the rest share what is left up
        to the chip, so a channel with a converter is drawn in three pieces and one
        without it in two. Every channel runs into its own chip inlet.
        """
        xp, xf, xc, _xt = self._columns()
        legs = len(self._runs[row]) if row < len(self._runs) else 0
        spans = [(xp + self.NODE_W, xf)]
        start = xf + self.NODE_W
        rest = max(0, legs - 1)
        if rest:
            step = (xc - start) / rest
            spans += [(start + step * i, start + step * (i + 1)) for i in range(rest)]
        return spans

    def _chip_rect(self) -> QRectF:
        """The chip, tall enough to take every inlet on its own edge."""
        _xp, _xf, xc, _xt = self._columns()
        top = self._row_y(0) - self.NODE_H / 2
        bottom = self._row_y(max(0, len(self._labels) - 1)) + self.NODE_H / 2
        return QRectF(xc, top, self.NODE_W, max(self.NODE_H, bottom - top))

    def _row_y(self, row: int) -> float:
        """Pipe centre for a channel: the inputs ride above it, the node label below."""
        return self.TOP_PAD + self.CELL_H + 6 + self.NODE_H / 2 + row * self.ROW_H

    def _mid_y(self) -> float:
        return (self._row_y(0) + self._row_y(max(0, len(self._labels) - 1))) / 2.0

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        _xp, _xf, xc, xt = self._columns()
        for row, cells in enumerate(self._runs):
            y = self._row_y(row)
            for cell, (start, end) in zip(cells, self._spans(row), strict=False):
                cell.move(int((start + end) / 2 - self.CELL_W / 2), int(y - self.CELL_H - 6))
        self._outlet.move(
            int((xc + self.NODE_W + xt) / 2 - self.CELL_W / 2),
            int(self._mid_y() - self.CELL_H - 6),
        )

    # -- painting ----------------------------------------------------------
    def _pipe_pen(self, bore_mm: float, share: float) -> QPen:
        width = max(2.0, min(9.0, 2.0 + bore_mm * 6.0))
        if share >= 1.0:
            colour = QColor(Theme.DANGER)
        elif share >= 0.5:
            colour = QColor(Theme.WARNING)
        else:
            colour = QColor(Theme.ACCENT)
        pen = QPen(colour)
        pen.setWidthF(width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        return pen

    def _absent_pen(self) -> QPen:
        pen = QPen(QColor(Theme.BORDER_COOL))
        pen.setWidthF(1.0)
        pen.setStyle(Qt.PenStyle.DotLine)
        return pen

    def _draw_converter(self, painter: QPainter, x: float, y: float) -> None:
        """Mark where the bore steps, as the 1/16" to 1/32" union does on the oil line."""
        painter.setPen(QPen(QColor(Theme.TEXT_MUTED), 1))
        painter.setBrush(QColor(Theme.BG_RAISED))
        painter.drawRect(QRectF(x - 4, y - 7, 8, 14))

    def _draw_node(self, painter: QPainter, x: float, y: float, text: str) -> None:
        rect = QRectF(x, y - self.NODE_H / 2, self.NODE_W, self.NODE_H)
        painter.setPen(QPen(QColor(Theme.BORDER_COOL), 1))
        painter.setBrush(QColor(Theme.BG_CONTROL))
        painter.drawRoundedRect(rect, 6, 6)
        painter.setPen(QColor(Theme.TEXT_WHITE))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        xp, xf, xc, xt = self._columns()
        mid = self._mid_y()
        chip = self._chip_rect()

        for row, label in enumerate(self._labels):
            y = self._row_y(row)
            previous_bore = 0.0
            for leg, (start, end) in enumerate(self._spans(row)):
                bore, share = self._run_style(row, leg)
                if bore <= 0:
                    # No tubing on this run: the parts butt together, so keep the
                    # path continuous but show there is nothing to account for.
                    painter.setPen(self._absent_pen())
                    painter.drawLine(QPointF(start, y), QPointF(end, y))
                    continue
                painter.setPen(self._pipe_pen(bore, share))
                painter.drawLine(QPointF(start, y), QPointF(end, y))
                if previous_bore and abs(bore - previous_bore) > 1e-9:
                    self._draw_converter(painter, start, y)
                previous_bore = bore
            self._draw_node(painter, xp, y, f"P{row + 1}")
            self._draw_node(painter, xf, y, f"F{row + 1}")
            painter.setPen(QColor(Theme.TEXT_MUTED))
            painter.drawText(
                QRectF(xp, y + self.NODE_H / 2, xf - xp, 16),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                label,
            )

        # Every inlet lands on the chip's own edge; the streams only meet inside it,
        # and what leaves is the one outlet all three channels are charged for.
        painter.setPen(QPen(QColor(Theme.BORDER_COOL), 1))
        painter.setBrush(QColor(Theme.BG_CONTROL))
        painter.drawRoundedRect(chip, 6, 6)
        painter.setPen(QColor(Theme.TEXT_WHITE))
        painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, "Chip")

        bore, share = self._outlet_style()
        painter.setPen(self._pipe_pen(bore, share))
        painter.drawLine(QPointF(chip.right(), mid), QPointF(xt, mid))
        self._draw_node(painter, xt, mid, "Tube")
        painter.end()


class PreflightPanel(QWidget):
    """Planning section of the control window.

    Lives alongside the workflow stages but is not one of them: it holds no
    workflow state and never touches hardware.
    """

    def __init__(
        self,
        *,
        channel_labels: tuple[str, ...],
        liquids: Callable[[], dict[str, tuple[float, float]]],
        channel_units: dict[str, str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("PreflightPanel")
        self._channel_labels = channel_labels
        self._channel_units = channel_units or {}
        self._liquids = liquids
        self._syncing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(ui.spacing("group"))
        root.addWidget(self._build_flow_panel())
        root.addWidget(self._build_tubing_panel())
        root.addWidget(self._build_system_panel())
        root.addWidget(self._build_consumption_panel())
        root.addWidget(self._build_gravimetric_panel())
        root.addStretch()

        self._channel_flows_edited()

    # ---- flows -----------------------------------------------------------
    def _build_flow_panel(self) -> QWidget:
        panel, body = _panel("Flow and phase ratio")
        reference = ", ".join(
            f"{name} {optimal:g} ({low:g}-{high:g})"
            for name, (optimal, low, high) in REFERENCE_FLOWS.items()
        )
        hint = QLabel(f"Reference uL/min - {reference}")
        hint.setObjectName("StageSummary")
        body.addWidget(hint)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        self.oil_flow = _spin(0.0, 5000.0, REFERENCE_FLOWS["oil"][0], " uL/min")
        self.beads_flow = _spin(0.0, 5000.0, REFERENCE_FLOWS["beads"][0], " uL/min")
        self.cells_flow = _spin(0.0, 5000.0, REFERENCE_FLOWS["cells"][0], " uL/min")
        _field(grid, 0, 0, "Oil", self.oil_flow)
        _field(grid, 0, 2, "Beads", self.beads_flow)
        _field(grid, 0, 4, "Cells", self.cells_flow)

        # Total flow and phase ratio are inputs too: set either pair and the other
        # follows, so the split can be driven from whichever numbers are known.
        self.total_flow = _spin(0.0, 15000.0, 384.0, " uL/min")
        self.phase_ratio = _spin(0.0, 50.0, 1.87, "", decimals=2)
        _field(grid, 1, 0, "Total flow", self.total_flow)
        _field(grid, 1, 2, "Phase ratio", self.phase_ratio)
        body.addLayout(grid)

        body.addWidget(
            _formula(
                "total       = oil + beads + cells",
                "phase ratio = oil / (beads + cells)",
                "oil         = total x PR / (PR + 1)",
                "beads=cells = total / (2 x (PR + 1))",
            )
        )

        for box in (self.oil_flow, self.beads_flow, self.cells_flow):
            box.valueChanged.connect(self._channel_flows_edited)
        for box in (self.total_flow, self.phase_ratio):
            box.valueChanged.connect(self._totals_edited)
        return panel

    def _channel_flows_edited(self) -> None:
        """Channel rates changed: derive total flow and phase ratio."""
        if self._syncing:
            return
        setup = self.setup()
        self._set_quietly(
            (self.total_flow, setup.total_ul_min),
            (self.phase_ratio, setup.phase_ratio),
        )
        self.recalculate()

    def _totals_edited(self) -> None:
        """Total flow or phase ratio changed: split them back over the channels."""
        if self._syncing:
            return
        solved = solve_flows(self.total_flow.value(), self.phase_ratio.value())
        self._set_quietly(
            (self.oil_flow, solved.oil_ul_min),
            (self.beads_flow, solved.beads_ul_min),
            (self.cells_flow, solved.cells_ul_min),
        )
        self.recalculate()

    def _set_quietly(self, *pairs: tuple[QDoubleSpinBox, float]) -> None:
        """Write derived values without the write bouncing back as another edit."""
        self._syncing = True
        try:
            for box, value in pairs:
                box.setValue(value)
        finally:
            self._syncing = False

    def setup(self) -> FlowSetup:
        return FlowSetup(self.oil_flow.value(), self.beads_flow.value(), self.cells_flow.value())

    # ---- layout ----------------------------------------------------------
    def _build_tubing_panel(self) -> QWidget:
        panel, body = _panel("Fluidic layout back pressure")
        hint = QLabel(
            "The layout as plumbed. Fill in each run's length and bore on the pipe it "
            "belongs to; pipe thickness follows the bore and turns amber, then red, as a "
            "run eats into the pressure budget. Inlets are parallel, so a channel pays "
            "only for its own runs, while the outlet carries all three flows and is "
            "charged to every channel. The L unit runs 1/16\" out to a union and 1/32\" "
            "into the chip, so it has the extra run the M units do not."
        )
        hint.setObjectName("StageSummary")
        hint.setWordWrap(True)
        body.addWidget(hint)

        self._segment_inputs: dict[str, list[tuple[QDoubleSpinBox, QComboBox]]] = {}
        run_cells: list[list[QWidget]] = []
        for channel in self._channel_labels:
            rows: list[tuple[QDoubleSpinBox, QComboBox]] = []
            run_cells.append([self._segment_cell(*leg, rows) for leg in self._legs(channel)])
            self._segment_inputs[channel] = rows

        # Built before the scheme: it colours its pipes against this budget.
        self.pressure_limit = _spin(0.0, 20000.0, 2000.0, " mbar", decimals=0)
        self.pressure_limit.valueChanged.connect(self.recalculate)

        self.outlet_length = _spin(0.0, 10000.0, 20.0, " cm")
        self.outlet_bore = self._bore_combo(0.25)
        self.outlet_length.valueChanged.connect(self.recalculate)
        self.outlet_bore.currentIndexChanged.connect(self.recalculate)
        outlet_cell = QWidget()
        outlet_layout = QVBoxLayout(outlet_cell)
        outlet_layout.setContentsMargins(0, 0, 0, 0)
        outlet_layout.setSpacing(2)
        outlet_layout.addWidget(self.outlet_length)
        outlet_layout.addWidget(self.outlet_bore)

        self.scheme = LayoutScheme(
            self._channel_labels,
            run_cells,
            outlet_cell,
            run_style=self._run_style,
            outlet_style=self._outlet_style,
        )
        body.addWidget(self.scheme)

        limits = QGridLayout()
        limits.setContentsMargins(0, 4, 0, 0)
        limits.setHorizontalSpacing(10)
        _field(limits, 0, 0, "Pressure limit", self.pressure_limit)
        body.addLayout(limits)

        self.layout_result = _value_label()
        self.layout_result.setWordWrap(True)
        body.addWidget(self.layout_result)
        return panel

    def _legs(self, channel: str) -> tuple[tuple[float, float], ...]:
        """Runs of tubing on a channel, as (length cm, bore mm) defaults.

        The L unit is plumbed in 1/16" and stepped down to 1/32" at a union before
        the chip, so it carries a third run the 1/32"-throughout M units do not.
        """
        if self._channel_units.get(channel) == "L":
            return ((20.0, 0.75), (10.0, 0.75), (5.0, 0.25))
        return ((20.0, 0.25), (15.0, 0.25))

    def _run_style(self, row: int, leg: int) -> tuple[float, float]:
        """Bore and share of the pressure budget, for drawing one run."""
        if row >= len(self._channel_labels):
            return (0.0, 0.0)
        channel = self._channel_labels[row]
        cells = self._segment_inputs.get(channel, [])
        if leg >= len(cells):
            return (0.0, 0.0)
        length, bore = cells[leg]
        bore_mm = float(bore.currentData())
        if length.value() <= 0:
            return (0.0, 0.0)
        flows = (self.oil_flow.value(), self.beads_flow.value(), self.cells_flow.value())
        flow = flows[row] if row < len(flows) else 0.0
        drop = Segment(length.value(), bore_mm).resistance(
            self._channel_viscosity(channel)
        ) * flow
        limit = self.pressure_limit.value()
        return (bore_mm, drop / limit if limit else 0.0)

    def _outlet_style(self) -> tuple[float, float]:
        bore_mm = float(self.outlet_bore.currentData())
        total = self.oil_flow.value() + self.beads_flow.value() + self.cells_flow.value()
        viscosity = self._channel_viscosity(self._channel_labels[0]) if self._channel_labels else 1.24
        drop = Segment(self.outlet_length.value(), bore_mm).resistance(viscosity) * total
        limit = self.pressure_limit.value()
        return (bore_mm, drop / limit if limit else 0.0)

    def _segment_cell(
        self,
        default_length: float,
        default_bore: float,
        collector: list[tuple[QDoubleSpinBox, QComboBox]],
    ) -> QWidget:
        """One run of tubing, sitting on the link it represents."""
        cell = QWidget()
        layout = QVBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        length = _spin(0.0, 10000.0, default_length, " cm")
        bore = self._bore_combo(default_bore)
        length.valueChanged.connect(self.recalculate)
        bore.currentIndexChanged.connect(self.recalculate)
        layout.addWidget(length)
        layout.addWidget(bore)
        collector.append((length, bore))
        return cell

    def _bore_combo(self, default_mm: float) -> QComboBox:
        combo = QComboBox()
        for label, value in TUBING_BORES:
            combo.addItem(label, value)
        for index in range(combo.count()):
            if abs(float(combo.itemData(index)) - default_mm) < 1e-9:
                combo.setCurrentIndex(index)
                break
        return combo

    def _channel_viscosity(self, channel: str) -> float:
        density_viscosity = self._liquids().get(channel)
        if density_viscosity and density_viscosity[1] > 0:
            return density_viscosity[1]
        return 1.0

    def _channel_paths(self) -> list[ChannelPath]:
        flows = (self.oil_flow.value(), self.beads_flow.value(), self.cells_flow.value())
        paths = []
        for index, channel in enumerate(self._channel_labels):
            rows = self._segment_inputs.get(channel, [])
            segments = tuple(
                Segment(length.value(), float(bore.currentData())) for length, bore in rows
            )
            paths.append(
                ChannelPath(
                    label=channel,
                    segments=segments,
                    flow_ul_min=flows[index] if index < len(flows) else 0.0,
                    viscosity_mpa_s=self._channel_viscosity(channel),
                )
            )
        return paths

    def _recalculate_tubing(self) -> None:
        if getattr(self, "scheme", None) is not None:
            self.scheme.update()
        paths = self._channel_paths()
        outlet = Segment(self.outlet_length.value(), float(self.outlet_bore.currentData()))
        loads = layout_back_pressure(
            paths,
            outlet=outlet,
            outlet_viscosity_mpa_s=self._channel_viscosity(self._channel_labels[0])
            if self._channel_labels
            else 1.24,
        )
        limit = self.pressure_limit.value()
        lines = []
        for load in loads:
            state = "" if load.total_mbar <= limit else "  OVER the limit on tubing alone"
            lines.append(
                f"{load.label}: own runs {load.path_mbar:,.0f} mbar + shared outlet "
                f"{load.outlet_mbar:,.0f} mbar = {load.total_mbar:,.0f} mbar{state}"
            )
        if loads:
            worst = max(loads, key=lambda load: load.total_mbar)
            lines.append(
                f"Tubing takes {worst.total_mbar / limit * 100:.0f}% of the "
                f"{limit:,.0f} mbar budget at worst ({worst.label}); the chip needs the rest."
            )
        self.layout_result.setText("\n".join(lines))

    # ---- measured system -------------------------------------------------
    def _build_system_panel(self) -> QWidget:
        panel, body = _panel("Measured system resistance")
        protocol = QLabel(
            "The three inlets meet at the junction and share one outlet, so a channel "
            "cannot be measured on its own -- what each one sees depends on what the "
            "others are doing. Sweep the setup as a whole instead, keeping the ratio "
            "you actually run:\n"
            "1. Prime every line until no bubbles remain, chip connected as it will be run.\n"
            "2. Set all three channels to their working flows, then scale all of them "
            "together -- 20% to 100% of target -- so the phase ratio never changes. Flows are "
            "regulated and the pressure the controller settles at is the reading; driving "
            "pressure instead would let each channel land wherever its own resistance put it.\n"
            "3. At each step wait until all channels read stable (within 2 uL/min for 5 s), "
            "then record every channel's pressure and its steady flow from the monitor.\n"
            "4. Enter one row per step, or let the System check stage run the sweep and fill "
            "them in. Repeat whenever the chip, tubing or liquids change.\n"
            "Holding the ratio fixed is what makes this valid: total flow then rises in "
            "step with each channel, so each one stays linear in its own flow."
        )
        protocol.setObjectName("StageSummary")
        protocol.setWordWrap(True)
        body.addWidget(protocol)

        table = QGridLayout()
        table.setContentsMargins(0, 4, 0, 0)
        table.setHorizontalSpacing(8)
        table.setVerticalSpacing(4)
        step_header = QLabel("Step")
        step_header.setObjectName("FieldLabel")
        table.addWidget(step_header, 1, 0)
        for index, channel in enumerate(self._channel_labels):
            name = QLabel(channel)
            name.setObjectName("ChannelName")
            table.addWidget(name, 0, 1 + index * 2, 1, 2, alignment=Qt.AlignmentFlag.AlignHCenter)
            for offset, unit in enumerate(("mbar", "uL/min")):
                header = QLabel(unit)
                header.setObjectName("FieldLabel")
                table.addWidget(header, 1, 1 + index * 2 + offset)

        self._system_rows: list[list[tuple[QDoubleSpinBox, QDoubleSpinBox]]] = []
        for index in range(SYSTEM_SWEEP_ROWS):
            label = QLabel(str(index + 1))
            label.setObjectName("MutedText")
            table.addWidget(label, index + 2, 0)
            row: list[tuple[QDoubleSpinBox, QDoubleSpinBox]] = []
            for channel_index in range(len(self._channel_labels)):
                pressure = _spin(0.0, 20000.0, 0.0, "", decimals=0)
                flow = _spin(0.0, 20000.0, 0.0, "", decimals=2)
                pressure.valueChanged.connect(self.recalculate)
                flow.valueChanged.connect(self.recalculate)
                table.addWidget(pressure, index + 2, 1 + channel_index * 2)
                table.addWidget(flow, index + 2, 2 + channel_index * 2)
                row.append((pressure, flow))
            self._system_rows.append(row)
        body.addLayout(table)

        self.system_fit = _value_label("Enter at least two points.")
        self.system_fit.setWordWrap(True)
        body.addWidget(self.system_fit)

        self.system_verdict = QLabel("")
        self.system_verdict.setObjectName("VerdictPass")
        self.system_verdict.setWordWrap(True)
        body.addWidget(self.system_verdict)

        self.system_remedies = _value_label("")
        self.system_remedies.setWordWrap(True)
        body.addWidget(self.system_remedies)

        body.addWidget(
            _formula(
                "per channel, swept at a fixed ratio:",
                "  P_i = R_i x Q_i + P0_i     fitted from that channel's points",
                "  R_i = tubing + fittings + chip, as seen by that inlet",
                "  P0_i = pressure that buys no flow (junction + head)",
                "  max flow_i = (limit - P0_i) / R_i",
                "the channel needing the most pressure limits the setup",
            )
        )
        return panel

    def _recalculate_system(self) -> None:
        limit = self.pressure_limit.value()
        targets = (self.oil_flow.value(), self.beads_flow.value(), self.cells_flow.value())

        measured: list[tuple[str, object, float]] = []
        for index, channel in enumerate(self._channel_labels):
            samples = [
                (row[index][0].value(), row[index][1].value())
                for row in self._system_rows
                if row[index][0].value() > 0 and row[index][1].value() > 0
            ]
            if len(samples) >= 2:
                target = targets[index] if index < len(targets) else 0.0
                measured.append((channel, fit_system_resistance(samples), target))

        if not measured:
            self.system_fit.setText(
                "Enter at least two steps for a channel. Every channel is judged separately, "
                "and the one needing the most pressure is what limits the setup."
            )
            self._set_verdict(None, "")
            self.system_remedies.setText("")
            return

        tubing_lines = []
        worst: tuple[str, object, float, object] | None = None
        for channel, system, target in measured:
            tubing_r = 0.0
            if target > 0:
                tubing_r = (
                    next(
                        (load.total_mbar for load in layout_back_pressure(
                            self._channel_paths(),
                            outlet=Segment(
                                self.outlet_length.value(), float(self.outlet_bore.currentData())
                            ),
                        ) if load.label == channel),
                        0.0,
                    )
                    / target
                )
            share = tubing_r / system.resistance * 100 if system.resistance > 0 else 0.0
            line = (
                f"{channel}: R {system.resistance:.2f} mbar per uL/min, "
                f"P0 {system.threshold_mbar:,.0f} mbar (r2 {system.r_squared:.3f}, n={system.samples}); "
                f"tubing {share:.1f}% of it, chip and fittings the rest"
            )
            if not system.trustworthy:
                line += " -- poor fit, suspect a leak, partial clog or bubbles"
            tubing_lines.append(line)

            result = assess_feasibility(
                system,
                target_flow_ul_min=target,
                limit_mbar=limit,
                tubing_resistance=tubing_r,
            )
            if worst is None or result.required_mbar > worst[3].required_mbar:
                worst = (channel, system, target, result)

        self.system_fit.setText("\n".join(tubing_lines))
        channel, _system, target, result = worst
        if result.feasible:
            verdict = (
                f"FEASIBLE - every measured channel fits. {channel} needs the most at "
                f"{result.required_mbar:,.0f} mbar of the {limit:,.0f} mbar available."
            )
        else:
            verdict = (
                f"NOT FEASIBLE - {channel} needs {result.required_mbar:,.0f} mbar for "
                f"{target:g} uL/min, {result.shortfall_mbar:,.0f} mbar beyond the "
                f"{limit:,.0f} mbar limit. That channel caps out at "
                f"{result.max_flow_ul_min:.0f} uL/min, and the ratio has to hold, so the "
                f"whole setup is limited by it."
            )
        self._set_verdict(result.feasible, verdict)
        self.system_remedies.setText(
            "\n".join(f"- {item}" for item in result.remedies) if result.remedies else ""
        )

    def record_sweep_point(self, step: int, pressures: list[float], flows: list[float]) -> None:
        """Write one settled step of a measured sweep into the table.

        Typed and measured points land in the same place, so the fit and the verdict
        below do not care which they came from.
        """
        if not 0 <= step < len(self._system_rows):
            return
        row = self._system_rows[step]
        for index, (pressure_box, flow_box) in enumerate(row):
            pressure_box.setValue(_safe_at(pressures, index))
            flow_box.setValue(_safe_at(flows, index))
        self.recalculate()

    def clear_sweep(self) -> None:
        for row in self._system_rows:
            for pressure_box, flow_box in row:
                pressure_box.setValue(0.0)
                flow_box.setValue(0.0)
        self.recalculate()

    def _set_verdict(self, feasible: bool | None, text: str) -> None:
        self.system_verdict.setText(text)
        self.system_verdict.setVisible(bool(text))
        name = "VerdictPass" if feasible else "VerdictFail"
        if self.system_verdict.objectName() != name:
            self.system_verdict.setObjectName(name)
            self.system_verdict.style().unpolish(self.system_verdict)
            self.system_verdict.style().polish(self.system_verdict)

    # ---- consumption -----------------------------------------------------
    def _build_consumption_panel(self) -> QWidget:
        panel, body = _panel("Reagent consumption")

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        self.setups = QSpinBox()
        self.setups.setRange(1, 99)
        self.setups.setValue(1)
        self.replicates = QSpinBox()
        self.replicates.setRange(0, 999)
        self.replicates.setValue(3)
        self.run_time = _spin(0.0, 86400.0, 60.0, " s")
        self.overage = _spin(0.0, 500.0, 30.0, " %")
        _field(grid, 0, 0, "Setups", self.setups)
        _field(grid, 0, 2, "Replicates", self.replicates)
        _field(grid, 0, 4, "Run time", self.run_time)
        _field(grid, 1, 0, "Overage", self.overage)
        body.addLayout(grid)

        fixed = QGridLayout()
        fixed.setContentsMargins(0, 0, 0, 0)
        fixed.setHorizontalSpacing(10)
        fixed.setVerticalSpacing(6)
        for column, name in enumerate(("Oil", "Water", "IPA"), start=1):
            header = QLabel(name)
            header.setObjectName("FieldLabel")
            fixed.addWidget(header, 0, column, alignment=Qt.AlignmentFlag.AlignHCenter)
        self._fixed_inputs: dict[str, tuple[QDoubleSpinBox, ...]] = {}
        defaults = {
            "Dead volume": (50.0, 50.0, 30.0),
            "Priming": (200.0, 100.0, 200.0),
            "Washing": (0.0, 0.0, 5000.0),
        }
        for row, (name, values) in enumerate(defaults.items(), start=1):
            label = QLabel(name)
            label.setObjectName("FieldLabel")
            fixed.addWidget(label, row, 0)
            boxes = []
            for column, value in enumerate(values, start=1):
                box = _spin(0.0, 100000.0, value, " uL", decimals=0)
                box.valueChanged.connect(self.recalculate)
                fixed.addWidget(box, row, column)
                boxes.append(box)
            self._fixed_inputs[name] = tuple(boxes)
        body.addLayout(fixed)

        self.consumption_grid = QGridLayout()
        self.consumption_grid.setContentsMargins(0, 6, 0, 0)
        self.consumption_grid.setHorizontalSpacing(10)
        self.consumption_grid.setVerticalSpacing(4)
        self._consumption_values: dict[str, tuple[QLabel, ...]] = {}
        for column, name in enumerate(("Stage", "Oil, uL", "Water, uL", "IPA, uL")):
            header = QLabel(name)
            header.setObjectName("FieldLabel")
            self.consumption_grid.addWidget(header, 0, column)
        for row, name in enumerate(
            ("Dead volume", "Priming", "Tests", "Washing", "Overall"), start=1
        ):
            label = QLabel(name)
            label.setObjectName("MutedText" if name != "Overall" else "ChannelName")
            self.consumption_grid.addWidget(label, row, 0)
            values = []
            for column in range(1, 4):
                value = _value_label()
                if name == "Overall":
                    value.setObjectName("ChannelName")
                self.consumption_grid.addWidget(value, row, column)
                values.append(value)
            self._consumption_values[name] = tuple(values)
        body.addLayout(self.consumption_grid)
        body.addWidget(
            _formula(
                "tests(oil)   = sum(oil flow)     x replicates x run time / 60",
                "tests(water) = sum(beads+cells)  x replicates x run time / 60",
                "dead total   = dead volume x setups x replicates",
                "overall(oil, water) = (tests + priming + dead total) x (1 + overage/100)",
                "overall(IPA)        =  washing + priming + dead total      [no overage]",
                "overall figures are rounded to the nearest 100 uL",
            )
        )

        for box in (self.setups, self.replicates):
            box.valueChanged.connect(self.recalculate)
        for box in (self.run_time, self.overage):
            box.valueChanged.connect(self.recalculate)
        return panel

    def _fixed_volumes(self, name: str) -> LiquidVolumes:
        oil, water, ipa = self._fixed_inputs[name]
        return LiquidVolumes(oil.value(), water.value(), ipa.value())

    # ---- gravimetric -----------------------------------------------------
    def _build_gravimetric_panel(self) -> QWidget:
        panel, body = _panel("Gravimetric calibration")
        hint = QLabel(
            "Dispense the target volume, weigh the tube before and after. "
            "The factor is the dispensed volume over the commanded volume, and is "
            "what belongs in the liquid profile's scale."
        )
        hint.setObjectName("StageSummary")
        hint.setWordWrap(True)
        body.addWidget(hint)
        body.addWidget(
            _formula(
                "net, g        = full - empty",
                "volume, uL    = net / density x 1000",
                "factor        = volume / target        (mean over replicates)",
                "dispense time = target / dispense flow x 60",
            )
        )

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        self.target_volume = _spin(0.1, 100000.0, 100.0, " uL", decimals=1)
        self.dispense_flow = _spin(0.1, 5000.0, 250.0, " uL/min")
        _field(grid, 0, 0, "Target volume", self.target_volume)
        _field(grid, 0, 2, "Dispense flow", self.dispense_flow)
        self.dispense_time_value = _value_label()
        grid.addWidget(self.dispense_time_value, 0, 4)
        body.addLayout(grid)

        table = QGridLayout()
        table.setContentsMargins(0, 6, 0, 0)
        table.setHorizontalSpacing(10)
        table.setVerticalSpacing(4)
        for column, name in enumerate(
            ("Channel", "Density", "Empty, g", "Full, g", "Net, g", "Volume, uL", "Factor")
        ):
            header = QLabel(name)
            header.setObjectName("FieldLabel")
            table.addWidget(header, 0, column)

        self._gravimetric_rows: list[dict[str, QWidget]] = []
        row_index = 1
        for channel in self._channel_labels:
            for replicate in range(GRAVIMETRIC_ROWS):
                label = QLabel(channel if replicate == 0 else "")
                label.setObjectName("MutedText")
                density = _value_label()
                empty = _spin(0.0, 10000.0, 0.0, " g", decimals=4)
                full = _spin(0.0, 10000.0, 0.0, " g", decimals=4)
                empty.valueChanged.connect(self.recalculate)
                full.valueChanged.connect(self.recalculate)
                net = _value_label()
                volume = _value_label()
                factor = _value_label()
                for column, widget in enumerate(
                    (label, density, empty, full, net, volume, factor)
                ):
                    table.addWidget(widget, row_index, column)
                self._gravimetric_rows.append(
                    {
                        "channel": channel,
                        "density": density,
                        "empty": empty,
                        "full": full,
                        "net": net,
                        "volume": volume,
                        "factor": factor,
                    }
                )
                row_index += 1
        body.addLayout(table)

        self.gravimetric_summary = _value_label("Enter weights to derive correction factors.")
        self.gravimetric_summary.setWordWrap(True)
        body.addWidget(self.gravimetric_summary)

        for box in (self.target_volume, self.dispense_flow):
            box.valueChanged.connect(self.recalculate)
        return panel

    # ---- recalculation ---------------------------------------------------
    def recalculate(self) -> None:
        setup = self.setup()
        report = estimate_consumption(
            [setup] * self.setups.value(),
            replicates=self.replicates.value(),
            run_time_s=self.run_time.value(),
            overage_percent=self.overage.value(),
            dead_volume=self._fixed_volumes("Dead volume"),
            priming=self._fixed_volumes("Priming"),
            washing=self._fixed_volumes("Washing"),
        )
        for name, volumes in report.stages:
            oil, water, ipa = self._consumption_values[name]
            oil.setText(f"{volumes.oil:,.0f}")
            water.setText(f"{volumes.water:,.0f}")
            ipa.setText(f"{volumes.ipa:,.0f}")

        self._recalculate_tubing()
        self._recalculate_system()
        self._recalculate_gravimetric()

    def _recalculate_gravimetric(self) -> None:
        densities = {name: values[0] for name, values in self._liquids().items()}
        target = self.target_volume.value()
        self.dispense_time_value.setText(
            f"{dispense_time_s(target, self.dispense_flow.value()):.1f} s per dispense"
        )

        runs = []
        for row in self._gravimetric_rows:
            channel = str(row["channel"])
            density = densities.get(channel, 0.0)
            row["density"].setText(f"{density:g}" if density else "-")
            empty = row["empty"].value()
            full = row["full"].value()
            if full <= empty:
                row["net"].setText("-")
                row["volume"].setText("-")
                row["factor"].setText("-")
                continue
            run = GravimetricRun(channel, empty, full)
            row["net"].setText(f"{run.net_g():.4f}")
            row["volume"].setText(f"{run.volume_ul(density):.2f}")
            row["factor"].setText(f"{run.relative(density, target):.3f}")
            runs.append(run)

        if not runs:
            self.gravimetric_summary.setText("Enter weights to derive correction factors.")
            return
        summary = [
            f"{result.channel}: factor {result.mean_relative:.3f} "
            f"({result.mean_volume_ul:.1f} uL, n={result.runs})"
            for result in gravimetric_factors(runs, densities=densities, target_ul=target)
        ]
        self.gravimetric_summary.setText("   ".join(summary))
