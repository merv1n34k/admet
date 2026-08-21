"""Pre-flight planning maths: flow split, reagent consumption, gravimetric factors.

This is the calculator that replaces the planning spreadsheet. It is deliberately
free of hardware and UI: everything here is a pure function of the numbers the
operator types in, so the same figures can be checked in tests.

Nothing in here is wired into the acquisition workflow; it is a standalone check
run before an experiment.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from math import pi
from statistics import mean

# Reference flow rates (uL/min) from the McCarroll protocol, converted from uL/h.
REFERENCE_FLOWS = {
    "oil": (250.0, 218.0, 300.0),
    "beads": (67.0, 58.0, 70.0),
    "cells": (67.0, 58.0, 70.0),
}


@dataclass(frozen=True)
class FlowSetup:
    """One set of channel flow rates."""

    oil_ul_min: float
    beads_ul_min: float
    cells_ul_min: float

    @property
    def aqueous_ul_min(self) -> float:
        return self.beads_ul_min + self.cells_ul_min

    @property
    def total_ul_min(self) -> float:
        return self.oil_ul_min + self.aqueous_ul_min

    @property
    def phase_ratio(self) -> float:
        """Oil against the combined aqueous streams."""
        aqueous = self.aqueous_ul_min
        return self.oil_ul_min / aqueous if aqueous else 0.0


def solve_flows(total_ul_min: float, phase_ratio: float) -> FlowSetup:
    """Split a total flow into oil and two equal aqueous streams at a phase ratio."""
    if total_ul_min <= 0 or phase_ratio <= 0:
        return FlowSetup(0.0, 0.0, 0.0)
    oil = total_ul_min / (phase_ratio + 1) * phase_ratio
    aqueous_each = total_ul_min / (phase_ratio + 1) / 2
    return FlowSetup(oil, aqueous_each, aqueous_each)


@dataclass(frozen=True)
class LiquidVolumes:
    """A volume per liquid, in uL."""

    oil: float = 0.0
    water: float = 0.0
    ipa: float = 0.0

    def scaled(self, factor: float) -> LiquidVolumes:
        return LiquidVolumes(self.oil * factor, self.water * factor, self.ipa * factor)


@dataclass(frozen=True)
class ConsumptionReport:
    dead_volume: LiquidVolumes
    priming: LiquidVolumes
    tests: LiquidVolumes
    washing: LiquidVolumes
    overall: LiquidVolumes

    @property
    def stages(self) -> tuple[tuple[str, LiquidVolumes], ...]:
        return (
            ("Dead volume", self.dead_volume),
            ("Priming", self.priming),
            ("Tests", self.tests),
            ("Washing", self.washing),
            ("Overall", self.overall),
        )


def estimate_consumption(
    setups: Sequence[FlowSetup],
    *,
    replicates: int,
    run_time_s: float,
    overage_percent: float = 30.0,
    dead_volume: LiquidVolumes = LiquidVolumes(50.0, 50.0, 30.0),
    priming: LiquidVolumes = LiquidVolumes(200.0, 100.0, 200.0),
    washing: LiquidVolumes = LiquidVolumes(0.0, 0.0, 5000.0),
) -> ConsumptionReport:
    """How much oil, water and IPA an experiment needs.

    Each setup is run `replicates` times for `run_time_s`, so the test volume is
    the summed flow over that time. Dead volume is paid once per setup per run.
    Overage covers oil and water; IPA is dominated by the wash and is not scaled.
    """
    minutes = max(0.0, run_time_s) / 60.0
    runs = max(0, replicates)
    count = len(setups)

    tests = LiquidVolumes(
        oil=sum(setup.oil_ul_min for setup in setups) * runs * minutes,
        water=sum(setup.aqueous_ul_min for setup in setups) * runs * minutes,
        ipa=0.0,
    )
    dead_total = dead_volume.scaled(count * runs)
    scale = (100.0 + overage_percent) / 100.0

    overall = LiquidVolumes(
        oil=_round_to_hundred((tests.oil + priming.oil + dead_total.oil) * scale),
        water=_round_to_hundred((tests.water + priming.water + dead_total.water) * scale),
        ipa=_round_to_hundred(washing.ipa + priming.ipa + dead_total.ipa),
    )
    return ConsumptionReport(
        dead_volume=dead_volume,
        priming=priming,
        tests=tests,
        washing=washing,
        overall=overall,
    )


def _round_to_hundred(value: float) -> float:
    # Half away from zero, matching the spreadsheet this replaces. Python's round()
    # is banker's rounding and would send 2650 down to 2600.
    return float(Decimal(value / 100.0).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 100)


@dataclass(frozen=True)
class BackPressure:
    """Pressure a length of tubing costs, and how much length the budget allows."""

    drop_mbar: float
    limit_mbar: float
    max_length_cm: float
    reynolds: float

    @property
    def headroom_mbar(self) -> float:
        return self.limit_mbar - self.drop_mbar

    @property
    def within_limit(self) -> bool:
        return self.drop_mbar <= self.limit_mbar

    @property
    def laminar(self) -> bool:
        """Hagen-Poiseuille assumes laminar flow; above ~2000 it stops holding."""
        return self.reynolds < 2000.0


def tubing_back_pressure(
    *,
    length_cm: float,
    inner_diameter_mm: float,
    flow_ul_min: float,
    viscosity_mpa_s: float,
    density_g_ml: float = 0.0,
    limit_mbar: float = 2000.0,
) -> BackPressure:
    """Hagen-Poiseuille pressure drop along a tube.

        dP = 128 * mu * L * Q / (pi * d^4)

    Pressure rises with the fourth power of the inverse diameter, so a slightly
    narrower or longer line costs far more than it looks like it should. When the
    drop exceeds what the controller can supply, the requested flow is simply never
    reached -- the loop saturates and the flow sits below setpoint.
    """
    length_m = max(0.0, length_cm) / 100.0
    diameter_m = max(0.0, inner_diameter_mm) / 1000.0
    flow_m3_s = max(0.0, flow_ul_min) * 1e-9 / 60.0
    viscosity_pa_s = max(0.0, viscosity_mpa_s) / 1000.0

    if diameter_m <= 0 or viscosity_pa_s <= 0:
        return BackPressure(0.0, limit_mbar, 0.0, 0.0)

    resistance = 128.0 * viscosity_pa_s / (pi * diameter_m**4)  # Pa per (m3/s) per m
    drop_pa = resistance * length_m * flow_m3_s
    drop_mbar = drop_pa / 100.0

    # Longest tube the pressure budget allows at this flow.
    limit_pa = max(0.0, limit_mbar) * 100.0
    max_length_cm = (
        limit_pa / (resistance * flow_m3_s) * 100.0 if flow_m3_s > 0 else float("inf")
    )

    area = pi * diameter_m**2 / 4.0
    velocity = flow_m3_s / area if area else 0.0
    density_kg_m3 = max(0.0, density_g_ml) * 1000.0
    reynolds = density_kg_m3 * velocity * diameter_m / viscosity_pa_s if viscosity_pa_s else 0.0

    return BackPressure(
        drop_mbar=drop_mbar,
        limit_mbar=limit_mbar,
        max_length_cm=max_length_cm,
        reynolds=reynolds,
    )


@dataclass(frozen=True)
class Segment:
    """One run of tubing: a length of a single bore."""

    length_cm: float
    bore_mm: float

    def resistance(self, viscosity_mpa_s: float) -> float:
        """mbar per uL/min. Hagen-Poiseuille with the flow term factored out."""
        return tubing_back_pressure(
            length_cm=self.length_cm,
            inner_diameter_mm=self.bore_mm,
            flow_ul_min=1.0,
            viscosity_mpa_s=viscosity_mpa_s,
        ).drop_mbar


@dataclass(frozen=True)
class ChannelPath:
    """A channel's own run: pressure source to sensor to chip."""

    label: str
    segments: tuple[Segment, ...]
    flow_ul_min: float
    viscosity_mpa_s: float

    def resistance(self) -> float:
        return sum(segment.resistance(self.viscosity_mpa_s) for segment in self.segments)

    def drop_mbar(self) -> float:
        return self.resistance() * self.flow_ul_min


