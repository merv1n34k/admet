from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "cellpose_flow_threshold": 0.4,
    "cellpose_cellprob_threshold": 0.0,
    "erosion_pixels": 5,
    "kernel_size": 7,
    "tophat_threshold": 30,
    "min_inclusion_area": 7,
    "max_inclusion_area": 50,
    "edge_buffer": 5,
    "min_droplet_diameter": 80,
    "max_droplet_diameter": 200,
    "px_to_um": 1.14,
    "cache": {
        "enabled": True,
        "max_frames": 100,
        "strategy": "lru",
        "dir": None,
    },
    "settings": {
        "dilution": 500,
        "poisson": True,
        "count": 6.5e5,
        "inclusions": True,
    },
}


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    path = _find_config(config_path)
    if path is None:
        return config

    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    _deep_update(config, loaded)
    return config


def _find_config(config_path: str | Path | None) -> Path | None:
    if config_path is not None:
        path = Path(config_path)
        return path if path.exists() else None
    candidate = Path.cwd() / "config.json"
    return candidate if candidate.exists() else None


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value
