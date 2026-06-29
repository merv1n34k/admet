from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np


def update_results_with_inclusions(
    results: Iterable[Mapping[str, Any]],
    inclusions_by_frame: Mapping[int, Iterable[tuple[int, int]]],
    disabled_droplets: Mapping[int, set[int]] | None = None,
    *,
    detect_inclusions: bool = True,
) -> list[dict[str, Any]]:
    disabled_droplets = disabled_droplets or {}
    corrected = []
    for row in results:
        item = dict(row)
        frame_idx = int(item["frame"])
        droplet_id = int(item["droplet_id"])
        if droplet_id in disabled_droplets.get(frame_idx, set()):
            continue

        count = 0
        if detect_inclusions:
            cx = float(item["center_x"])
            cy = float(item["center_y"])
            radius = float(item["diameter_px"]) / 2.0
            for ix, iy in inclusions_by_frame.get(frame_idx, []):
                if np.hypot(float(ix) - cx, float(iy) - cy) <= radius:
                    count += 1

        item["inclusions"] = count
        item["detected"] = True
        corrected.append(item)
    return corrected
