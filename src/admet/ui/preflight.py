"""Pre-flight planning section.

Replaces the planning spreadsheet: work out the flow split, how much oil, water
and IPA an experiment will consume, and the correction factor a weighed dispense
implies. It sits in the control window beside the workflow stages but is not one
of them -- it holds no workflow state and never touches hardware.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from admet.ui import theme as ui
from admet.workflows.preflight import (
    REFERENCE_FLOWS,
    FlowSetup,
    GravimetricRun,
    LiquidVolumes,
    dispense_time_s,
    estimate_consumption,
    gravimetric_factors,
    solve_flows,
)

GRAVIMETRIC_ROWS = 3


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


class PreflightPanel(QWidget):
    """Planning section of the control window.

    Lives alongside the workflow stages but is not one of them: it holds no
    workflow state and never touches hardware.
    """

    def __init__(
        self,
        *,
        channel_labels: tuple[str, ...],
        densities: Callable[[], dict[str, float]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("PreflightPanel")
        self._channel_labels = channel_labels
        self._densities = densities
        self._syncing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(ui.spacing("group"))
        root.addWidget(self._build_flow_panel())
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

        self._recalculate_gravimetric()

    def _recalculate_gravimetric(self) -> None:
        densities = self._densities()
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
