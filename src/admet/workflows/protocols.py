"""Shared Priming and Wash builders for Qt and named Python operations.

Experiments are JSON protocols; the engine executes their declared steps.
"""

from __future__ import annotations

from collections.abc import Callable

from admet.engines.acquisition.fluidics.config import (
    BEADS_M_SENSOR,
    CELLS_M_SENSOR,
    OIL_L_SENSOR,
    PRIMING_AQUEOUS_FLOW_UL_MIN,
    PRIMING_OIL_FLOW_UL_MIN,
)
from admet.engines.acquisition.pipeline import ProtocolStep


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


def protocol_names() -> tuple[str, ...]:
    """Every name build_protocol accepts."""
    return tuple(sorted(PROTOCOLS))


PROTOCOLS: dict[str, Callable[[dict], list[ProtocolStep]]] = {
    "Priming": build_priming_protocol,
    "Wash": build_wash_protocol,
}
