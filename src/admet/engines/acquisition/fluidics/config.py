from __future__ import annotations

ACQUISITION_INTERVAL_MS = 100
STATS_WINDOW_SAMPLES = 300
STABILITY_TOLERANCE_UL_MIN = 2.0
STABILITY_DURATION_S = 5.0
STABILITY_WINDOW_SAMPLES = int(STABILITY_DURATION_S / (ACQUISITION_INTERVAL_MS / 1000.0))

FLUIDIC_CHANNELS = (
    ("oil_l", "Oil L", "IPA", 2.25, 0.0, 0.0),
    ("cells_m", "Cells M", "H2O", 1.0, 0.0, 0.0),
    ("beads_m", "Beads M", "H2O", 1.0, 0.0, 0.0),
)
FLUIDIC_CHANNEL_LABELS = tuple(label for _key, label, *_rest in FLUIDIC_CHANNELS)

# Which flow unit each channel carries. Liquid profiles are written per unit type,
# so a channel is only offered the profiles for its own unit (M1/M2 are the same).
FLUIDIC_CHANNEL_UNITS = {
    "oil_l": "L",
    "cells_m": "M",
    "beads_m": "M",
}
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

SENSOR_CALIBRATIONS = {
    "None": 0,
    "H2O": 1,
    "IPA": 2,
    "HFE": 3,
    "FC40": 4,
    "Oil": 5,
}
