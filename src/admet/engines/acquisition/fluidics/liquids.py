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

    profiles = []
    for entry in data.get("profiles", ()):
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
