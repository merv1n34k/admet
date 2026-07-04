"""
Droplet Detection Module - Simplified
Handles contour detection and filtering
"""

import cv2
import numpy as np
from typing import List, Tuple, Dict
from .logger import get_logger

logger = get_logger("Detector")


class DropletDetector:
    """Detects droplets using contour analysis"""

    def __init__(self, config: dict = None):
        default_config = {
            "closing_t": 2,
            "closing_e": 1,
            "min_area": 50,
            "max_area": 10000,
            "min_circularity": 0.3,
            "max_aspect_ratio": 4.0,
            "no_edge": False,
        }

        self.config = default_config
        if config:
            self.config.update(config)

        logger.debug("DropletDetector initialized")

    def detect(self, binary: np.ndarray) -> Tuple[List, List[Dict]]:
        """
        Detect droplets in binary image

        Args:
            binary: Binary image

        Returns:
            Tuple of (contours, properties)
        """
        contours = self.find_raw_contours(binary)

        logger.debug(f"Found {len(contours)} raw contours")

        # Filter
        filtered = self._filter_contours(contours, binary.shape)

        logger.debug(f"Filtered to {len(filtered)} droplets")

        # Calculate properties
        properties = [self._get_properties(c) for c in filtered]

        return filtered, properties

    def find_raw_contours(self, binary: np.ndarray) -> List:
        processed = self._apply_morphology(binary)
        contours, _ = cv2.findContours(
            processed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        return contours

    def _apply_morphology(self, binary: np.ndarray) -> np.ndarray:
        """Apply morphological operations"""
        result = binary.copy()

        # Closing
        if self.config["closing_t"] > 0:
            k_size = 2 * self.config["closing_t"] + 1
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_size, k_size))
            result = cv2.morphologyEx(result, cv2.MORPH_CLOSE, kernel)

        # Erosion
        if self.config["closing_e"] > 0:
            k_size = 2 * self.config["closing_e"] + 1
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_size, k_size))
            result = cv2.erode(result, kernel)

        return result

    def _filter_contours(self, contours: List, img_shape: Tuple) -> List:
        """Filter contours by size and shape"""
        h, w = img_shape[:2]
        filtered = []

        for contour in contours:
            area = cv2.contourArea(contour)

            # Area filter
            if area < self.config["min_area"] or area > self.config["max_area"]:
                continue

            # Circularity filter
            perimeter = cv2.arcLength(contour, True)
            if perimeter > 0:
                circularity = 4 * np.pi * area / (perimeter**2)
                if circularity < self.config["min_circularity"]:
                    continue

            x, y, bw, bh = cv2.boundingRect(contour)
            aspect_ratio = max(bw / max(bh, 1), bh / max(bw, 1))
            if aspect_ratio > self.config["max_aspect_ratio"]:
                continue

            # Edge filter
            if self.config["no_edge"]:
                if x <= 0 or y <= 0 or x + bw >= w or y + bh >= h:
                    continue

            filtered.append(contour)

        return filtered

    def _get_properties(self, contour) -> Dict:
        """Calculate basic properties for a contour"""
        area = cv2.contourArea(contour)
        perimeter = cv2.arcLength(contour, True)

        # Centroid
        M = cv2.moments(contour)
        if M["m00"] != 0:
            cx = M["m10"] / M["m00"]
            cy = M["m01"] / M["m00"]
        else:
            cx, cy = 0, 0

        # Bounding box
        x, y, w, h = cv2.boundingRect(contour)

        # Circularity
        circularity = 4 * np.pi * area / (perimeter**2) if perimeter > 0 else 0

        # Equivalent diameter
        diameter = np.sqrt(4 * area / np.pi)

        return {
            "area": area,
            "perimeter": perimeter,
            "centroid_x": cx,
            "centroid_y": cy,
            "bbox_x": x,
            "bbox_y": y,
            "bbox_width": w,
            "bbox_height": h,
            "circularity": circularity,
            "equivalent_diameter": diameter,
        }