@dataclass(frozen=True)
class ChannelLoad:
    label: str
    path_mbar: float
    outlet_mbar: float

    @property
    def total_mbar(self) -> float:
        return self.path_mbar + self.outlet_mbar


def layout_back_pressure(
    channels: Sequence[ChannelPath],
    *,
    outlet: Segment | None = None,
    outlet_viscosity_mpa_s: float = 1.24,
) -> tuple[ChannelLoad, ...]:
    """Pressure each inlet must supply for a converging chip.

    The inlets run in parallel, so a channel only carries its own path. The outlet
    is downstream of the junction and carries every channel's flow, so its drop is
    added to all of them -- it is the one segment shared by the whole setup.
    """
    total_flow = sum(channel.flow_ul_min for channel in channels)
    outlet_drop = 0.0
    if outlet is not None:
        outlet_drop = outlet.resistance(outlet_viscosity_mpa_s) * total_flow
    return tuple(
        ChannelLoad(channel.label, channel.drop_mbar(), outlet_drop) for channel in channels
    )


@dataclass(frozen=True)
class SystemResistance:
    """What a measured pressure/flow sweep says about the whole fluidic path.

        P = R * Q + P0

    R is tubing, fittings and chip together; P0 is the pressure that buys no flow
    at all -- the Laplace pressure holding the interface at the droplet junction,
    plus any hydrostatic head.
    """

    resistance: float  # mbar per uL/min
    threshold_mbar: float
    r_squared: float
    samples: int

    def pressure_for(self, flow_ul_min: float) -> float:
        return self.resistance * flow_ul_min + self.threshold_mbar

    def flow_at(self, pressure_mbar: float) -> float:
        if self.resistance <= 0:
            return 0.0
        return max(0.0, (pressure_mbar - self.threshold_mbar) / self.resistance)

    @property
    def trustworthy(self) -> bool:
        """A poor fit means a leak, a partial clog, bubbles, or a bad point."""
        return self.samples >= 3 and self.r_squared >= 0.95


