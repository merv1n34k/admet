"""Non-actuating preflight tools for flow ratios, layout and consumption."""

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

from admet.ui.theme import Theme
from admet.workflows.preflight import (
    REFERENCE_FLOWS,
    CheckConditions,
    FlowSetup,
    LiquidVolumes,
    estimate_consumption,
    ChannelPath,
    Segment,
    emulsion_viscosity,
    layout_back_pressure,
    solve_flows,
)


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

        # The stage page mounts these sections; this widget retains their state.
        self.sections: dict[str, QWidget] = {
            "flow": self._build_flow_panel(),
            "layout": self._build_tubing_panel(),
            "consumption": self._build_consumption_panel(),
        }
        self.hide()

        self._channel_flows_edited()

    def sections_for(self, keys: tuple[str, ...]) -> list[QWidget]:
        """The named sections, in the order asked for."""
        return [self.sections[key] for key in keys if key in self.sections]

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
            "Lengths and bores as plumbed. Thickness follows the bore; a run turns "
            "amber, then red, as it eats into the budget. Inlets are parallel, so each "
            "pays only for its own runs -- the outlet carries all three flows and is "
            "charged to every channel."
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
        self.pressure_limit = _spin(1.0, 1_000_000_000.0, 2000.0, " mbar", decimals=0)
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
        _field(limits, 0, 0, "Pressure budget", self.pressure_limit)
        body.addLayout(limits)
        note = QLabel(
            "One unit's ceiling, applied per channel rather than shared; where the "
            "units differ, enter the lowest. Calculation only; hardware trips belong to the protocol. "
            "Tubing only -- the chip is measured below."
        )
        note.setObjectName("StageSummary")
        note.setWordWrap(True)
        body.addWidget(note)

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
        flows = (self.oil_flow.value(), self.cells_flow.value(), self.beads_flow.value())
        flow = flows[row] if row < len(flows) else 0.0
        drop = Segment(length.value(), bore_mm).resistance(
            self._channel_viscosity(channel)
        ) * flow
        limit = self.pressure_limit.value()
        return (bore_mm, drop / limit if limit else 0.0)

    def _outlet_viscosity(self) -> float:
        """The emulsion's viscosity, not the oil's.

        Downstream of the chip the carrier is packed with aqueous droplets and
        flows less easily than either liquid alone. The outlet carries every
        channel's flow, so this is the segment least worth under-counting.
        """
        setup = self.setup()
        total = setup.total_ul_min
        if not self._channel_labels or total <= 0:
            return 1.24
        carrier = self._channel_viscosity(self._channel_labels[0])
        aqueous = [
            self._channel_viscosity(channel) for channel in self._channel_labels[1:]
        ]
        dispersed = sum(aqueous) / len(aqueous) if aqueous else carrier
        return emulsion_viscosity(carrier, dispersed, setup.aqueous_ul_min / total)

    def _outlet_style(self) -> tuple[float, float]:
        bore_mm = float(self.outlet_bore.currentData())
        total = self.oil_flow.value() + self.beads_flow.value() + self.cells_flow.value()
        drop = Segment(self.outlet_length.value(), bore_mm).resistance(self._outlet_viscosity()) * total
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
        self._select_bore(combo, default_mm)
        return combo

    def _channel_viscosity(self, channel: str) -> float:
        density_viscosity = self._liquids().get(channel)
        if density_viscosity and density_viscosity[1] > 0:
            return density_viscosity[1]
        return 1.0

    def _channel_paths(self) -> list[ChannelPath]:
        flows = (self.oil_flow.value(), self.cells_flow.value(), self.beads_flow.value())
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
            outlet_viscosity_mpa_s=self._outlet_viscosity(),
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

    def _outlet_segment(self) -> Segment:
        return Segment(self.outlet_length.value(), float(self.outlet_bore.currentData()))

    # ---- snapshots -------------------------------------------------------
    def conditions(self) -> CheckConditions:
        """The setup as it stands, to be frozen alongside a measurement."""
        return CheckConditions(
            setup=self.setup(),
            liquids=dict(self._liquids()),
            paths=tuple(self._channel_paths()),
            outlet=self._outlet_segment(),
            pressure_limit_mbar=self.pressure_limit.value(),
        )

    def load_snapshot(self, data: dict) -> bool:
        """Restore preflight settings without loading experimental results."""
        conditions = data.get("conditions") if isinstance(data.get("conditions"), dict) else {}
        restored = False

        flows = conditions.get("flows_ul_min") if isinstance(conditions, dict) else None
        if isinstance(flows, dict):
            self._set_quietly(
                (self.oil_flow, float(flows.get("oil") or 0.0)),
                (self.beads_flow, float(flows.get("beads") or 0.0)),
                (self.cells_flow, float(flows.get("cells") or 0.0)),
            )
            restored = True

        limit = conditions.get("pressure_limit_mbar")
        if isinstance(limit, int | float):
            self.pressure_limit.setValue(float(limit))

        layout = conditions.get("layout") if isinstance(conditions.get("layout"), dict) else {}
        for channel in layout.get("channels", ()) if isinstance(layout, dict) else ():
            if not isinstance(channel, dict):
                continue
            rows = self._segment_inputs.get(str(channel.get("channel") or ""), [])
            for (length, bore), run in zip(rows, channel.get("runs", ()), strict=False):
                if not isinstance(run, dict):
                    continue
                length.setValue(float(run.get("length_cm") or 0.0))
                self._select_bore(bore, float(run.get("bore_mm") or 0.0))
            restored = restored or bool(rows)
        outlet = layout.get("outlet") if isinstance(layout, dict) else None
        if isinstance(outlet, dict):
            self.outlet_length.setValue(float(outlet.get("length_cm") or 0.0))
            self._select_bore(self.outlet_bore, float(outlet.get("bore_mm") or 0.0))

        for name in ("setups", "replicates", "run_time", "overage"):
            value = data.get("consumption", {}).get(name)
            if isinstance(value, (int, float)):
                getattr(self, name).setValue(value)
        self.recalculate()
        return restored

    def workspace_state(self):
        return {
            "conditions": self.conditions().to_dict(),
            "consumption": {name: getattr(self, name).value()
                            for name in ("setups", "replicates", "run_time", "overage")},
        }

    def _select_bore(self, combo: QComboBox, bore_mm: float) -> None:
        for index in range(combo.count()):
            if abs(float(combo.itemData(index)) - bore_mm) < 1e-9:
                combo.setCurrentIndex(index)
                return

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
