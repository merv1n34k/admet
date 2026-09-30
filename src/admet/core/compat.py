"""Compatibility readers for saved project data; no migrations or hardware calls.

Historical aliases live here so current consumers do not each interpret them.
Readers do not mutate their inputs or replace missing measurements with zero.
"""

from pathlib import Path
import re


def session_project_id(data):
    return str(data.get("project_id") or data.get("id") or "project")


def allows_external_path(metadata):
    return bool(metadata.get("external") or metadata.get("external_media") or metadata.get("nas"))


def recording_id(recording):
    return str(recording.get("recording_id") or recording.get("video_prefix") or "")


def converted_frame_rate(recording):
    return recording.get("converted_fps") or recording.get("fps")


def protocol_run_id(data):
    return data.get("run_id", data.get("plan_id"))


def valid_recorded_run_id(value):
    return isinstance(value, str) and re.fullmatch(r"(?:run|plan)_[a-f0-9]{32}", value) is not None


def preflight_metadata(metadata, settings):
    # Keep historical experimental entries when updating only the setup fields.
    previous = metadata.get("qt_checkup")
    return {**metadata, "qt_checkup": {**(previous if isinstance(previous, dict) else {}), **settings}}


def recording_path(directory, summary):
    value = summary.get("artifacts", {}).get("fluidics_csv")
    if not isinstance(value, str) or not value:
        raise ValueError("This run has no fluidics recording")
    path = Path(value)
    # Rebase project-local recordings first when an .admetp was moved to another PC.
    normalized = value.replace("\\", "/")
    if "/records/" in normalized:
        relative = "records/" + normalized.rsplit("/records/", 1)[1]
        candidate = directory.parents[2] / relative
        if candidate.is_file():
            return candidate
    if not path.is_absolute():
        path = directory.parents[2] / path
    if not path.is_file():
        raise ValueError(f"Recording not found: {value}")
    return path
