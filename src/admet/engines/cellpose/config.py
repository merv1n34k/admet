from __future__ import annotations

import copy
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
        "max_entries": 100,
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


def load_config() -> dict[str, Any]:
    """Return a copy of the built-in detection config (no external file loading)."""
    return copy.deepcopy(DEFAULT_CONFIG)
