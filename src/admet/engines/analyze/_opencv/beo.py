"""
Background Extraction - Object-Based
Based on Chong et al. 2016 ADM paper
Uses iterative object patching and background division
"""

import cv2
import numpy as np
from typing import List
from .logger import get_logger

logger = get_logger("BEO")


class BackgroundExtractor:
    """Object-based background extraction using iterative patching"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.background = None
        self.i_min = 0
        self.i_max = 255
        logger.debug("BackgroundExtractor initialized")

    def compute_background(self, frames: List[np.ndarray]) -> np.ndarray:
        """
        Compute background using object-based iterative patching.

        Phase 1: Average first half of frames to get initial background (A1)
        Phase 2: Iteratively patch non-background regions using remaining frames

        Args:
            frames: List of grayscale frames (ideally 80+, minimum 10)

        Returns:
            Background image
        """
        if not frames:
            logger.error("No frames provided")
            return None

        n_frames = len(frames)
        logger.info(f"Computing background from {n_frames} frames (object-based)")

        # Split frames: first half for averaging, second half for patching
        n_avg = max(n_frames // 2, 5)
        n_patch = n_frames - n_avg

        avg_frames = frames[:n_avg]
        patch_frames = frames[n_avg:]

        # Phase 1: Compute average image A1
        logger.debug(f"Phase 1: Averaging {n_avg} frames")
        frames_float = np.array([f.astype(np.float32) for f in avg_frames])

        # Track min/max for intensity normalization
        self.i_min = int(np.min(frames_float))
        self.i_max = int(np.max(frames_float))

        A1 = np.mean(frames_float, axis=0).astype(np.uint8)

        # If we don't have enough frames for patching, just return average
        if n_patch < 2:
            logger.warning("Not enough frames for patching, using simple average")
            self.background = cv2.GaussianBlur(A1, (5, 5), 1)
            return self.background

        # Phase 2: Iteratively patch using operator h
        logger.debug(f"Phase 2: Patching with {n_patch} frames")
        R = A1.copy()

        for i, F in enumerate(patch_frames):
            R = self._operator_h(R, F, A1)
            if (i + 1) % 10 == 0:
                logger.debug(f"Patching iteration {i + 1}/{n_patch}")

        # Light blur to smooth final result
        self.background = cv2.GaussianBlur(R, (5, 5), 1)

        logger.info("Background extraction complete (object-based)")
        return self.background

    def _operator_h(self, R: np.ndarray, F: np.ndarray, A: np.ndarray) -> np.ndarray:
        """
        Operator h: Patches background regions from frame F onto result R.

        1. Perform preliminary background removal (PBR) on F using A
        2. Create binary mask marking droplet regions
        3. Patch: take background from F where no droplets, keep R elsewhere

        Args:
            R: Current result image
            F: New frame to extract background from
            A: Average image (used for PBR)

        Returns:
            Updated result with patched background
        """
        # Step 1: Preliminary background removal using division (method f)
        D = self._pbr_division(F, A)

        # Step 2: Create binary mask (procedure g)
        # Dark regions in D are droplets
        B1 = self._create_droplet_mask(D)

        # Step 3: Patch
        # B1 = 0 where droplets are, 255 where background is
        # M1 = background regions from F
        # M2 = droplet regions from R (keep existing)
        B1_norm = B1.astype(np.float32) / 255.0
        B2_norm = 1.0 - B1_norm

        M1 = (F.astype(np.float32) * B1_norm).astype(np.uint8)
        M2 = (R.astype(np.float32) * B2_norm).astype(np.uint8)

        result = cv2.add(M1, M2)
        return result

    def _pbr_division(self, F: np.ndarray, A: np.ndarray) -> np.ndarray:
        """
        Preliminary Background Removal using division method.
        Based on Equation 1 from the paper.

        Division is more robust to illumination changes than subtraction.

        Args:
            F: Frame to process
            A: Average/background image

        Returns:
            Background-removed image (droplets appear dark)
        """
        # Normalize intensity range (Equation 1b)
        # l2(I) = 245 * (I - I_min) / (I_max - I_min) + 10
        # Min value is 10 to avoid division issues

        i_range = max(self.i_max - self.i_min, 1)

        F_float = F.astype(np.float32)
        A_float = A.astype(np.float32)

        l2_F = 245.0 * (F_float - self.i_min) / i_range + 10.0
        l2_A = 245.0 * (A_float - self.i_min) / i_range + 10.0

        # Apply division formula (Equation 1a)
        # D = 255 * l2(F) / l2(A) when F <= A (droplet darker than background)
        # D = 255 when F > A (no droplet, or brighter)

        # Avoid division by zero
        l2_A = np.maximum(l2_A, 1.0)

        D = np.where(
            l2_F > l2_A,
            255.0,  # F brighter than A: background
            255.0 * l2_F / l2_A,  # F darker: droplet region
        )

        D = np.clip(D, 0, 255).astype(np.uint8)
        return D

    def _create_droplet_mask(self, D: np.ndarray) -> np.ndarray:
        """
        Create binary mask marking droplet regions (procedure g from paper).

        Args:
            D: Background-removed image (droplets are dark)

        Returns:
            Binary mask: 255 = background, 0 = droplet
        """
        # Threshold to find dark regions (droplets)
        # Use a fixed threshold since D is normalized
        _, binary = cv2.threshold(D, 200, 255, cv2.THRESH_BINARY)

        # Fill holes in droplet regions
        binary_inv = cv2.bitwise_not(binary)
        contours, _ = cv2.findContours(
            binary_inv, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        expanded = np.zeros_like(binary_inv)
        min_expansion = int(self.config.get("mask_expansion", 3))
        expansion_scale = float(self.config.get("mask_expansion_scale", 0.15))
        for contour in contours:
            droplet = np.zeros_like(binary_inv)
            cv2.drawContours(droplet, [contour], -1, 255, -1)
            _, _, w, _ = cv2.boundingRect(contour)
            kernel_size = max(min_expansion, int(round(w * expansion_scale)))
            if kernel_size % 2 == 0:
                kernel_size += 1
            kernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)
            )
            expanded = cv2.bitwise_or(expanded, cv2.dilate(droplet, kernel))

        # Invert back: 255 = background, 0 = droplet
        mask = cv2.bitwise_not(expanded)
        return mask

    def subtract_background(self, frame: np.ndarray, invert: bool = True) -> np.ndarray:
        """
        Remove background from frame using division method.

        Division is more robust to illumination changes than subtraction.

        Args:
            frame: Input frame
            invert: Invert result (droplets become bright)

        Returns:
            Background-removed frame
        """
        if self.background is None:
            logger.warning("No background set, returning original")
            return frame

        # Resize background if needed
        if frame.shape != self.background.shape:
            bg = cv2.resize(self.background, (frame.shape[1], frame.shape[0]))
        else:
            bg = self.background

        # Use division method for background removal
        result = self._pbr_division(frame, bg)

        if invert:
            result = 255 - result

        return result
