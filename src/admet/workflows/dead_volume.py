"""Dead volume measured by hand: the operator runs the channel and enters each value."""

from copy import deepcopy
import math
from statistics import mean, stdev

from admet.workflows.calculation_schema import measurement_binding
from admet.workflows.gravimetry import measurement

# Two-sided 95 % Student t for n - 1 degrees of freedom.
_T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}


def normalize_calculation(entry, steps, fields):
    if set(entry) != {"type", "channel", "liquid", "volumes"}:
        raise ValueError("dead_volume requires type, channel, liquid and volumes")
    if type(entry["channel"]) is not int or entry["channel"] < 0:
        raise ValueError("calculation channel must be a non-negative integer")
    if not isinstance(entry["liquid"], str) or not entry["liquid"].strip():
        raise ValueError("liquid must name the liquid in the channel")
    volumes = entry["volumes"]
    if not isinstance(volumes, list) or not 2 <= len(volumes) <= 10 or len(set(volumes)) != len(volumes):
        raise ValueError("dead_volume needs 2 to 10 distinct volume measurements")
    for key in volumes:
        measurement_binding(fields, key, {"uL"})
    return deepcopy(entry)


def check(context):
    for key in context["config"]["volumes"]:
        if measurement(context, key) <= 0:
            raise ValueError(f"{key}: a dead volume must be positive")


def calculate(context):
    check(context)
    config = context["config"]
    values = [measurement(context, key) for key in config["volumes"]]
    volume, sd = mean(values), stdev(values)
    sem = sd / math.sqrt(len(values))
    t = _T975[len(values) - 1]
    return {"status": "usable", "channel": config["channel"], "liquid": config["liquid"],
            "volume_ul": volume, "volumes_ul": values, "sd_ul": sd, "cv": sd / volume,
            "ci95_ul": [volume - t * sem, volume + t * sem], "issues": [],
            "note": "Mean of the dead volumes entered by hand. The interval is repeatability only."}
