"""Liquids, and the correction profiles that tell a flow unit how to read them.

A liquid is what flows: its density and viscosity. A profile is how one kind of
flow unit (L or M) reads a liquid: the sensor table and the custom-scale terms
that correct the reading. Each channel of the rig selects one profile.

Defaults come from liquids.json next to this module and are read-only; a project
adds its own liquids and profiles alongside them.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

LIQUIDS_FILE = Path(__file__).with_name("liquids.json")


@dataclass(frozen=True)
class Liquid:
    id: str
    name: str
    density: float
    viscosity: float = 0.0
    default: bool = False
    changed: str = ""


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    liquid: str
    unit: str
    calibration: str
    scale: float
    square: float = 0.0
    cube: float = 0.0
    default: bool = False
    changed: str = ""

    def corrections(self, prefix: str) -> dict[str, object]:
        """The channel's correction settings; the engine names the square and cube terms offset and quadratic."""
        return {
            f"{prefix}_calibration": self.calibration,
            f"{prefix}_scale": self.scale,
            f"{prefix}_offset": self.square,
            f"{prefix}_quadratic": self.cube,
        }

    def summary(self) -> str:
        text = f"{self.calibration} table, scale {self.scale:g}"
        if self.square:
            text += f", square {self.square:g}"
        if self.cube:
            text += f", cube {self.cube:g}"
        return text


@lru_cache(maxsize=1)
def _defaults() -> tuple[tuple[Liquid, ...], tuple[Profile, ...]]:
    try:
        data = json.loads(LIQUIDS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Could not read default liquids from %s: %s", LIQUIDS_FILE, exc)
        return (), ()
    return parse_liquids(data.get("liquids"), default=True), parse_profiles(data.get("profiles"), default=True)


def default_liquids() -> tuple[Liquid, ...]:
    return _defaults()[0]


def default_profiles() -> tuple[Profile, ...]:
    return _defaults()[1]


def parse_liquids(entries, *, default: bool = False) -> tuple[Liquid, ...]:
    liquids = []
    for entry in entries if isinstance(entries, (list, tuple)) else ():
        try:
            liquids.append(Liquid(id=str(entry["id"]), name=str(entry["name"]), density=float(entry["density"]),
                                  viscosity=float(entry.get("viscosity", 0.0)), default=default,
                                  changed=str(entry.get("updated_at") or "")))
        except (KeyError, TypeError, ValueError) as exc:
            log.error("Skipping malformed liquid %r: %s", entry, exc)
    return tuple(liquids)


def parse_profiles(entries, *, default: bool = False) -> tuple[Profile, ...]:
    profiles = []
    for entry in entries if isinstance(entries, (list, tuple)) else ():
        try:
            profiles.append(Profile(
                id=str(entry["id"]), name=str(entry["name"]), liquid=str(entry["liquid"]),
                unit=str(entry["unit"]).upper(), calibration=str(entry["calibration"]),
                scale=float(entry["scale"]), square=float(entry.get("square", 0.0)),
                cube=float(entry.get("cube", 0.0)), default=default, changed=str(entry.get("updated_at") or "")))
        except (KeyError, TypeError, ValueError) as exc:
            log.error("Skipping malformed profile %r: %s", entry, exc)
    return tuple(profiles)


def default_profile_id(prefix: str, unit: str, calibration: str, scale: float) -> str:
    """The default profile matching a channel's built-in correction, else the first for its unit."""
    candidates = [p for p in default_profiles() if p.unit == unit.upper()]
    exact = next((p for p in candidates if p.calibration == calibration and p.scale == scale), None)
    return exact.id if exact else (candidates[0].id if candidates else "")


def unique_id(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "entry"
    candidate, suffix = base, 2
    while candidate in taken:
        candidate, suffix = f"{base}_{suffix}", suffix + 1
    return candidate


def check_liquid(entry: dict[str, object]) -> dict[str, object]:
    """A liquid entry with a name, a positive density and a non-negative viscosity."""
    name = str(entry.get("name", "")).strip()
    if not name:
        raise ValueError("Name the liquid")
    try:
        density, viscosity = float(entry.get("density", 0)), float(entry.get("viscosity", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("Density and viscosity must be numbers") from exc
    if not (math.isfinite(density) and math.isfinite(viscosity)) or not density > 0 or viscosity < 0:
        raise ValueError("Density must be positive and viscosity not negative")
    return {"id": entry.get("id"), "name": name, "density": density, "viscosity": viscosity}


def check_profile(entry: dict[str, object], liquid_ids: set[str]) -> dict[str, object]:
    """A profile entry naming a known liquid, an L or M unit, a sensor table and finite terms."""
    from admet.engines.acquisition.fluidics.config import SENSOR_CALIBRATIONS

    name = str(entry.get("name", "")).strip()
    if not name:
        raise ValueError("Name the profile")
    if entry.get("liquid") not in liquid_ids:
        raise ValueError("Choose a liquid for the profile")
    unit = str(entry.get("unit", "")).upper()
    if unit not in ("L", "M"):
        raise ValueError("Flow unit must be L or M")
    if entry.get("calibration") not in SENSOR_CALIBRATIONS:
        raise ValueError(f"Unknown sensor table: {entry.get('calibration')}")
    try:
        scale, square, cube = (float(entry.get(key, 0.0)) for key in ("scale", "square", "cube"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Scale, square and cube must be numbers") from exc
    if not all(math.isfinite(v) for v in (scale, square, cube)) or scale == 0:
        raise ValueError("Scale must be a non-zero number, square and cube finite")
    return {"id": entry.get("id"), "name": name, "liquid": entry["liquid"], "unit": unit,
            "calibration": entry["calibration"], "scale": scale, "square": square, "cube": cube}


def channel_profile(saved: dict, prefix: str) -> str:
    channel = saved.get(prefix) if isinstance(saved, dict) else None
    return str(channel.get("profile") or "") if isinstance(channel, dict) else ""


def remember_profile(saved: dict, prefix: str, profile_id: str) -> dict:
    """The project calibration with this channel on this profile."""
    saved = dict(saved) if isinstance(saved, dict) else {}
    saved[prefix] = {**(saved.get(prefix) or {}), "profile": profile_id}
    return saved


def dead_volume(saved: dict, prefix: str) -> float:
    channel = saved.get(prefix) if isinstance(saved, dict) else None
    value = channel.get("dead_volume_ul") if isinstance(channel, dict) else None
    return float(value) if isinstance(value, (int, float)) and value >= 0 else 0.0


def remember_dead_volume(saved: dict, prefix: str, value: float) -> dict:
    """The project calibration with this channel's dead volume, whatever profile it uses."""
    saved = dict(saved) if isinstance(saved, dict) else {}
    saved[prefix] = {**(saved.get(prefix) or {}), "dead_volume_ul": float(value)}
    return saved
