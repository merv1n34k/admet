from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np


class Cache:
    def __init__(self, config: dict[str, Any], cache_dir: str | Path | None = None):
        cache_cfg = config.get("cache", {})
        self.enabled = cache_cfg.get("enabled", True)
        self.max_entries = cache_cfg.get("max_entries", 100)
        configured_dir = cache_dir or cache_cfg.get("dir")
        self.cache_dir = (
            Path(configured_dir)
            if configured_dir
            else Path.home() / ".admet-cache" / "cellpose"
        )
        self.metadata_path = self.cache_dir / "metadata.json"
        self.metadata = self._load_metadata()
        self.config = config

    def is_valid(self, source_filename: str) -> bool:
        if not self.enabled:
            return False
        if self.metadata.get("config_hash") != self.get_config_hash():
            return False
        return (self.cache_dir / f"{self._get_cache_key(source_filename)}.npz").exists()

    def load_frame(self, source_filename: str) -> dict[str, Any]:
        cache_key = self._get_cache_key(source_filename)
        cache_file = self.cache_dir / f"{cache_key}.npz"
        data = np.load(cache_file, allow_pickle=True)
        self._touch(cache_key)
        return {
            "min_projection": data["min_projection"],
            "droplet_coords": [_plain_coordinate(coords) for coords in data["droplet_coords"]],
        }

    def save_frame(self, source_filename: str, min_proj, droplet_coords) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_key = self._get_cache_key(source_filename)
        np.savez(
            self.cache_dir / f"{cache_key}.npz",
            min_projection=min_proj,
            droplet_coords=np.array(droplet_coords, dtype=object),
        )
        self.metadata["config_hash"] = self.get_config_hash()
        self.metadata["frames"][cache_key] = {
            "source": str(source_filename),
            "cached_at": datetime.now().isoformat(),
        }
        self._touch(cache_key, save=False)
        self._enforce_lru()
        self._save_metadata()

    def clear(self) -> None:
        if self.cache_dir.exists():
            shutil.rmtree(self.cache_dir)
        self.metadata = self._default_metadata()

    def get_config_hash(self) -> str:
        keys = [
            "cellpose_flow_threshold",
            "cellpose_cellprob_threshold",
            "min_droplet_diameter",
            "max_droplet_diameter",
            "erosion_pixels",
            "kernel_size",
            "tophat_threshold",
            "min_inclusion_area",
            "max_inclusion_area",
            "edge_buffer",
            "px_to_um",
        ]
        data = {key: self.config.get(key) for key in keys}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]

    def _get_cache_key(self, source_filename: str) -> str:
        name = Path(source_filename).stem
        return hashlib.sha256(name.encode()).hexdigest()[:16]

    def _load_metadata(self) -> dict[str, Any]:
        if self.metadata_path.exists():
            try:
                with self.metadata_path.open("r", encoding="utf-8") as handle:
                    return json.load(handle)
            except (json.JSONDecodeError, OSError):
                return self._default_metadata()
        return self._default_metadata()

    def _default_metadata(self) -> dict[str, Any]:
        return {"version": "1.0", "config_hash": None, "frames": {}, "access_order": []}

    def _save_metadata(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with self.metadata_path.open("w", encoding="utf-8") as handle:
            json.dump(self.metadata, handle, indent=2)

    def _touch(self, cache_key: str, *, save: bool = True) -> None:
        if cache_key in self.metadata["access_order"]:
            self.metadata["access_order"].remove(cache_key)
        self.metadata["access_order"].append(cache_key)
        if save:
            self._save_metadata()

    def _enforce_lru(self) -> None:
        while len(self.metadata["access_order"]) > self.max_entries:
            oldest_key = self.metadata["access_order"].pop(0)
            cache_file = self.cache_dir / f"{oldest_key}.npz"
            if cache_file.exists():
                cache_file.unlink()
            self.metadata["frames"].pop(oldest_key, None)


def _plain_coordinate(value):
    if isinstance(value, str):
        return value
    if isinstance(value, np.ndarray):
        value = value.tolist()
    if isinstance(value, str):
        return value
    return tuple(value)
