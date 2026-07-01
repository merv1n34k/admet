from __future__ import annotations

from dataclasses import dataclass, field

ACQUISITION_INTERVAL_MS = 100
STATS_WINDOW_SAMPLES = 300
STABILITY_TOLERANCE_UL_MIN = 2.0
STABILITY_DURATION_S = 5.0
STABILITY_WINDOW_SAMPLES = int(STABILITY_DURATION_S / (ACQUISITION_INTERVAL_MS / 1000.0))

PIPELINE_TICK_MS = 200

FLUIDIC_CHANNELS = (
    ("oil_l", "Oil L", "IPA", 2.25, 0.0, 0.0),
    ("cells_m", "Cells M", "H2O", 1.0, 0.0, 0.0),
    ("beads_m", "Beads M", "H2O", 1.0, 0.0, 0.0),
)
FLUIDIC_CHANNEL_LABELS = tuple(label for _key, label, *_rest in FLUIDIC_CHANNELS)
OIL_L_SENSOR = 0
CELLS_M_SENSOR = 1
BEADS_M_SENSOR = 2

PRIMING_OIL_FLOW_UL_MIN = 250.0
PRIMING_AQUEOUS_FLOW_UL_MIN = 67.0
DROPSEQ_OIL_FLOW_UL_MIN = 300.0

SIM_INSTR_TYPE = 4
SIM_INSTRUMENTS = [
    {"serial": 1001, "config": [1, 100, 0, 5, 7, 0, 0, 0, 0, 0]},
    {"serial": 1002, "config": [1, 101, 0, 5, 4, 0, 0, 0, 0, 0]},
    {"serial": 1003, "config": [1, 102, 0, 5, 4, 0, 0, 0, 0, 0]},
]

SENSOR_REAL_SMAX = {
    "Flow_L": 5000.0,
    "Flow_M": 80.0,
}

PRESSURE_CHANNEL_NAMES = list(FLUIDIC_CHANNEL_LABELS)
SENSOR_CHANNEL_NAMES = list(FLUIDIC_CHANNEL_LABELS)

SENSOR_CALIBRATIONS = {
    "None": 0,
    "H2O": 1,
    "IPA": 2,
    "HFE": 3,
    "FC40": 4,
    "Oil": 5,
}


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


PIPELINES: dict[str, list[ProtocolStep]] = {
    "Drop-Seq": [
        ProtocolStep(
            name="Prerun",
            sensor_setpoints={0: 250.0, 1: 67.0, 2: 67.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 0, "target_volume_ul": 75.0},
            on_complete="zero",
        ),
        ProtocolStep(
            name="Run-prestab",
            sensor_setpoints={0: 250.0, 1: 0.0, 2: 0.0},
            trigger_type="condition",
            trigger_params={"sensor_index": 0, "min_value": 125.0},
            confirm_message="Prerun complete. Start stabilization?",
            group="run",
            repeat=3,
        ),
        ProtocolStep(
            name="Run-stab",
            sensor_setpoints={0: 250.0, 1: 67.0, 2: 67.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 0, "target_volume_ul": 250.0},
            on_complete="zero",
            group="run",
            repeat=3,
        ),
    ],
    "Priming": [
        ProtocolStep(
            name="Prime Oil L",
            sensor_setpoints={0: 250.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 0, "target_volume_ul": 40.0},
            on_complete="zero",
            confirm_message="Prime Oil L at 250 uL/min for 40 uL. Proceed?",
        ),
        ProtocolStep(
            name="Prime Cells M",
            sensor_setpoints={1: 67.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 1, "target_volume_ul": 5.0},
            on_complete="zero",
            confirm_message="Prime Cells M at 67 uL/min for 5 uL. Proceed?",
        ),
        ProtocolStep(
            name="Prime Beads M",
            sensor_setpoints={2: 67.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 2, "target_volume_ul": 5.0},
            on_complete="zero",
            confirm_message="Prime Beads M at 67 uL/min for 5 uL. Proceed?",
        ),
    ],
    "Wash": [
        ProtocolStep(
            name="Wash flow phase",
            sensor_setpoints={0: 250.0, 1: 80.0, 2: 80.0},
            trigger_type="volume",
            trigger_params={"sensor_index": 0, "target_volume_ul": 500.0},
            on_complete="zero",
            confirm_message="Start wash phase: 250/80/80 uL/min until Oil L dispenses 500 uL?",
        ),
        ProtocolStep(
            name="Wash pressure phase",
            sensor_setpoints={},
            trigger_type="time",
            trigger_params={"duration_s": 120.0},
            pressure_setpoints={0: 2000.0, 1: 2000.0, 2: 2000.0},
            on_complete="zero",
            confirm_message="Set all pressure channels to 2000 mbar for 120 seconds?",
        ),
        ProtocolStep(
            name="Confirm wash complete",
            sensor_setpoints={},
            trigger_type="time",
            trigger_params={"duration_s": 0.0},
            confirm_message="Pressure wash complete. Confirm pipeline close.",
        ),
    ],
}


def build_protocol(name: str, settings: dict | None = None) -> list[ProtocolStep]:
    if name == "Priming" and settings:
        return build_priming_protocol(settings)
    if name == "Drop-Seq" and settings:
        return build_dropseq_protocol(settings)
    if name == "Wash" and settings:
        return build_wash_protocol(settings)
    protocol = PIPELINES.get(name)
    if protocol is None:
        raise ValueError(f"Unknown pipeline: {name}")
    return list(protocol)


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
                            OIL_L_SENSOR: DROPSEQ_OIL_FLOW_UL_MIN,
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
                            f"Start {label}: Oil L {DROPSEQ_OIL_FLOW_UL_MIN:g} uL/min, "
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
