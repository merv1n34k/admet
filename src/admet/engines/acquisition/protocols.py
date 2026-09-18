"""The experiments this instrument knows how to run.

This is the file to edit to add an experiment. A protocol is a list of steps,
each naming the flows or pressures to apply, what to wait for, and what to leave
the channel doing afterwards. Nothing here drives hardware: these are
declarations, and the pipeline runs them.

To add one: write a function taking the settings dict and returning the steps,
then add it to PROTOCOLS. Any setting it reads must also be declared in
settings.py and listed on the run_protocol action in engine.py, or it will not
reach you.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from admet.engines.acquisition.fluidics.config import (
    BEADS_M_SENSOR,
    CELLS_M_SENSOR,
    DROPSEQ_OIL_FLOW_UL_MIN,
    OIL_L_SENSOR,
    PRIMING_AQUEOUS_FLOW_UL_MIN,
    PRIMING_OIL_FLOW_UL_MIN,
    STABILITY_DURATION_S,
    STABILITY_TIMEOUT_S,
    STABILITY_TOLERANCE_UL_MIN,
)

@dataclass(frozen=True)
class ProtocolStep:
    name: str
    sensor_setpoints: dict[int, float]
    trigger_type: str
    trigger_params: dict
    pressure_setpoints: dict[int, float] = field(default_factory=dict)
    on_complete: str = "hold"
    confirm_message: str = ""
    repeat: int = 1
    group: str = ""

def build_protocol(name: str, settings: dict | None = None) -> list[ProtocolStep]:
    """The steps of one protocol, built from the settings it was given.

    Settings are required. There is deliberately no default protocol to fall back
    on: a name that arrives without its settings is a mistake worth reporting,
    not a reason to run something else under the same name.
    """
    builder = PROTOCOLS.get(name)
    if builder is None:
        known = ", ".join(protocol_names())
        raise ValueError(f"unknown protocol {name!r}; this build runs: {known}")
    if not settings:
        raise ValueError(f"protocol {name!r} needs its settings")
    return builder(settings)


def build_priming_protocol(settings: dict) -> list[ProtocolStep]:
    oil_volume = float(settings["prime_oil_volume_ul"])
    aqueous_volume = float(settings["prime_aqueous_volume_ul"])
    return [
        ProtocolStep(
            name="Prime Oil L",
            sensor_setpoints={OIL_L_SENSOR: PRIMING_OIL_FLOW_UL_MIN},
            trigger_type="volume",
            trigger_params={"sensor_index": OIL_L_SENSOR, "target_volume_ul": oil_volume},
            on_complete="zero",
            confirm_message=f"Prime Oil L at {PRIMING_OIL_FLOW_UL_MIN:g} uL/min for {oil_volume:g} uL. Proceed?",
        ),
        ProtocolStep(
            name="Prime Cells M",
            sensor_setpoints={CELLS_M_SENSOR: PRIMING_AQUEOUS_FLOW_UL_MIN},
            trigger_type="volume",
            trigger_params={"sensor_index": CELLS_M_SENSOR, "target_volume_ul": aqueous_volume},
            on_complete="zero",
            confirm_message=f"Prime Cells M at {PRIMING_AQUEOUS_FLOW_UL_MIN:g} uL/min for {aqueous_volume:g} uL. Proceed?",
        ),
        ProtocolStep(
            name="Prime Beads M",
            sensor_setpoints={BEADS_M_SENSOR: PRIMING_AQUEOUS_FLOW_UL_MIN},
            trigger_type="volume",
            trigger_params={"sensor_index": BEADS_M_SENSOR, "target_volume_ul": aqueous_volume},
            on_complete="zero",
            confirm_message=f"Prime Beads M at {PRIMING_AQUEOUS_FLOW_UL_MIN:g} uL/min for {aqueous_volume:g} uL. Proceed?",
        ),
    ]

def build_dropseq_protocol(settings: dict) -> list[ProtocolStep]:
    steps: list[ProtocolStep] = []
    set_count = int(settings["set_count"])
    replicate_count = int(settings["replicate_count"])
    run_volume_ul = float(settings["run_volume_ul"])
    oil_flow = float(settings.get("run_oil_flow_ul_min", DROPSEQ_OIL_FLOW_UL_MIN))
    aqueous_total = float(settings["run_aqueous_total_flow_ul_min"])
    aqueous_channel = aqueous_total / 2.0
    for set_index in range(1, set_count + 1):
        for replicate_index in range(1, replicate_count + 1):
            label = f"set{set_index:02d}_rep{replicate_index:02d}"
            steps.extend(
                (
                    ProtocolStep(
                        name=f"Run {label}",
                        sensor_setpoints={
                            OIL_L_SENSOR: oil_flow,
                            CELLS_M_SENSOR: aqueous_channel,
                            BEADS_M_SENSOR: aqueous_channel,
                        },
                        trigger_type="volume",
                        trigger_params={
                            "sensor_index": OIL_L_SENSOR,
                            "target_volume_ul": run_volume_ul,
                        },
                        on_complete="zero",
                        confirm_message=(
                            f"Start {label}: Oil L {oil_flow:g} uL/min, "
                            f"Cells M/Beads M {aqueous_channel:g} uL/min?"
                        ),
                    ),
                    ProtocolStep(
                        name=f"Confirm {label}",
                        sensor_setpoints={},
                        trigger_type="time",
                        trigger_params={"duration_s": 0.0},
                        confirm_message=f"{label} complete. Confirm before continuing.",
                    ),
                )
            )
    return steps

def build_wash_protocol(settings: dict) -> list[ProtocolStep]:
    oil_flow = float(settings["wash_oil_flow_ul_min"])
    aqueous_channel = float(settings["wash_aqueous_total_flow_ul_min"]) / 2.0
    oil_volume = float(settings["wash_oil_volume_ul"])
    pressure = float(settings["wash_pressure_mbar"])
    duration_s = float(settings["wash_pressure_duration_s"])
    return [
        ProtocolStep(
            name="Wash flow phase",
            sensor_setpoints={
                OIL_L_SENSOR: oil_flow,
                CELLS_M_SENSOR: aqueous_channel,
                BEADS_M_SENSOR: aqueous_channel,
            },
            trigger_type="volume",
            trigger_params={"sensor_index": OIL_L_SENSOR, "target_volume_ul": oil_volume},
            on_complete="zero",
            confirm_message=(
                f"Start wash phase 1: {oil_flow:g}/{aqueous_channel:g}/{aqueous_channel:g} "
                f"uL/min until Oil L dispenses {oil_volume:g} uL?"
            ),
        ),
        ProtocolStep(
            name="Wash pressure phase",
            sensor_setpoints={},
            pressure_setpoints={OIL_L_SENSOR: pressure, CELLS_M_SENSOR: pressure, BEADS_M_SENSOR: pressure},
            trigger_type="time",
            trigger_params={"duration_s": duration_s},
            on_complete="zero",
            confirm_message=f"Set all pressure channels to {pressure:g} mbar for {duration_s:g} seconds?",
        ),
        ProtocolStep(
            name="Confirm wash complete",
            sensor_setpoints={},
            trigger_type="time",
            trigger_params={"duration_s": 0.0},
            confirm_message="Pressure wash complete. Confirm pipeline close.",
        ),
    ]

def expand_protocol_steps(steps: list[ProtocolStep]) -> list[ProtocolStep]:
    expanded = []
    index = 0
    while index < len(steps):
        step = steps[index]
        if step.group:
            group_steps = []
            group_repeat = 1
            while index < len(steps) and steps[index].group == step.group:
                group_steps.append(steps[index])
                group_repeat = max(group_repeat, steps[index].repeat)
                index += 1
            for _ in range(group_repeat):
                expanded.extend(group_steps)
        else:
            for _ in range(max(1, step.repeat)):
                expanded.append(step)
            index += 1
    return expanded

CHARACTERISE_FRACTIONS = (0.2, 0.4, 0.6, 0.8, 1.0)

GRAVIMETRY_CHANNELS = (
    (OIL_L_SENSOR, "Oil L"),
    (CELLS_M_SENSOR, "Cells M"),
    (BEADS_M_SENSOR, "Beads M"),
)

def protocol_names() -> tuple[str, ...]:
    """Every name build_protocol accepts."""
    return tuple(sorted(PROTOCOLS))

def build_gravimetry_protocol(settings: dict) -> list[ProtocolStep]:
    """Dispense a weighed volume from every channel, one replicate at a time.

    Each replicate is two gated steps: one before, so the tube on the outlet is
    the one that was weighed empty, and one after, so the dispense is not followed
    by another until its mass has been written down. The scale is not wired to
    anything -- the operator types the weights into the dispense table, and this
    protocol only guarantees that what lands in the tube is the commanded volume.
    """
    target_ul = float(settings["gravimetric_target_ul"])
    flow_ul_min = float(settings["gravimetric_flow_ul_min"])
    replicates = int(settings["gravimetric_replicates"])
    steps: list[ProtocolStep] = []
    for sensor, label in GRAVIMETRY_CHANNELS:
        for replicate in range(1, replicates + 1):
            position = f"{label} replicate {replicate} of {replicates}"
            steps.extend(
                (
                    ProtocolStep(
                        name=f"Dispense {label} {replicate}",
                        sensor_setpoints={sensor: flow_ul_min},
                        trigger_type="volume",
                        trigger_params={
                            "sensor_index": sensor,
                            "target_volume_ul": target_ul,
                        },
                        on_complete="zero",
                        confirm_message=(
                            f"{position}: weigh an empty tube, note its mass and put it on "
                            f"the {label} outlet. Dispensing {target_ul:g} uL at "
                            f"{flow_ul_min:g} uL/min."
                        ),
                    ),
                    ProtocolStep(
                        name=f"Weigh {label} {replicate}",
                        sensor_setpoints={},
                        trigger_type="time",
                        trigger_params={"duration_s": 0.0},
                        confirm_message=(
                            f"{position} dispensed. Weigh the tube and enter empty and full "
                            f"into the dispense table, then continue."
                        ),
                    ),
                )
            )
    return steps

def build_characterise_protocol(settings: dict) -> list[ProtocolStep]:
    """Sweep the setup to measure what the plumbing and chip cost.

    Every channel is scaled by the same fraction of its working flow, so the phase
    ratio holds throughout. That is what keeps each channel linear in its own flow
    and lets the three be fitted separately afterwards.

    Flow regulation is used rather than open-loop pressure: driving pressure would
    let each channel land wherever its own resistance put it and the ratio would
    not hold. The pressure the controller settles at is the measurement.
    """
    oil = float(settings["run_oil_flow_ul_min"])
    aqueous = float(settings["run_aqueous_total_flow_ul_min"]) / 2.0
    # What counts as settled, and how long to wait for it. A rig that hunts around
    # its setpoint needs a looser tolerance or a longer window; one that saturates
    # needs a shorter timeout so the sweep is not spent waiting on it.
    settle = {
        "sensor_index": OIL_L_SENSOR,
        "tolerance_ul_min": float(
            settings.get("sweep_tolerance_ul_min", STABILITY_TOLERANCE_UL_MIN)
        ),
        "window_s": float(settings.get("sweep_window_s", STABILITY_DURATION_S)),
        "timeout_s": float(settings.get("sweep_timeout_s", STABILITY_TIMEOUT_S)),
    }
    steps: list[ProtocolStep] = []
    for fraction in CHARACTERISE_FRACTIONS:
        steps.append(
            ProtocolStep(
                name=f"Sweep {fraction * 100:.0f}%",
                sensor_setpoints={
                    OIL_L_SENSOR: oil * fraction,
                    CELLS_M_SENSOR: aqueous * fraction,
                    BEADS_M_SENSOR: aqueous * fraction,
                },
                trigger_type="stability",
                trigger_params=dict(settle),
                on_complete="hold",
                group="characterise",
            )
        )
    steps.append(
        ProtocolStep(
            name="Stop",
            sensor_setpoints={OIL_L_SENSOR: 0.0, CELLS_M_SENSOR: 0.0, BEADS_M_SENSOR: 0.0},
            trigger_type="time",
            trigger_params={"duration_s": 1.0},
            on_complete="zero",
            group="characterise",
        )
    )
    return steps


# Every protocol this build can run, by the name the operator selects.
PROTOCOLS: dict[str, Callable[[dict], list[ProtocolStep]]] = {
    "Priming": build_priming_protocol,
    "Drop-Seq": build_dropseq_protocol,
    "Wash": build_wash_protocol,
    "Characterise": build_characterise_protocol,
    "Gravimetry": build_gravimetry_protocol,
}