def fit_system_resistance(samples: Sequence[tuple[float, float]]) -> SystemResistance:
    """Least-squares fit of P = R*Q + P0 over measured (pressure, flow) pairs."""
    points = [(float(q), float(p)) for p, q in samples if q > 0 or p > 0]
    if len(points) < 2:
        return SystemResistance(0.0, 0.0, 0.0, len(points))

    n = len(points)
    mean_q = sum(q for q, _p in points) / n
    mean_p = sum(p for _q, p in points) / n
    varq = sum((q - mean_q) ** 2 for q, _p in points)
    if varq <= 0:
        return SystemResistance(0.0, 0.0, 0.0, n)

    slope = sum((q - mean_q) * (p - mean_p) for q, p in points) / varq
    intercept = mean_p - slope * mean_q

    total = sum((p - mean_p) ** 2 for _q, p in points)
    residual = sum((p - (slope * q + intercept)) ** 2 for q, p in points)
    r_squared = 1.0 - residual / total if total > 0 else 1.0
    return SystemResistance(slope, intercept, r_squared, n)


@dataclass(frozen=True)
class Feasibility:
    """Whether a target flow is reachable, and what to change when it is not."""

    feasible: bool
    target_flow_ul_min: float
    required_mbar: float
    limit_mbar: float
    max_flow_ul_min: float
    remedies: tuple[str, ...] = ()

    @property
    def shortfall_mbar(self) -> float:
        return max(0.0, self.required_mbar - self.limit_mbar)


