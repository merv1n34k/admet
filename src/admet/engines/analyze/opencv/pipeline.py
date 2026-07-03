"""
Main processing pipeline for ADM-style droplet measurement.
"""

import time
from pathlib import Path

import numpy as np
from typing import Callable
from .logger import get_logger
from .io import VideoReader
from .beo import BackgroundExtractor
from .btvf import ThresholdFinder
from .detect import DropletDetector

logger = get_logger("Pipeline")


class DropletPipeline:
    """Simplified pipeline: background -> threshold -> detect"""

    def __init__(self, config: dict = None):
        self.config = config or {}

        # Core modules only
        self.video_reader = None
        self.background_extractor = BackgroundExtractor(
            self.config.get("background", {})
        )
        threshold_config = self.config.get("threshold", {}).copy()
        threshold_config.update(
            {
                "min_area": self.config.get("segmentation", {}).get("min_area", 50),
                "max_area": self.config.get("segmentation", {}).get("max_area", 10000),
                "min_circularity": self.config.get("segmentation", {}).get(
                    "min_circularity", 0.5
                ),
                "max_aspect_ratio": self.config.get("segmentation", {}).get(
                    "max_aspect_ratio", 4.0
                ),
            }
        )
        self.threshold_finder = ThresholdFinder(threshold_config)
        self.detector = DropletDetector(self.config.get("segmentation", {}))

        # State
        self.background = None
        self.threshold_value = None
        self.frame_count = 0
        self.progress_callback = None
        self.frame_callback = None

        # Store frame data for GUI
        self.stored_frame_data = {}
        self.all_detections = []
        self.active_tracks = {}
        self.finished_tracks = []
        self.corrected_tracks = []
        self.trajectory_meta = {}
        self.next_track_id = 1
        self.global_velocity = None
        self.cache_dir = None
        self.contour_cache_buffer = []
        self.contour_chunk_index = 0
        self.contour_cache_chunk_size = 5000

        logger.debug("Pipeline initialized")

    def set_video(self, video_path: str):
        """Set video source"""
        video_config = self.config.get("video", {})
        self.video_reader = VideoReader(
            source=video_path,
            scale_factor=video_config.get("scale_factor", 1.0),
            roi=video_config.get("roi"),
        )

    def set_progress_callback(self, callback: Callable[[int, str], None]):
        """Set progress callback"""
        self.progress_callback = callback

    def set_frame_callback(self, callback: Callable[[dict], None]):
        """Set frame callback for GUI"""
        self.frame_callback = callback

    def run(self) -> dict:
        """Run the pipeline"""
        logger.info("Starting pipeline")
        start_time = time.time()

        # Open video
        if not self.video_reader:
            return {"error": "No video source"}

        if not self.video_reader.open():
            return {"error": "Failed to open video"}

        props = self.video_reader.get_properties()
        total_frames = props["total_frames"]
        start_frame, end_frame = self._frame_range(total_frames)

        # Step 1: Background
        self._report_progress(0, "Extracting background...")
        if not self._compute_background():
            return {"error": "Background extraction failed"}

        # Step 2: Threshold
        self._report_progress(5, "Finding threshold...")
        if not self._determine_threshold():
            return {"error": "Threshold determination failed"}

        # Step 3: Process frames
        self._report_progress(10, "Processing frames...")
        self._prepare_cache_dir()
        self._process_all_frames(start_frame, end_frame)

        # Done
        self.video_reader.release()

        elapsed = time.time() - start_time
        self._report_progress(100, "Complete")

        logger.info(f"Pipeline completed in {elapsed:.2f}s")

        return self._get_results(elapsed)

    def _frame_range(self, total_frames: int) -> tuple:
        video_config = self.config.get("video", {})
        start_frame = max(int(video_config.get("start_frame") or 0), 0)
        if total_frames > 0:
            start_frame = min(start_frame, total_frames)

        configured_end = video_config.get("end_frame")
        if configured_end is None:
            end_frame = total_frames
        else:
            end_frame = max(int(configured_end), start_frame)
            if total_frames > 0:
                end_frame = min(end_frame, total_frames)

        return start_frame, end_frame

    def _report_progress(self, percent: int, message: str):
        if self.progress_callback:
            self.progress_callback(percent, message)
        logger.info(f"{percent}% - {message}")

    def _compute_background(self) -> bool:
        """Compute background"""
        try:
            n_frames = self.config.get("background", {}).get("sample_frames", 80)
            start_frame, end_frame = self._frame_range(self.video_reader.total_frames)
            frames = self.video_reader.read_sampled_frames(
                n_frames, seed=11, start_frame=start_frame, end_frame=end_frame
            )

            if len(frames) < 2:
                logger.error("Not enough frames")
                return False

            self.background = self.background_extractor.compute_background(frames)
            self.video_reader.seek_frame(0)
            return True

        except Exception as e:
            logger.error(f"Background failed: {e}")
            return False

    def _determine_threshold(self) -> bool:
        """Determine threshold"""
        try:
            n_frames = self.config.get("threshold", {}).get("sample_frames", 5)
            start_frame, end_frame = self._frame_range(self.video_reader.total_frames)
            frames = self.video_reader.read_sampled_frames(
                n_frames, seed=23, start_frame=start_frame, end_frame=end_frame
            )

            # Apply background subtraction
            if self.background is not None:
                frames = [
                    self.background_extractor.subtract_background(f, invert=True)
                    for f in frames
                ]

            self.threshold_value = self.threshold_finder.auto_threshold(frames)
            self.video_reader.seek_frame(0)
            return True

        except Exception as e:
            logger.error(f"Threshold failed: {e}")
            return False

    def _process_all_frames(self, start_frame: int, end_frame: int):
        """Process all frames"""
        total_frames = max(end_frame - start_frame, 0)
        processing_config = self.config.get("processing", {})
        store_preview_frames = processing_config.get("store_preview_frames", True)
        max_preview_frames = max(
            1, int(processing_config.get("max_preview_frames", 500))
        )
        preview_stride = max(1, int(np.ceil(total_frames / max_preview_frames)))
        next_progress = 15
        processing_start = time.time()
        processed_frames = 0

        self.video_reader.seek_frame(start_frame)
        for frame_idx, frame in self.video_reader.get_frame_generator():
            absolute_frame_idx = start_frame + frame_idx
            if absolute_frame_idx >= end_frame:
                break

            # Process frame
            result = self._process_frame(frame, absolute_frame_idx)
            self._update_tracks(result, absolute_frame_idx)
            self._cache_detection_assets(result, absolute_frame_idx)

            if store_preview_frames and (
                processed_frames % preview_stride == 0
                or processed_frames == total_frames - 1
            ):
                self.stored_frame_data[absolute_frame_idx] = result

            if self.frame_callback and absolute_frame_idx in self.stored_frame_data:
                self.frame_callback(result)

            # Store detection counts
            self.all_detections.append(
                {
                    "frame": absolute_frame_idx,
                    "count": len(result["contours"]),
                    "properties": result["properties"],
                }
            )

            if total_frames > 0:
                processed_count = processed_frames + 1
                percent_done = processed_count / total_frames
                progress = 10 + int(85 * percent_done)
                while progress >= next_progress and next_progress <= 95:
                    eta = self._format_eta(
                        processing_start, processed_count, total_frames
                    )
                    self._report_progress(
                        next_progress,
                        f"Frame {absolute_frame_idx + 1}/{end_frame}, ETA {eta}",
                    )
                    next_progress += 5

            processed_frames += 1
            self.frame_count = processed_frames

        self._flush_contour_cache()
        self._finalize_tracks(force=True)
        self._apply_trajectory_counter()

    def _format_eta(
        self, start_time: float, processed_frames: int, total_frames: int
    ) -> str:
        elapsed = max(time.time() - start_time, 0.001)
        rate = processed_frames / elapsed if processed_frames > 0 else 0
        if rate <= 0:
            return "calculating"

        remaining_seconds = max((total_frames - processed_frames) / rate, 0)
        minutes, seconds = divmod(int(round(remaining_seconds)), 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:d}:{seconds:02d}"

    def _process_frame(self, frame: np.ndarray, frame_idx: int) -> dict:
        """Process single frame"""
        result = {
            "frame_number": frame_idx,
            "original": frame.copy(),
            "background": self.background.copy()
            if self.background is not None
            else None,
            "processed": None,
            "binary": None,
            "contours": [],
            "raw_contours": [],
            "properties": [],
        }

        # Background subtraction
        if self.background is not None:
            processed = self.background_extractor.subtract_background(
                frame, invert=True
            )
            no_background = self.background_extractor.subtract_background(
                frame, invert=False
            )
        else:
            processed = frame
            no_background = frame

        result["processed"] = processed.copy()
        result["no_background"] = no_background.copy()

        # Threshold: processed images use bright droplets on dark background.
        binary = self.threshold_finder.apply_threshold(processed)
        result["binary"] = binary.copy()

        # Detect
        raw_contours = self.detector.find_raw_contours(binary)
        contours, properties = self.detector.detect(binary)
        for contour, props in zip(contours, properties):
            props["contour_points"] = contour.reshape(-1, 2).astype(np.int16)
        result["contours"] = contours
        result["raw_contours"] = raw_contours
        result["properties"] = properties

        return result

    def _prepare_cache_dir(self):
        processing_config = self.config.get("processing", {})
        cache_dir = processing_config.get("cache_dir")
        if not cache_dir:
            self.cache_dir = None
            self.contour_cache_buffer = []
            self.contour_chunk_index = 0
            return
        self.cache_dir = Path(cache_dir)

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "contours").mkdir(exist_ok=True)
        self.contour_cache_buffer = []
        self.contour_chunk_index = 0
        self.contour_cache_chunk_size = max(
            1, int(processing_config.get("contour_cache_chunk_size", 5000))
        )

    def _cache_detection_assets(self, result: dict, frame_idx: int):
        if self.cache_dir is None:
            return

        for det_idx, props in enumerate(result.get("properties", [])):
            contour = props.pop("contour_points", None)
            if contour is not None:
                self.contour_cache_buffer.append((props, contour))
                if len(self.contour_cache_buffer) >= self.contour_cache_chunk_size:
                    self._flush_contour_cache()

    def _flush_contour_cache(self):
        if not self.contour_cache_buffer or self.cache_dir is None:
            return

        contour_dir = self.cache_dir / "contours"
        chunk_path = contour_dir / f"chunk_{self.contour_chunk_index:05d}.npz"
        points = []
        offsets = []
        lengths = []
        offset = 0
        for _, contour in self.contour_cache_buffer:
            contour = np.asarray(contour, dtype=np.int16).reshape(-1, 2)
            points.append(contour)
            offsets.append(offset)
            lengths.append(len(contour))
            offset += len(contour)

        packed_points = (
            np.vstack(points).astype(np.int16)
            if points
            else np.empty((0, 2), dtype=np.int16)
        )
        np.savez_compressed(
            chunk_path,
            points=packed_points,
            offsets=np.asarray(offsets, dtype=np.int64),
            lengths=np.asarray(lengths, dtype=np.int32),
        )

        for idx, (props, _) in enumerate(self.contour_cache_buffer):
            props["contour_path"] = str(chunk_path)
            props["contour_offset"] = int(offsets[idx])
            props["contour_length"] = int(lengths[idx])

        self.contour_cache_buffer = []
        self.contour_chunk_index += 1

    def _update_tracks(self, result: dict, frame_idx: int):
        tracking_config = self.config.get("tracking", {})
        max_distance = tracking_config.get("max_distance", 30)
        max_miss = tracking_config.get("max_miss", 2)

        detections = list(enumerate(result["properties"]))
        unmatched_tracks = set(self.active_tracks)
        unmatched_detections = {det_idx for det_idx, _ in detections}

        # Match detections against each track's predicted next position (last
        # centroid + velocity), not its last position. This stops a track from
        # absorbing freshly generated droplets near the source while its real
        # droplet has already moved downstream.
        flow_dir = self._flow_unit_vector()
        candidates = []
        for det_idx, props in detections:
            cx = props["centroid_x"]
            cy = props["centroid_y"]
            for track_id in unmatched_tracks:
                track = self.active_tracks[track_id]
                px, py = self._predict_track_position(track)
                residual = float(np.hypot(cx - px, cy - py))
                if residual > max_distance:
                    continue
                if flow_dir is not None:
                    lx, ly = track["last_centroid"]
                    along = (cx - lx) * flow_dir[0] + (cy - ly) * flow_dir[1]
                    if along < -max_distance:
                        continue
                candidates.append((residual, track_id, det_idx))

        for _, track_id, det_idx in sorted(candidates):
            if track_id not in unmatched_tracks or det_idx not in unmatched_detections:
                continue
            props = result["properties"][det_idx]
            self._append_detection_to_track(track_id, props, frame_idx)
            unmatched_tracks.discard(track_id)
            unmatched_detections.discard(det_idx)

        for det_idx in self._entry_ordered_detection_indices(
            result["properties"], unmatched_detections
        ):
            props = result["properties"][det_idx]
            track_id = self.next_track_id
            self.next_track_id += 1
            self.active_tracks[track_id] = {
                "id": track_id,
                "frames": [],
                "properties": [],
                "last_centroid": (props["centroid_x"], props["centroid_y"]),
                "velocity": self.global_velocity or (0.0, 0.0),
                "missed": 0,
            }
            self._append_detection_to_track(track_id, props, frame_idx)

        for track_id in list(unmatched_tracks):
            track = self.active_tracks[track_id]
            track["missed"] += 1
            if track["missed"] > max_miss:
                self.finished_tracks.append(self.active_tracks.pop(track_id))

    def _predict_track_position(self, track: dict) -> tuple:
        lx, ly = track["last_centroid"]
        vx, vy = track.get("velocity", (0.0, 0.0))
        steps = track.get("missed", 0) + 1
        return lx + vx * steps, ly + vy * steps

    def _flow_unit_vector(self):
        if self.global_velocity is None:
            return None
        vx, vy = self.global_velocity
        mag = float(np.hypot(vx, vy))
        if mag < 1e-6:
            return None
        return vx / mag, vy / mag

    def _update_global_velocity(self, inst_v: tuple, alpha: float = 0.2):
        if self.global_velocity is None:
            self.global_velocity = inst_v
        else:
            gx, gy = self.global_velocity
            self.global_velocity = (gx + alpha * (inst_v[0] - gx),
                                    gy + alpha * (inst_v[1] - gy))

    def _entry_ordered_detection_indices(self, properties: list, indices: set) -> list:
        flow_direction = self.config.get("tracking", {}).get(
            "flow_direction", "left_to_right"
        )
        reverse = flow_direction != "right_to_left"
        return sorted(
            indices,
            key=lambda idx: properties[idx].get("centroid_x", 0),
            reverse=reverse,
        )

    def _append_detection_to_track(self, track_id: int, props: dict, frame_idx: int):
        tracking_config = self.config.get("tracking", {})
        vel_alpha = tracking_config.get("velocity_smoothing", 0.5)
        track = self.active_tracks[track_id]
        new_centroid = (props["centroid_x"], props["centroid_y"])
        if track["frames"]:
            steps = (frame_idx - track["frames"][-1]) or 1
            lx, ly = track["last_centroid"]
            inst_v = ((new_centroid[0] - lx) / steps, (new_centroid[1] - ly) / steps)
            ovx, ovy = track.get("velocity", (0.0, 0.0))
            track["velocity"] = (ovx + vel_alpha * (inst_v[0] - ovx),
                                 ovy + vel_alpha * (inst_v[1] - ovy))
            self._update_global_velocity(inst_v)
        track["frames"].append(frame_idx)
        track["properties"].append(props)
        track["last_centroid"] = new_centroid
        track["missed"] = 0
        props["track_id"] = track_id
        props["droplet_id"] = track_id

    def _finalize_tracks(self, force: bool = False):
        if force:
            self.finished_tracks.extend(self.active_tracks.values())
            self.active_tracks = {}

    def _apply_trajectory_counter(self):
        tracking_config = self.config.get("tracking", {})
        if not tracking_config.get("trajectory_counter", True):
            return

        detections = self._trajectory_detections()
        if len(detections) < 2:
            return

        points = np.asarray(
            [[det["prop"]["centroid_x"], det["prop"]["centroid_y"]] for det in detections],
            dtype=np.float32,
        )
        areas = np.asarray([det["prop"].get("area", 0.0) for det in detections], dtype=np.float32)
        center = np.mean(points, axis=0)
        centered = points - center
        _, _, vh = np.linalg.svd(centered, full_matrices=False)
        axis = vh[0]
        raw_s = centered @ axis
        if float(np.ptp(raw_s)) <= 1e-6:
            return

        low_limit = np.percentile(raw_s, 20)
        high_limit = np.percentile(raw_s, 80)
        low_area = float(np.mean(areas[raw_s <= low_limit])) if np.any(raw_s <= low_limit) else 0.0
        high_area = float(np.mean(areas[raw_s >= high_limit])) if np.any(raw_s >= high_limit) else 0.0
        direction = -1.0 if low_area < high_area else 1.0
        s_values = raw_s * direction
        for det, s_value in zip(detections, s_values):
            det["s"] = float(s_value)
            det["prop"]["trajectory_s"] = float(s_value)

        s_min = float(np.min(s_values))
        s_max = float(np.max(s_values))
        checkpoint_count = int(tracking_config.get("trajectory_checkpoints", 6))
        checkpoint_count = max(1, checkpoint_count)
        checkpoints = np.linspace(s_min, s_max, checkpoint_count + 2)[1:-1]
        spacing = float((s_max - s_min) / (checkpoint_count + 1))

        events = []
        next_event_id = 1
        for _, source_detections in self._group_detections_by_source(detections).items():
            source_detections.sort(key=lambda item: (item["frame"], item["index"]))
            # One physical droplet per track. The predicted-position tracker
            # already separates droplets, so we do NOT re-split a track on
            # checkpoint crossings: under aliasing-induced position jitter that
            # fragmented each droplet into many phantom counts and skewed the
            # size/speed histograms. Checkpoints are only recorded for reporting.
            event = self._new_trajectory_event()
            last_s = None
            for detection in source_detections:
                s_value = detection["s"]
                if last_s is not None:
                    event["checkpoints"].update(
                        self._crossed_checkpoints(last_s, s_value, checkpoints)
                    )
                event["detections"].append(detection)
                last_s = s_value

            next_event_id = self._finish_trajectory_event(
                event, events, next_event_id, spacing
            )

        self.corrected_tracks = events
        self.trajectory_meta = {
            "enabled": True,
            "axis": (axis * direction).astype(float).tolist(),
            "center": center.astype(float).tolist(),
            "direction": "toward_lower_area_end",
            "checkpoints": checkpoints.astype(float).tolist(),
            "low_end_area": low_area,
            "high_end_area": high_area,
            "stuck_events": len([event for event in events if event.get("stuck")]),
        }

    def _trajectory_detections(self) -> list:
        detections = []
        for frame in self.all_detections:
            frame_number = frame.get("frame", 0)
            for index, prop in enumerate(frame.get("properties", [])):
                detections.append(
                    {
                        "frame": frame_number,
                        "index": index,
                        "prop": prop,
                        "source_id": prop.get("track_id", prop.get("droplet_id", index)),
                    }
                )
        return detections

    def _group_detections_by_source(self, detections: list) -> dict:
        grouped = {}
        for detection in detections:
            grouped.setdefault(detection["source_id"], []).append(detection)
        return grouped

    def _new_trajectory_event(self) -> dict:
        return {"detections": [], "checkpoints": set()}

    def _crossed_checkpoints(self, previous_s: float, current_s: float, checkpoints) -> list:
        if current_s < previous_s:
            return []
        return [
            idx
            for idx, checkpoint in enumerate(checkpoints)
            if previous_s < checkpoint <= current_s
        ]

    def _finish_trajectory_event(
        self,
        event: dict,
        events: list,
        next_event_id: int,
        checkpoint_spacing: float,
    ) -> int:
        detections = event.get("detections", [])
        if not detections:
            return next_event_id

        frames = [det["frame"] for det in detections]
        s_values = [det["s"] for det in detections]
        progress = max(s_values) - min(s_values) if s_values else 0.0
        tracking_config = self.config.get("tracking", {})
        stuck_min_frames = int(tracking_config.get("stuck_min_frames", 8))
        stuck = (
            len(set(frames)) >= stuck_min_frames
            and progress < max(checkpoint_spacing, 1.0)
        )

        if stuck:
            for detection in detections:
                prop = detection["prop"]
                prop["stuck"] = True
                prop["droplet_id"] = None
                prop["trajectory_event_id"] = None
            event["stuck"] = True
            event["droplet_id"] = None
        else:
            for detection in detections:
                prop = detection["prop"]
                prop["source_track_id"] = prop.get("track_id")
                prop["track_id"] = next_event_id
                prop["droplet_id"] = next_event_id
                prop["trajectory_event_id"] = next_event_id
                prop["stuck"] = False
            event["stuck"] = False
            event["droplet_id"] = next_event_id
            next_event_id += 1

        event["frames"] = frames
        event["properties"] = [det["prop"] for det in detections]
        event["checkpoint_count"] = len(event.get("checkpoints", set()))
        events.append(event)
        return next_event_id

    def _track_summaries(self) -> list:
        if self.corrected_tracks:
            return self._corrected_track_summaries()

        tracking_config = self.config.get("tracking", {})
        min_count = tracking_config.get("min_count", 3)
        fps = self.config.get("analysis", {}).get("fps") or self.video_reader.fps or 1
        scale = self.config.get("analysis", {}).get("microns_per_pixel", 1.0)

        summaries = []
        for track in self.finished_tracks:
            frames = track["frames"]
            props = track["properties"]
            if len(frames) < min_count:
                continue

            first = props[0]
            last = props[-1]
            dx = last["centroid_x"] - first["centroid_x"]
            dy = last["centroid_y"] - first["centroid_y"]
            duration = (frames[-1] - frames[0]) / fps if frames[-1] > frames[0] else 0
            distance_px = float(np.hypot(dx, dy))
            speed_um_s = distance_px * scale / duration if duration > 0 else 0.0
            areas = [p["area"] for p in props]
            perimeters = [p["perimeter"] for p in props]
            diameters = [p["equivalent_diameter"] for p in props]

            summaries.append(
                {
                    "track_id": track["id"],
                    "droplet_id": track["id"],
                    "first_frame": frames[0],
                    "last_frame": frames[-1],
                    "frame_count": len(frames),
                    "duration_s": duration,
                    "mean_area_px": float(np.mean(areas)),
                    "mean_area_um2": float(np.mean(areas) * scale * scale),
                    "mean_perimeter_um": float(np.mean(perimeters) * scale),
                    "mean_diameter_um": float(np.mean(diameters) * scale),
                    "speed_um_s": speed_um_s,
                    "speed_mm_s": speed_um_s / 1000.0,
                    "orientation_deg": float(np.degrees(np.arctan2(dy, dx))),
                }
            )
        return summaries

    def _corrected_track_summaries(self) -> list:
        tracking_config = self.config.get("tracking", {})
        min_count = tracking_config.get("min_count", 3)
        fps = self.config.get("analysis", {}).get("fps") or self.video_reader.fps or 1
        scale = self.config.get("analysis", {}).get("microns_per_pixel", 1.0)
        summaries = []
        for event in self.corrected_tracks:
            if event.get("stuck") or event.get("droplet_id") is None:
                continue
            if len(event.get("frames", [])) < min_count:
                continue

            frames = event["frames"]
            props = event["properties"]
            first = props[0]
            last = props[-1]
            dx = last["centroid_x"] - first["centroid_x"]
            dy = last["centroid_y"] - first["centroid_y"]
            duration = (frames[-1] - frames[0]) / fps if frames[-1] > frames[0] else 0
            distance_px = float(np.hypot(dx, dy))
            speed_um_s = distance_px * scale / duration if duration > 0 else 0.0
            areas = [p["area"] for p in props]
            perimeters = [p["perimeter"] for p in props]
            diameters = [p["equivalent_diameter"] for p in props]

            summaries.append(
                {
                    "track_id": event["droplet_id"],
                    "droplet_id": event["droplet_id"],
                    "source_track_ids": sorted(
                        {
                            p.get("source_track_id")
                            for p in props
                            if p.get("source_track_id") is not None
                        }
                    ),
                    "first_frame": frames[0],
                    "last_frame": frames[-1],
                    "frame_count": len(frames),
                    "checkpoint_count": event.get("checkpoint_count", 0),
                    "duration_s": duration,
                    "mean_area_px": float(np.mean(areas)),
                    "mean_area_um2": float(np.mean(areas) * scale * scale),
                    "mean_perimeter_um": float(np.mean(perimeters) * scale),
                    "mean_diameter_um": float(np.mean(diameters) * scale),
                    "speed_um_s": speed_um_s,
                    "speed_mm_s": speed_um_s / 1000.0,
                    "orientation_deg": float(np.degrees(np.arctan2(dy, dx))),
                }
            )
        return summaries

    def _confined_volume_nl(self, length_um: float, width_um: float, depth_um: float) -> float:
        """Volume (nl) of a droplet confined in a width x depth channel.

        Cross-section modelled as an ellipse (axes width & depth): a capsule
        (elliptical-cylinder body + caps) when L >= width, an ellipsoid otherwise.
        """
        cross_area = (np.pi / 4.0) * width_um * depth_um
        if length_um >= width_um:
            vol_um3 = cross_area * (length_um - width_um / 3.0)
        else:
            vol_um3 = (np.pi / 6.0) * length_um * width_um * depth_um
        return vol_um3 / 1e6

    def _compute_true_statistics(self, tracks: list) -> tuple:
        """Aliasing-immune volume / true-count estimate from system parameters.

        Calibrates um/px from the known channel width vs the measured cross-stream
        droplet extent, computes a per-droplet confined volume, and derives the
        true generation rate from mass conservation (f = Q_aq / V). Returns
        (aggregate_stats, per_droplet_geometry); both empty when parameters or
        detections are insufficient.
        """
        system = self.config.get("system", {})
        q_aq = float(system.get("aqueous_flow_ul_min", 0.0) or 0.0)
        chan_w = float(system.get("channel_width_um", 0.0) or 0.0)
        chan_d = float(system.get("channel_depth_um", 0.0) or 0.0)
        if q_aq <= 0 or chan_w <= 0 or chan_d <= 0 or not tracks:
            return {}, {}

        accepted_ids = {
            t["droplet_id"] for t in tracks if t.get("droplet_id") is not None
        }
        per_droplet: dict = {}
        all_centroids = []
        for det in self.all_detections:
            for prop in det["properties"]:
                did = prop.get("droplet_id")
                if did is None or did not in accepted_ids:
                    continue
                all_centroids.append(
                    (prop.get("centroid_x", 0.0), prop.get("centroid_y", 0.0))
                )
                entry = per_droplet.setdefault(int(did), {"w": [], "h": []})
                entry["w"].append(prop.get("bbox_width", 0.0))
                entry["h"].append(prop.get("bbox_height", 0.0))

        if len(all_centroids) < 2 or not per_droplet:
            return {}, {}

        pts = np.asarray(all_centroids, dtype=np.float64)
        _, _, vh = np.linalg.svd(pts - pts.mean(axis=0), full_matrices=False)
        axis = vh[0]
        # cross-stream extent is perpendicular to the dominant flow axis
        cross_is_x = abs(axis[0]) < abs(axis[1])

        geom_px = {}
        cross_all = []
        for did, ext in per_droplet.items():
            w = float(np.median(ext["w"]))
            h = float(np.median(ext["h"]))
            cross = w if cross_is_x else h
            flow = h if cross_is_x else w
            geom_px[did] = (flow, cross)
            cross_all.append(cross)

        median_cross_px = float(np.median(cross_all))
        if median_cross_px <= 0:
            return {}, {}
        scale = chan_w / median_cross_px  # um per px

        droplet_geometry = {}
        volumes = []
        for did, (flow_px, cross_px) in geom_px.items():
            length_um = flow_px * scale
            width_um = cross_px * scale
            vol_nl = self._confined_volume_nl(length_um, width_um, chan_d)
            droplet_geometry[did] = {
                "volume_nl": vol_nl,
                "length_um": length_um,
                "width_um": width_um,
            }
            volumes.append(vol_nl)

        median_volume_nl = float(np.median(volumes)) if volumes else 0.0
        fps = self.config.get("analysis", {}).get("fps") or self.video_reader.fps or 1
        q_aq_nl_s = q_aq * 1000.0 / 60.0  # ul/min -> nl/s
        f_true = q_aq_nl_s / median_volume_nl if median_volume_nl > 0 else 0.0
        t_analyzed = self.frame_count / fps if fps > 0 else 0.0
        n_true = f_true * t_analyzed

        true_stats = {
            "droplet_volume_nl": median_volume_nl,
            "true_frequency_hz": f_true,
            "true_count": n_true,
            "scale_um_px": scale,
        }
        return true_stats, droplet_geometry

    def _get_results(self, elapsed: float) -> dict:
        """Compile results"""
        tracks = self._track_summaries()

        # Count detections and completed droplets separately
        total_detections = sum(d["count"] for d in self.all_detections)
        total_droplets = len(tracks)
        avg_per_frame = total_detections / self.frame_count if self.frame_count > 0 else 0

        # Get all diameters
        diameters = []
        for det in self.all_detections:
            for prop in det["properties"]:
                diameters.append(prop["equivalent_diameter"])

        # Basic stats
        mean_diameter = np.mean(diameters) if diameters else 0
        std_diameter = np.std(diameters) if diameters else 0

        # Scale to microns if configured
        scale = self.config.get("analysis", {}).get("microns_per_pixel", 1.0)
        mean_diameter_um = mean_diameter * scale
        std_diameter_um = std_diameter * scale
        mean_track_speed = (
            float(np.mean([t["speed_mm_s"] for t in tracks])) if tracks else 0.0
        )
        frequency = 0.0
        fps = self.config.get("analysis", {}).get("fps") or self.video_reader.fps or 1
        if tracks:
            first_frame = min(t["first_frame"] for t in tracks)
            last_frame = max(t["first_frame"] for t in tracks)
            span_s = (last_frame - first_frame) / fps if last_frame > first_frame else 0
            frequency = (len(tracks) - 1) / span_s if span_s > 0 else 0.0

        true_stats, droplet_geometry = self._compute_true_statistics(tracks)

        return {
            "total_droplets": total_droplets,
            "total_detections": total_detections,
            "frames_processed": self.frame_count,
            "avg_droplets_per_frame": avg_per_frame,
            "mean_diameter_um": mean_diameter_um,
            "std_diameter_um": std_diameter_um,
            "mean_speed_mm_s": mean_track_speed,
            "frequency_hz": frequency,
            "cv_diameter": std_diameter / mean_diameter if mean_diameter > 0 else 0,
            "processing_time": elapsed,
            "fps_achieved": self.frame_count / elapsed if elapsed > 0 else 0,
            "frame_data": self.all_detections,
            "stored_frames": self.stored_frame_data,
            "analysis": self.config.get("analysis", {}),
            "threshold": self.threshold_value,
            "threshold_groups": self.threshold_finder.threshold_groups,
            "tracks": tracks,
            "trajectory": self.trajectory_meta,
            "true_stats": true_stats,
            "droplet_geometry": droplet_geometry,
            "cache_dir": str(self.cache_dir) if self.cache_dir else None,
        }
