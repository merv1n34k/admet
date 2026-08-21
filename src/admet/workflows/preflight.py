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
