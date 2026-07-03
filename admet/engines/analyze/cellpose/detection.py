from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import numpy as np

from . import scanprotocol
from .cache import Cache
from .config import load_config


class CellposeUnavailableError(ImportError):
    """Raised when Cellpose is required but not installed."""


class CellposeDetection:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        store_visualizations: bool = False,
        use_cache: bool = True,
        detect_inclusions: bool = True,
        cache_dir: str | Path | None = None,
        model_factory: Callable[[], Any] | None = None,
    ):
        self.config = config if config else load_config()
        self.results_data: list[dict[str, Any]] = []
        self.store_visualizations = store_visualizations
        self.visualization_data = {} if store_visualizations else None
        self.use_cache = use_cache
        self.cache = Cache(self.config, cache_dir=cache_dir) if use_cache else None
        self.detect_inclusions = detect_inclusions
        self._cellpose_model = None
        self._model_factory = model_factory
        self._frame_output: Path | None = None

    def parse_filename(self, filename: str) -> tuple[int, int | None]:
        z_match = re.search(r"_z(\d+)_", filename)
        z_index = int(z_match.group(1)) if z_match else 0

        f_match = re.search(r"a01f(\d+)d4", filename, re.IGNORECASE)
        frame_index = int(f_match.group(1)) if f_match else None

        return z_index, frame_index

    def load_and_group_images(self, input_dir: str | Path) -> dict[int, list[tuple[int, Path]]]:
        input_path = Path(input_dir)
        image_files = []
        for extension in (".tif", ".tiff", ".png", ".jpg", ".jpeg"):
            image_files.extend(input_path.glob(f"*{extension}"))
            image_files.extend(input_path.glob(f"*{extension.upper()}"))

        frame_groups: dict[int, list[tuple[int, Path]]] = defaultdict(list)
        for filepath in image_files:
            z_index, frame_index = self.parse_filename(filepath.name)
            if frame_index is not None:
                frame_groups[frame_index].append((z_index, filepath))

        for frame_index in frame_groups:
            frame_groups[frame_index].sort(key=lambda item: item[0])
        return dict(frame_groups)

    def create_min_projection(self, z_stack_files: Iterable[tuple[int, Path]]):
        cv2 = _require_cv2()
        images = []
        for _, filepath in z_stack_files:
            image = cv2.imread(str(filepath), cv2.IMREAD_ANYDEPTH | cv2.IMREAD_GRAYSCALE)
            if image is None:
                continue
            if image.dtype == np.uint16:
                image = image.astype(np.float32) * 64
                image = np.clip(image, 0, 65535)
                image = (image / 256).astype(np.uint8)
            else:
                image = np.clip(image, 0, 255).astype(np.uint8)
            images.append(image)

        if not images:
            return None

        stack = np.stack(images, axis=0)
        min_projection = np.min(stack, axis=0).astype(np.uint8)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        return clahe.apply(min_projection)

    def detect_droplets_cellpose(self, image) -> list[str]:
        if self._cellpose_model is None:
            self._cellpose_model = self._load_cellpose_model()

        masks, _, _ = self._cellpose_model.eval(
            image,
            normalize=True,
            flow_threshold=self.config["cellpose_flow_threshold"],
            cellprob_threshold=self.config["cellpose_cellprob_threshold"],
        )
        return self.masks_to_coordinates(masks)

    def masks_to_coordinates(self, masks) -> list[str]:
        coordinate_list = []
        for mask_id in np.unique(masks)[1:]:
            binary_mask = (masks == mask_id).astype(np.uint8)
            coords = self.mask_to_coordinates(binary_mask)
            if coords is not None:
                coordinate_list.append(coords)
        return coordinate_list

    def mask_to_coordinates(self, binary_mask) -> str | None:
        cv2 = _require_cv2()
        contours, _ = cv2.findContours(binary_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None

        contour = max(contours, key=cv2.contourArea)
        coords = []
        for point in contour:
            coords.extend([str(point[0][0]), str(point[0][1])])
        return ",".join(coords)

    def coordinates_to_mask(self, coord_string: str, image_shape: tuple[int, int]):
        cv2 = _require_cv2()
        coords = [float(value) for value in coord_string.split(",")]
        points = np.array(coords).reshape(-1, 2).astype(np.int32)
        mask = np.zeros(image_shape, dtype=np.uint8)
        cv2.fillPoly(mask, [points], 255)
        return mask

    def erode_mask(self, mask, erosion_pixels: int):
        if erosion_pixels <= 0:
            return mask
        cv2 = _require_cv2()
        kernel_size = 2 * erosion_pixels + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
        return cv2.erode(mask, kernel, iterations=1)

    def detect_inclusions_in_droplet(self, image, droplet_mask, *, store_masked: bool = False):
        cv2 = _require_cv2()
        masked_image = cv2.bitwise_and(image, image, mask=droplet_mask)

        kernel_size = self.config["kernel_size"]
        if kernel_size % 2 == 0:
            kernel_size += 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
        blackhat = cv2.morphologyEx(masked_image, cv2.MORPH_BLACKHAT, kernel)
        _, inclusions = cv2.threshold(
            blackhat,
            self.config["tophat_threshold"],
            255,
            cv2.THRESH_BINARY,
        )

        inclusions = cv2.bitwise_and(inclusions, inclusions, mask=droplet_mask)
        filtered_inclusions, count = self.filter_inclusions_by_size(inclusions)
        if store_masked:
            return filtered_inclusions, count, blackhat
        return filtered_inclusions, count

    def filter_inclusions_by_size(self, inclusion_mask):
        cv2 = _require_cv2()
        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
            inclusion_mask,
            connectivity=8,
        )

        height, width = inclusion_mask.shape
        edge_buffer = self.config.get("edge_buffer", 5)
        filtered_mask = np.zeros_like(inclusion_mask)
        inclusion_count = 0

        for label in range(1, num_labels):
            area = stats[label, cv2.CC_STAT_AREA]
            x = stats[label, cv2.CC_STAT_LEFT]
            y = stats[label, cv2.CC_STAT_TOP]
            w_comp = stats[label, cv2.CC_STAT_WIDTH]
            h_comp = stats[label, cv2.CC_STAT_HEIGHT]

            if (
                x < edge_buffer
                or y < edge_buffer
                or x + w_comp > width - edge_buffer
                or y + h_comp > height - edge_buffer
            ):
                continue

            if self.config["min_inclusion_area"] <= area <= self.config["max_inclusion_area"]:
                filtered_mask[labels == label] = 255
                inclusion_count += 1

        return filtered_mask, inclusion_count

    def process_frame(self, frame_idx: int, min_projection, droplet_coords: list[str] | None = None) -> None:
        cv2 = _require_cv2()
        store_viz = self.store_visualizations
        save_overlay = self._frame_output is not None
        overlay = cv2.cvtColor(min_projection, cv2.COLOR_GRAY2BGR) if save_overlay else None

        if store_viz:
            frame_viz = {
                "min_projection": min_projection,
                "droplet_masks": [],
                "eroded_masks": [],
                "inclusion_masks": [],
                "masked_images": [],
            }

        if droplet_coords is None:
            droplet_coords = self.detect_droplets_cellpose(min_projection)

        if not droplet_coords:
            if store_viz:
                self.visualization_data[frame_idx] = frame_viz
            if save_overlay:
                self._save_overlay(frame_idx, overlay)
            return

        valid_droplet_idx = 0
        for coords in droplet_coords:
            droplet_mask = self.coordinates_to_mask(coords, min_projection.shape)
            contours, _ = cv2.findContours(
                droplet_mask,
                cv2.RETR_EXTERNAL,
                cv2.CHAIN_APPROX_SIMPLE,
            )
            if not contours:
                continue

            moments = cv2.moments(contours[0])
            if moments["m00"] == 0:
                continue

            cx = int(moments["m10"] / moments["m00"])
            cy = int(moments["m01"] / moments["m00"])
            area = cv2.contourArea(contours[0])
            diameter = np.sqrt(4 * area / np.pi)
            if not (
                self.config["min_droplet_diameter"]
                <= diameter
                <= self.config["max_droplet_diameter"]
            ):
                continue

            eroded_mask = self.erode_mask(droplet_mask, self.config["erosion_pixels"])
            if np.sum(eroded_mask) == 0:
                continue

            inclusion_count = 0
            inclusion_mask = np.zeros_like(droplet_mask)
            if self.detect_inclusions:
                if store_viz:
                    inclusion_mask, inclusion_count, blackhat = self.detect_inclusions_in_droplet(
                        min_projection,
                        eroded_mask,
                        store_masked=True,
                    )
                    frame_viz["masked_images"].append(blackhat)
                else:
                    inclusion_mask, inclusion_count = self.detect_inclusions_in_droplet(
                        min_projection,
                        eroded_mask,
                    )

            if save_overlay:
                cv2.drawContours(overlay, contours, -1, (0, 255, 0), 1)
                if self.detect_inclusions:
                    overlay[inclusion_mask > 0] = (0, 0, 255)
                    cv2.putText(
                        overlay,
                        str(inclusion_count),
                        (cx - 5, cy + 5),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.4,
                        (0, 255, 255),
                        1,
                    )

            if store_viz:
                frame_viz["droplet_masks"].append(
                    {
                        "mask": droplet_mask,
                        "center": (cx, cy),
                        "radius": diameter / 2,
                        "inclusions": inclusion_count,
                    }
                )
                frame_viz["eroded_masks"].append(eroded_mask)
                frame_viz["inclusion_masks"].append(inclusion_mask)

            self.results_data.append(
                {
                    "frame": frame_idx,
                    "droplet_id": valid_droplet_idx,
                    "center_x": cx,
                    "center_y": cy,
                    "diameter_px": diameter,
                    "diameter_um": diameter * self.config["px_to_um"],
                    "area_px": area,
                    "area_um2": area * (self.config["px_to_um"] ** 2),
                    "inclusions": inclusion_count,
                }
            )
            valid_droplet_idx += 1

        if store_viz:
            self.visualization_data[frame_idx] = frame_viz
        if save_overlay:
            self._save_overlay(frame_idx, overlay)

    def run(
        self,
        input_dir: str | Path,
        *,
        frame_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        frame_groups = self.load_and_group_images(input_dir)
        if not frame_groups:
            return []

        frame_indices = sorted(frame_groups)
        if frame_limit and frame_limit > 0:
            frame_indices = frame_indices[:frame_limit]

        cache_hits = 0
        for frame_idx in frame_indices:
            z_stack_files = frame_groups[frame_idx]
            cache_key_file = z_stack_files[0][1].name if z_stack_files else None
            if self.cache and cache_key_file and self.cache.is_valid(cache_key_file):
                cached_data = self.cache.load_frame(cache_key_file)
                min_projection = cached_data["min_projection"]
                droplet_coords = cached_data["droplet_coords"]
                cache_hits += 1
                self.process_frame(frame_idx, min_projection, droplet_coords)
                continue

            min_projection = self.create_min_projection(z_stack_files)
            if min_projection is None:
                continue

            droplet_coords = self.detect_droplets_cellpose(min_projection)
            if self.cache and cache_key_file:
                self.cache.save_frame(cache_key_file, min_projection, droplet_coords)
            self.process_frame(frame_idx, min_projection, droplet_coords)

        self.cache_hits = cache_hits
        return self.results_data

    def _load_cellpose_model(self):
        if self._model_factory is not None:
            return self._model_factory()
        try:
            from cellpose.models import CellposeModel
        except ImportError as exc:
            raise CellposeUnavailableError(
                "Cellpose is required for droplet detection. Install the analyze extra."
            ) from exc
        return CellposeModel(gpu=True)

    def _save_overlay(self, frame_idx: int, overlay) -> None:
        if self._frame_output is None:
            return
        cv2 = _require_cv2()
        cv2.imwrite(str(self._frame_output / f"frame_{frame_idx}.png"), overlay)

    def _write_layout(self, input_dir: str | Path, output_path: Path, frame_indices: list[int]) -> None:
        layout = scanprotocol.build_layout(input_dir, len(frame_indices))
        if layout is None:
            return
        layout["frames"] = list(frame_indices)
        with (output_path / "layout.json").open("w", encoding="utf-8") as handle:
            json.dump(layout, handle)

    def _write_results_csv(self, csv_path: Path) -> None:
        fieldnames = [
            "frame",
            "droplet_id",
            "center_x",
            "center_y",
            "diameter_px",
            "diameter_um",
            "area_px",
            "area_um2",
            "inclusions",
        ]
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self.results_data)


def _require_cv2():
    try:
        import cv2
    except ImportError as exc:
        raise ImportError("OpenCV is required for Cellpose image processing.") from exc
    return cv2
