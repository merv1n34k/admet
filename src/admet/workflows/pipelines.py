"""Runs written as sequences of operations.

An operation does one thing and knows when it would be wrong to do it. A pipeline
is several of them in order -- the setup you do every morning, the run you do
after it -- written in Python, because a list of function calls is a better
language for this than anything that would have to be invented.

    def morning(settings):
        yield step("connect", simulated=settings["simulated"])
        yield step("apply_corrections")
        yield step("prime", prime_oil_volume_ul=settings["prime_volume_ul"])

Each stage is checked by the operation's own guards as it is reached, so a
pipeline cannot do anything the operation would have refused on its own. A stage
that starts a protocol is waited on before the next begins.

A protocol that stops for the operator stops the pipeline: it reports which stage
it reached and why, and the operator answers with confirm and starts the pipeline
again from there. Nothing is auto-confirmed -- a confirmation exists because
somebody has to look at the rig.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from admet.core.engine import Param, ParamKind
from admet.workflows.operations import operation as find_operation


@dataclass(frozen=True)
class Stage:
    """One operation within a pipeline."""

    operation: str
    settings: dict[str, Any] = field(default_factory=dict)
    label: str = ""

    def described(self) -> str:
        return self.label or find_operation(self.operation).label


def step(operation: str, label: str = "", **settings: Any) -> Stage:
    return Stage(operation=operation, settings=settings, label=label)


@dataclass(frozen=True)
class Pipeline:
    id: str
    label: str
    description: str
    params: tuple[Param, ...] = ()
    build: Callable[[dict[str, Any]], Iterator[Stage]] = field(repr=False, default=None)  # type: ignore[assignment]

    def stages(self, settings: dict[str, Any]) -> tuple[Stage, ...]:
        return tuple(self.build(settings))


# ---- the pipelines ---------------------------------------------------------


def _setup(settings: dict[str, Any]) -> Iterator[Stage]:
    """Everything between a cold instrument and a rig ready to run."""
    yield step("connect", simulated=settings["simulated"])
    yield step("apply_corrections")
    yield step(
        "prime",
        prime_oil_volume_ul=settings["prime_oil_volume_ul"],
        prime_aqueous_volume_ul=settings["prime_aqueous_volume_ul"],
        tick_s=settings["tick_s"],
    )


def _checks(settings: dict[str, Any]) -> Iterator[Stage]:
    """Both system checks, dispense before flow.

    The dispense check comes first because a channel whose correction factor is
    wrong reports flows that are not flows, and the flow check would then be
    measuring the wrong thing.
    """
    yield step("gravimetry", tick_s=settings["tick_s"])
    yield step("characterise", tick_s=settings["tick_s"])


def _shutdown(_settings: dict[str, Any]) -> Iterator[Stage]:
    """Leave the rig safe and the instrument released."""
    yield step("wash", tick_s=0.2)
    yield step("disconnect")


PIPELINES: tuple[Pipeline, ...] = (
    Pipeline(
        "setup",
        "Morning setup",
        "Connect, apply corrections, and prime every line.",
        params=(
            Param("simulated", "Simulated hardware", ParamKind.BOOLEAN, default=False),
            Param("prime_oil_volume_ul", "Oil volume", ParamKind.FLOAT, default=40.0, minimum=0.1),
            Param(
                "prime_aqueous_volume_ul", "Aqueous volume", ParamKind.FLOAT, default=5.0, minimum=0.1
            ),
            Param("tick_s", "Pipeline tick", ParamKind.FLOAT, default=0.2, minimum=0.001),
        ),
        build=_setup,
    ),
    Pipeline(
        "checks",
        "Both system checks",
        "Run the dispense check, then the flow check.",
        params=(Param("tick_s", "Pipeline tick", ParamKind.FLOAT, default=0.2, minimum=0.001),),
        build=_checks,
    ),
    Pipeline(
        "shutdown",
        "Wash and release",
        "Wash the chip and disconnect the instrument.",
        build=_shutdown,
    ),
)

BY_ID = {pipeline.id: pipeline for pipeline in PIPELINES}


def pipeline(pipeline_id: str) -> Pipeline:
    if pipeline_id not in BY_ID:
        known = ", ".join(sorted(BY_ID))
        raise LookupError(f"unknown pipeline {pipeline_id!r}; this build offers: {known}")
    return BY_ID[pipeline_id]
