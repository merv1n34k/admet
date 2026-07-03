"""
Automated binary threshold value selection (BTVS).
"""

import cv2
import numpy as np
from typing import List, Dict
from .logger import get_logger

logger = get_logger("BTVF")


class ThresholdFinder:
    """Threshold finder using the ADM paper's BTVS threshold sweep."""

    def __init__(self, config: dict = None):
        default_config = {
            "method": "btvs",
            "manual_threshold": None,
            "sample_frames": 5,
            "min_area": 50,
            "max_area": 10000,
            "min_circularity": 0.5,
            "max_aspect_ratio": 4.0,
            "min_range": 20,
            "centroid_distance": 12.0,
            "top_fraction": 0.5,
            "fill_closed_contours": True,
        }
        self.config = default_config
        if config:
            self.config.update(config)
        self.threshold_value = None
        self.threshold_groups = []
        logger.debug("ThresholdFinder initialized")

    def auto_threshold(self, frames: List[np.ndarray]) -> int:
        """
        Find threshold using automated BTVS.

        Args:
            frames: Background-removed sample frames with bright droplets

        Returns:
            Threshold value (0-255)
        """
        # Check for manual override
        manual = self.config.get("manual_threshold")
        if manual is not None:
            self.threshold_value = manual
            logger.info(f"Using manual threshold: {manual}")
            return manual

        if not frames:
            self.threshold_value = 128
            logger.warning("No frames for BTVS, using 128")
            return self.threshold_value

        method = self.config.get("method", "btvs")
        if method == "otsu":
            return self._otsu_threshold(frames)

        logger.info(f"Finding BTVS threshold from {len(frames)} frames")
        combined = self._combine_frames(frames[: self.config["sample_frames"]])
        groups = self._collect_threshold_groups(combined)
        self.threshold_groups = groups

        eligible = [g for g in groups if g["range"] >= self.config["min_range"]]
        if not eligible:
            logger.warning("No eligible BTVS groups, falling back to Otsu")
            return self._otsu_threshold(frames)

        eligible.sort(key=lambda g: g["range"], reverse=True)
        keep = max(1, int(np.ceil(len(eligible) * self.config["top_fraction"])))
        medians = [g["median"] for g in eligible[:keep]]

        self.threshold_value = int(np.median(medians))
        logger.info(
            f"BTVS threshold: {self.threshold_value} from {len(eligible)} groups"
        )
        return self.threshold_value

    def _otsu_threshold(self, frames: List[np.ndarray]) -> int:
        pixels = []
        for frame in frames[:5]:
            pixels.extend(frame.flatten())
        pixels_array = np.array(pixels, dtype=np.uint8)
        self.threshold_value, _ = cv2.threshold(
            pixels_array, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        self.threshold_value = int(self.threshold_value)
        logger.info(f"Otsu threshold: {self.threshold_value}")
        return self.threshold_value

    def _combine_frames(self, frames: List[np.ndarray]) -> np.ndarray:
        if len(frames) == 1:
            return frames[0].copy()
        return np.maximum.reduce([f.astype(np.uint8) for f in frames])

    def _collect_threshold_groups(self, frame: np.ndarray) -> List[Dict]:
        groups = []
        centroid_distance = float(self.config["centroid_distance"])

        for threshold in range(1, 255):
            binary = self._raw_threshold(frame, threshold=threshold)
            binary = self._fill_closed_contours(binary)
            contours, _ = cv2.findContours(
                binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )

            for contour in contours:
                props = self._contour_properties(contour)
                if not self._is_qualified(props):
                    continue

                cx, cy = props["centroid"]
                match = None
                match_dist = centroid_distance
                for group in groups:
                    gx, gy = group["centroid"]
                    dist = float(np.hypot(cx - gx, cy - gy))
                    if dist <= match_dist:
                        match = group
                        match_dist = dist

                if match is None:
                    groups.append(
                        {
                            "centroid": (cx, cy),
                            "thresholds": [threshold],
                            "areas": [props["area"]],
                        }
                    )
                else:
                    n = len(match["thresholds"])
                    gx, gy = match["centroid"]
                    match["centroid"] = ((gx * n + cx) / (n + 1), (gy * n + cy) / (n + 1))
                    match["thresholds"].append(threshold)
                    match["areas"].append(props["area"])

        summarized = []
        for group in groups:
            thresholds = sorted(set(group["thresholds"]))
            min_t = min(thresholds)
            max_t = max(thresholds)
            summarized.append(
                {
                    "centroid": group["centroid"],
                    "min": min_t,
                    "max": max_t,
                    "range": max_t - min_t + 1,
                    "median": int(np.median(thresholds)),
                    "count": len(thresholds),
                }
            )
        return summarized

    def _contour_properties(self, contour) -> Dict:
        area = float(cv2.contourArea(contour))
        perimeter = float(cv2.arcLength(contour, True))
        circularity = 4 * np.pi * area / (perimeter**2) if perimeter > 0 else 0.0
        _, _, w, h = cv2.boundingRect(contour)
        aspect_ratio = max(w / max(h, 1), h / max(w, 1))
        moments = cv2.moments(contour)
        if moments["m00"]:
            centroid = (moments["m10"] / moments["m00"], moments["m01"] / moments["m00"])
        else:
            centroid = (0.0, 0.0)
        return {
            "area": area,
            "circularity": circularity,
            "centroid": centroid,
            "aspect_ratio": aspect_ratio,
        }

    def _is_qualified(self, props: Dict) -> bool:
        return (
            self.config["min_area"] <= props["area"] <= self.config["max_area"]
            and props["circularity"] >= self.config["min_circularity"]
            and props["aspect_ratio"] <= self.config["max_aspect_ratio"]
        )

    def _raw_threshold(
        self, frame: np.ndarray, invert: bool = False, threshold: int = None
    ) -> np.ndarray:
        value = self.threshold_value if threshold is None else threshold
        if value is None:
            logger.warning("No threshold set, using 128")
            value = 128
            self.threshold_value = value

        thresh_type = cv2.THRESH_BINARY_INV if invert else cv2.THRESH_BINARY
        _, binary = cv2.threshold(frame, int(value), 255, thresh_type)
        return binary

    def _fill_closed_contours(self, binary: np.ndarray) -> np.ndarray:
        if not self.config.get("fill_closed_contours", True):
            return binary

        flood = binary.copy()
        mask = np.zeros((flood.shape[0] + 2, flood.shape[1] + 2), np.uint8)
        cv2.floodFill(flood, mask, (0, 0), 255)
        holes = cv2.bitwise_not(flood)
        return cv2.bitwise_or(binary, holes)

    def apply_threshold(
        self,
        frame: np.ndarray,
        invert: bool = False,
        threshold: int = None,
    ) -> np.ndarray:
        """
        Apply threshold to frame

        Args:
            frame: Input grayscale frame
            invert: Invert binary result

        Returns:
            Binary frame
        """
        binary = self._raw_threshold(frame, invert=invert, threshold=threshold)
        return self._fill_closed_contours(binary)