def assess_feasibility(
    system: SystemResistance,
    *,
    target_flow_ul_min: float,
    limit_mbar: float,
    tubing_resistance: float = 0.0,
    tubing_length_cm: float = 0.0,
    tubing_id_mm: float = 0.0,
) -> Feasibility:
    """Can the controller drive the target flow, and if not what has to change?

    Only the tubing's share of the resistance can be changed by re-plumbing. If
    the rest of the path -- chip and fittings -- already exceeds the budget, no
    tubing change rescues it, and saying so is more useful than a suggestion that
    cannot work.
    """
    required = system.pressure_for(target_flow_ul_min)
    max_flow = system.flow_at(limit_mbar)
    if system.resistance <= 0 or target_flow_ul_min <= 0:
        return Feasibility(False, target_flow_ul_min, required, limit_mbar, max_flow)
    if required <= limit_mbar:
        return Feasibility(True, target_flow_ul_min, required, limit_mbar, max_flow)

    remedies: list[str] = []
    remedies.append(f"Run at {max_flow:.0f} uL/min or less at this pressure limit.")
    remedies.append(f"Raise the pressure limit to at least {required:,.0f} mbar.")

    # Resistance budget left for the tubing once the fixed part is paid.
    budget = (limit_mbar - system.threshold_mbar) / target_flow_ul_min
    fixed = system.resistance - tubing_resistance
    allowed = budget - fixed
    if tubing_resistance <= 0:
        remedies.append("Enter the tubing geometry above to see what re-plumbing would buy.")
    elif allowed <= 0:
        remedies.append(
            "The chip and fittings alone exceed the budget: no tubing change helps. "
            "Use a lower flow, a higher pressure, or a less restrictive chip."
        )
    else:
        if tubing_length_cm > 0:
            max_length = tubing_length_cm * allowed / tubing_resistance
            remedies.append(f"Shorten the tubing to {max_length:.0f} cm or less.")
        if tubing_id_mm > 0:
            # Resistance goes as 1/d^4, so the bore only has to grow a little.
            needed_id = tubing_id_mm * (tubing_resistance / allowed) ** 0.25
            remedies.append(f"Widen the tubing bore to {needed_id:.2f} mm or more.")
    return Feasibility(False, target_flow_ul_min, required, limit_mbar, max_flow, tuple(remedies))


@dataclass(frozen=True)
class GravimetricRun:
    """One weighed dispense used to derive a channel's correction factor."""

    channel: str
    empty_g: float
    full_g: float

    def net_g(self) -> float:
        return self.full_g - self.empty_g

    def volume_ul(self, density_g_ml: float) -> float:
        if density_g_ml <= 0:
            return 0.0
        return self.net_g() / density_g_ml * 1000.0

    def relative(self, density_g_ml: float, target_ul: float) -> float:
        """Dispensed over commanded: the factor the sensor reading needs."""
        if target_ul <= 0:
            return 0.0
        return self.volume_ul(density_g_ml) / target_ul


@dataclass(frozen=True)
class GravimetricResult:
    channel: str
    runs: int
    mean_volume_ul: float
    mean_relative: float
    volumes_ul: tuple[float, ...] = field(default_factory=tuple)


def gravimetric_factors(
    runs: Sequence[GravimetricRun],
    *,
    densities: dict[str, float],
    target_ul: float,
) -> tuple[GravimetricResult, ...]:
    """Mean correction factor per channel from weighed dispenses."""
    grouped: dict[str, list[GravimetricRun]] = {}
    for run in runs:
        grouped.setdefault(run.channel, []).append(run)

    results = []
    for channel, channel_runs in grouped.items():
        density = densities.get(channel, 0.0)
        volumes = tuple(run.volume_ul(density) for run in channel_runs)
        relatives = [run.relative(density, target_ul) for run in channel_runs]
        results.append(
            GravimetricResult(
                channel=channel,
                runs=len(channel_runs),
                mean_volume_ul=mean(volumes) if volumes else 0.0,
                mean_relative=mean(relatives) if relatives else 0.0,
                volumes_ul=volumes,
            )
        )
    return tuple(results)


def dispense_time_s(target_ul: float, flow_ul_min: float) -> float:
    """How long a gravimetric dispense of target_ul takes at flow_ul_min."""
    if flow_ul_min <= 0:
        return 0.0
    return target_ul / flow_ul_min * 60.0
