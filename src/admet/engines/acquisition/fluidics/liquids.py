"""Liquid profiles for the Fluigent flow units.

A profile bundles everything that changes when a channel carries a different
liquid: the sensor calibration table and the custom-scale terms that correct the
reading, plus the density used to derive that correction gravimetrically.

Profiles live in liquids.json next to this module and are meant to be edited by
hand; there is deliberately no UI for creating them.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

LIQUIDS_FILE = Path(__file__).with_name("liquids.json")


@dataclass(frozen=True)
class LiquidProfile:
    id: str
    name: str
    unit: str
    calibration: str
    scale: float
    offset: float = 0.0
    quadratic: float = 0.0
    density: float = 0.0
    viscosity: float = 0.0

    def corrections(self, prefix: str) -> dict[str, object]:
        """Correction settings this profile applies to the given channel."""
        return {
            f"{prefix}_calibration": self.calibration,
            f"{prefix}_scale": self.scale,
            f"{prefix}_offset": self.offset,
            f"{prefix}_quadratic": self.quadratic,
        }

    def summary(self) -> str:
        text = f"{self.calibration} table, scale {self.scale:g}"
        if self.offset:
            text += f", offset {self.offset:g}"
        if self.quadratic:
            text += f", quadratic {self.quadratic:g}"
        if self.density:
            text += f", {self.density:g} g/mL"
        if self.viscosity:
            text += f", {self.viscosity:g} mPa.s"
        return text


@lru_cache(maxsize=1)
def load_profiles() -> tuple[LiquidProfile, ...]:
    try:
        data = json.loads(LIQUIDS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Could not read liquid profiles from %s: %s", LIQUIDS_FILE, exc)
        return ()

    return parse_profiles(data.get("profiles", ()))


def parse_profiles(entries) -> tuple[LiquidProfile, ...]:
    profiles = []
    for entry in entries if isinstance(entries, (list, tuple)) else ():
        if not isinstance(entry, dict):
            continue
        try:
            profiles.append(
                LiquidProfile(
                    id=str(entry["id"]),
                    name=str(entry["name"]),
                    unit=str(entry["unit"]).upper(),
                    calibration=str(entry["calibration"]),
                    scale=float(entry["scale"]),
                    offset=float(entry.get("offset", 0.0)),
                    quadratic=float(entry.get("quadratic", 0.0)),
                    density=float(entry.get("density", 0.0)),
                    viscosity=float(entry.get("viscosity", 0.0)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.error("Skipping malformed liquid profile %r: %s", entry, exc)
    return tuple(profiles)


def profiles_for_unit(unit: str) -> tuple[LiquidProfile, ...]:
    return tuple(profile for profile in load_profiles() if profile.unit == unit.upper())


def profile_by_id(profile_id: str) -> LiquidProfile | None:
    return next((profile for profile in load_profiles() if profile.id == profile_id), None)


def default_profile_id(prefix: str, unit: str, calibration: str, scale: float) -> str:
    """Profile matching a channel's built-in correction defaults.

    Falls back to the first profile for the unit so a channel always starts on a
    real profile even if the JSON no longer describes its original defaults.
    """
    candidates = profiles_for_unit(unit)
    exact = next(
        (p for p in candidates if p.calibration == calibration and p.scale == scale),
        None,
    )
    if exact is not None:
        return exact.id
    named = next((p for p in candidates if p.id == prefix), None)
    if named is not None:
        return named.id
    return candidates[0].id if candidates else ""


CORRECTION_FIELDS = ("calibration", "scale", "offset", "quadratic")


def rig_corrections(saved: dict, prefix: str, profile: LiquidProfile) -> tuple[dict[str, object], str]:
    """A channel's corrections for this liquid on this rig, and when they were set.

    The project keeps each channel's own values per liquid; a liquid never
    calibrated on this channel falls back to its profile, with no date.
    """
    channel = saved.get(prefix) if isinstance(saved, dict) else None
    liquids = channel.get("liquids") if isinstance(channel, dict) else None
    entry = liquids.get(profile.id) if isinstance(liquids, dict) else None
    if isinstance(entry, dict) and all(field in entry for field in CORRECTION_FIELDS):
        return ({f"{prefix}_{field}": entry[field] for field in CORRECTION_FIELDS},
                str(entry.get("updated_at") or ""))
    return profile.corrections(prefix), ""


def remember_corrections(saved: dict, prefix: str, profile_id: str,
                         values: dict[str, object] | None = None, updated_at: str = "") -> dict:
    """The project calibration with this channel on this liquid, and its values if given."""
    saved = dict(saved) if isinstance(saved, dict) else {}
    channel = dict(saved.get(prefix) or {})
    liquids = dict(channel.get("liquids") or {})
    if values is not None:
        liquids[profile_id] = {**{field: values[f"{prefix}_{field}"] for field in CORRECTION_FIELDS},
                               "updated_at": updated_at}
    saved[prefix] = {**channel, "profile": profile_id, "liquids": liquids}
    return saved


def check_liquid(entry: dict[str, object]) -> dict[str, object]:
    """A project liquid entry with a usable name, unit, sensor table and properties."""
    from admet.engines.acquisition.fluidics.config import SENSOR_CALIBRATIONS

    entry = {**entry, "name": str(entry.get("name", "")).strip(), "unit": str(entry.get("unit", "")).upper()}
    if not entry["name"]:
        raise ValueError("Name the liquid")
    if entry["unit"] not in ("L", "M"):
        raise ValueError("Flow unit must be L or M")
    if entry.get("calibration") not in SENSOR_CALIBRATIONS:
        raise ValueError(f"Unknown sensor table: {entry.get('calibration')}")
    density, viscosity = float(entry.get("density", 0)), float(entry.get("viscosity", 0))
    if not density > 0 or viscosity < 0:
        raise ValueError("Density must be positive and viscosity not negative")
    return {**entry, "density": density, "viscosity": viscosity}


def new_liquid(name: str, unit: str, calibration: str, density: float, viscosity: float,
               taken: set[str]) -> dict[str, object]:
    """A project liquid: starts uncorrected (scale 1) until calibrated on the rig."""
    entry = check_liquid({"name": name, "unit": unit, "calibration": calibration,
                          "density": density, "viscosity": viscosity})
    base = re.sub(r"[^a-z0-9]+", "_", entry["name"].lower()).strip("_") or "liquid"
    unit = entry["unit"].lower()
    liquid_id, suffix = f"{base}_{unit}", 2
    while liquid_id in taken:
        liquid_id, suffix = f"{base}_{unit}_{suffix}", suffix + 1
    return {"id": liquid_id, **entry, "scale": 1.0, "offset": 0.0, "quadratic": 0.0}


def dead_volume(saved: dict, prefix: str) -> float:
    channel = saved.get(prefix) if isinstance(saved, dict) else None
    value = channel.get("dead_volume_ul") if isinstance(channel, dict) else None
    return float(value) if isinstance(value, (int, float)) and value >= 0 else 0.0


def remember_dead_volume(saved: dict, prefix: str, value: float) -> dict:
    """The project calibration with this channel's dead volume, whatever liquid it carries."""
    saved = dict(saved) if isinstance(saved, dict) else {}
    saved[prefix] = {**(saved.get(prefix) or {}), "dead_volume_ul": float(value)}
    return saved
