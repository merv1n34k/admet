"""Droplets as what is darker than the empty channel.

A droplet darkens its rim against the channel, and often its whole footprint, by more
than the camera noise, so a threshold set a few times above the noise gives its outline.
The background is the bright side of each pixel over the video, each frame is matched to
it in brightness before differencing, a closed rim is filled, and a rim left open where
the light passed through is completed by its convex hull when that hull is the size and
shape of a droplet. An opening the size of a satellite droplet then keeps satellites, wall
lines and specks off the main droplets.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

DEFAULTS: dict[str, Any] = {
    "k_sigma": 4.0,          # threshold = k x the frame's noise
    "satellite": 13,         # px: what an opening this wide removes is not a main droplet
    "min_area": 800.0,       # px²: a droplet of 32 px across; satellites and specks are smaller
    "max_area": 50000.0,     # px²
    "min_solidity": 0.85,
    "min_threshold": 2.0,    # levels: the threshold never falls below this, however quiet the frame
    "min_roundness": 0.7,    # of a hull, to complete an open rim with it
    "skip_left_pct": 0.0,    # % of the width left out on the left (the nozzle)
    "skip_right_pct": 0.0,   # % of the width left out on the right (the outlet)
    "border": 3,             # px: a droplet this close to the top or bottom edge is cut off
    "blur": 3,
}


def percentile_background(frames: list[np.ndarray], percentile: float = 85) -> np.ndarray:
    """The bright side of each pixel over frames sampled across the video.

    Droplets are darker than the channel, so a high percentile over time is the empty
    channel even where droplets pass most of the time, and it follows slow lighting drift.
    """
    return np.percentile(np.stack(frames), percentile, axis=0).astype(np.uint8)


def match_brightness(frame: np.ndarray, background: np.ndarray) -> np.ndarray:
    """The frame with its brightness matched to the background's.

    Lighting drifts by a few percent over a recording. A gain and offset fitted from two
    intensity quantiles bring the frame onto the background; droplets are a minority of
    pixels, so they hardly move the quantiles.
    """
    f20, f80 = np.percentile(frame, (20, 80))
    b20, b80 = np.percentile(background, (20, 80))
    gain = (b80 - b20) / (f80 - f20) if f80 > f20 else 1.0
    gain = min(max(gain, 0.7), 1.4)
    return cv2.convertScaleAbs(frame, alpha=gain, beta=b80 - gain * f80)


def window(width: int, config: dict[str, Any]) -> tuple[int, int]:
    """The columns droplets are measured in: the frame less the skipped ends, in px."""
    left = int(round(width * float(config.get("skip_left_pct", 0.0)) / 100))
    right = width - int(round(width * float(config.get("skip_right_pct", 0.0)) / 100))
    return (left, right) if left < right else (0, width)


def detect(frame: np.ndarray, background: np.ndarray, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Droplets in one frame, with every stage kept for viewing.

    Returns properties (as the analysis rows carry them), rejected shapes with why,
    satellites, the threshold and noise, and the images matched, darker, mask, bodies.
    """
    cfg = {**DEFAULTS, **(config or {})}
    matched = match_brightness(frame, background)
    darker = cv2.subtract(background, matched)              # clipped at 0: only darker than background counts
    blur = int(cfg["blur"])
    if blur > 1:
        darker = cv2.GaussianBlur(darker, (blur | 1, blur | 1), 0)
    # Noise from the whole frame: droplets are a minority of pixels, so the MAD is the background's.
    sigma = 1.4826 * float(np.median(np.abs(darker.astype(np.float32) - np.median(darker)))) + 0.5
    threshold = max(float(cfg["min_threshold"]), float(cfg["k_sigma"]) * sigma)
    _, mask = cv2.threshold(darker, threshold, 255, cv2.THRESH_BINARY)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, _ellipse(5))
    padded = cv2.copyMakeBorder(mask, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    fill = np.zeros((padded.shape[0] + 2, padded.shape[1] + 2), np.uint8)
    cv2.floodFill(padded, fill, (0, 0), 255)
    filled = cv2.bitwise_or(mask, cv2.bitwise_not(padded)[1:-1, 1:-1])
    completed = complete_rims(filled, float(cfg["min_area"]), float(cfg["max_area"]), float(cfg["min_solidity"]),
                              float(cfg["min_roundness"]))
    bodies = cv2.morphologyEx(filled, cv2.MORPH_OPEN, _ellipse(max(3, int(cfg["satellite"]))))
    crumbs = cv2.subtract(filled, cv2.dilate(bodies, _ellipse(5)))

    height, width = frame.shape[:2]
    low, high = window(width, cfg)
    border = int(cfg["border"])
    droplets, contours, rejected, satellites = [], [], [], []
    for contour in cv2.findContours(bodies, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        props = properties(contour)
        x, y, w, h = props["bbox_x"], props["bbox_y"], props["bbox_width"], props["bbox_height"]
        if not (x > low and x + w < high and y > border and y + h < height - border):
            props["why"] = "outside window"
        elif not float(cfg["min_area"]) <= props["area"] <= float(cfg["max_area"]):
            props["why"] = "size"
        elif props["solidity"] < float(cfg["min_solidity"]):
            props["why"] = "shape"
        if "why" in props:
            rejected.append({**props, "contour": contour})
        else:
            props["contour_points"] = contour.reshape(-1, 2).astype(np.int16)
            droplets.append(props)
            contours.append(contour)
    for contour in cv2.findContours(crumbs, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        area = cv2.contourArea(contour)
        x, y, w, h = cv2.boundingRect(contour)
        if 15 <= area < float(cfg["min_area"]) and low < x and x + w < high:
            satellites.append({"area": area, "centroid_x": x + w / 2, "centroid_y": y + h / 2, "contour": contour})
    return {"properties": droplets, "contours": contours, "rejected": rejected, "satellites": satellites, "sigma": sigma,
            "threshold": threshold, "window": (low, high), "completed": completed,
            "images": {"matched": matched, "darker": darker, "mask": mask, "bodies": bodies}}


def complete_rims(filled: np.ndarray, min_area: float, max_area: float, min_solidity: float,
                  min_roundness: float) -> int:
    """Fill, in place, each open rim whose convex hull is a droplet: the right size, round, and far
    from solid itself. A droplet lit from one side shows a dark rim on that side only, so the fill
    from outside leaks in; its hull is still the droplet. How many were completed."""
    completed = 0
    for contour in cv2.findContours(filled, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        hull = cv2.convexHull(contour)
        hull_area = float(cv2.contourArea(hull))
        if not min_area <= hull_area <= max_area or float(cv2.contourArea(contour)) >= min_solidity * hull_area:
            continue
        perimeter = float(cv2.arcLength(hull, True))
        _, _, w, h = cv2.boundingRect(hull)
        roundness = 4 * np.pi * hull_area / perimeter ** 2 if perimeter > 0 else 0.0
        if roundness < min_roundness or not 0.6 <= w / max(h, 1) <= 1.7:
            continue
        cv2.fillConvexPoly(filled, hull, 255)
        completed += 1
    return completed


def properties(contour) -> dict[str, Any]:
    """A droplet's measurements, as the analysis rows carry them."""
    area = float(cv2.contourArea(contour))
    perimeter = float(cv2.arcLength(contour, True))
    moments = cv2.moments(contour)
    cx = moments["m10"] / moments["m00"] if moments["m00"] else 0.0
    cy = moments["m01"] / moments["m00"] if moments["m00"] else 0.0
    x, y, w, h = cv2.boundingRect(contour)
    hull = float(cv2.contourArea(cv2.convexHull(contour)))
    return {
        "area": area,
        "perimeter": perimeter,
        "centroid_x": float(cx),
        "centroid_y": float(cy),
        "bbox_x": int(x),
        "bbox_y": int(y),
        "bbox_width": int(w),
        "bbox_height": int(h),
        "circularity": 4 * np.pi * area / perimeter ** 2 if perimeter > 0 else 0.0,
        "solidity": area / hull if hull else 0.0,
        "equivalent_diameter": float(np.sqrt(4 * area / np.pi)),
    }


def _ellipse(size: int) -> np.ndarray:
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
