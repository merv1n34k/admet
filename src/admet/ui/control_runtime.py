from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from PySide6.QtCore import QTimer

from admet.core.project import ProjectStore
from admet.core.session import SessionFile, SessionItem
from admet.ui.project import save_project
from admet.ui.render import (
    find_session_item,
    fluidics_csv_metadata,
    normalize_recording_metadata,
    prefer_video_row,
    recording_item_id,
    recording_item_metadata,
    recording_video_key,
    recordings_from_metadata,
    session_file_id,
    session_stored_path,
    upsert_session_file,
    upsert_session_item,
    video_file_id,
    video_metadata,
    video_row,
)
from admet.workflows import Stage, StageStatus


class ControlSessionMixin:
    def _can_complete_completed_pipeline_stage(self, stage: Stage) -> bool:
        if not stage.pipeline:
            return False
        if self.workflow_state.statuses.get(stage.id) is not StageStatus.ACTIVE:
            return False
        return not (
            stage.completion_gate == "recording_confirmation"
            and not self._runs_completion_confirmed
        )

    def _schedule_completed_pipeline_stage_finish(self, stage: Stage) -> None:
        if self._completion_pending or not self._can_complete_completed_pipeline_stage(stage):
            return
        self._completion_pending = True
        self._refresh_action_box(stage)
        QTimer.singleShot(500, self._finish_completed_pipeline_stage)

    def _finish_completed_pipeline_stage(self) -> None:
        self._completion_pending = False
        self._complete_completed_pipeline_stage()

    def _complete_completed_pipeline_stage(self) -> None:
        stage = self.workflow.current_stage(self.workflow_state)
        if not self._can_complete_completed_pipeline_stage(stage):
            return
        self._latest_pipeline_event = None
        self._clear_pipeline_confirmation()
        self._dismiss_notification()
        self._complete_current_stage()
        if stage.completion_gate == "recording_confirmation":
            self._runs_completion_confirmed = False

    def _refresh_action_box(self, stage: Stage) -> None:
        self._refresh_runtime_state()
        signature = self._structure_signature(stage)
        if self._window_controller.needs_mount(stage.id, signature):
            self._render_current_stage()
            return
        self._mounted_signature = signature
        self._sync_action_box(stage)
        self._sync_toc()

    def _refresh_runtime_state(self) -> None:
        return None

    def _structure_signature(self, stage: Stage) -> tuple[Any, ...]:
        return (stage.id,)

    def _sync_action_box(self, stage: Stage) -> None:
        return None

    def _sync_toc(self) -> None:
        return None

    def _clear_pipeline_confirmation(self) -> None:
        return None

    def _dismiss_notification(self) -> None:
        return None

    def _store_recording_artifact(self, recording: Any) -> None:
        if not isinstance(recording, dict) or self.project_path is None or self.api.session is None:
            return
        try:
            store = ProjectStore(self.project_path, self.api.session)
            store.append_control_recording(recording)
            self.project_path = store.path
            self.api.session = store.session
            self.api.workdir = str(store.path)
        except Exception as exc:
            self._append_log(f"project: acquisition metadata save failed: {exc}")

    def _register_recording_artifact(self, recording: Any) -> bool:
        if not isinstance(recording, dict) or self.api.session is None:
            return False
        file_ids: list[str] = []
        files = list(self.api.session.files)
        files, recording, file_ids = self._register_video_file(files, recording, file_ids)
        files, recording, file_ids = self._register_fluidics_file(files, recording, file_ids)
        if not file_ids:
            return False
        item_id = recording_item_id(recording)
        existing_item = find_session_item(self.api.session.items, item_id)
        item_files = list(existing_item.files if existing_item is not None else ())
        for file_id in file_ids:
            if file_id not in item_files:
                item_files.append(file_id)
        item = SessionItem(
            id=item_id,
            project_type="control_acquisition",
            engine=self.api.engine.id,
            settings={
                "recording_label": str(
                    recording.get("recording_id") or recording.get("video_prefix") or ""
                )
            },
            files=tuple(item_files),
            metadata=recording_item_metadata(recording),
        )
        self.api.session = replace(
            self.api.session,
            files=tuple(files),
            items=tuple(upsert_session_item(list(self.api.session.items), item)),
        )
        return True

    def _register_video_file(
        self,
        files: list[SessionFile],
        recording: dict[str, Any],
        file_ids: list[str],
    ) -> tuple[list[SessionFile], dict[str, Any], list[str]]:
        video_path = str(recording.get("video_path") or "")
        if not video_path:
            return files, recording, file_ids
        video_path, external = session_stored_path(video_path, self.project_path)
        recording = {**recording, "video_path": video_path}
        metadata = video_metadata(recording)
        if external:
            metadata["external"] = True
        file_id = video_file_id(video_path, files)
        files = upsert_session_file(
            files,
            SessionFile(
                id=file_id,
                path=video_path,
                role="control_video",
                media_type="video/avi",
                metadata=metadata,
            ),
        )
        return files, recording, [*file_ids, file_id]

    def _register_fluidics_file(
        self,
        files: list[SessionFile],
        recording: dict[str, Any],
        file_ids: list[str],
    ) -> tuple[list[SessionFile], dict[str, Any], list[str]]:
        fluidics_csv = str(recording.get("fluidics_csv") or "")
        if not fluidics_csv:
            return files, recording, file_ids
        fluidics_csv, external = session_stored_path(fluidics_csv, self.project_path)
        recording = {**recording, "fluidics_csv": fluidics_csv}
        metadata = fluidics_csv_metadata(recording)
        if external:
            metadata["external"] = True
        file_id = session_file_id("fluidics", fluidics_csv, files)
        files = upsert_session_file(
            files,
            SessionFile(
                id=file_id,
                path=fluidics_csv,
                role="control_fluidics_csv",
                media_type="text/csv",
                metadata=metadata,
            ),
        )
        return files, recording, [*file_ids, file_id]

    def _load_project_recordings(self) -> None:
        if self.project_path is None or self.api.session is None:
            return
        metadata_path = self.project_path / "records" / "metadata.json"
        if not metadata_path.is_file():
            return
        try:
            with metadata_path.open("r", encoding="utf-8") as handle:
                metadata = json.load(handle)
        except Exception as exc:
            self._append_log(f"project: skipped recording metadata {metadata_path}: {exc}")
            return
        processed = 0
        changed = False
        for recording in recordings_from_metadata(metadata):
            normalized = normalize_recording_metadata(recording, metadata_path.parent)
            if self._register_recording_artifact(normalized):
                changed = True
                processed += 1
        if processed:
            self._append_log(f"project: loaded {processed} recording metadata item(s)")
        if changed:
            self._save_loaded_recordings()

    def _save_loaded_recordings(self) -> None:
        try:
            project = save_project(self.project_path, self.api.session)
            self.project_path = project.path
            self.api.session = project.session
            self.api.workdir = str(self.project_path)
        except Exception as exc:
            self._append_log(f"project: recording metadata save failed: {exc}")

    def _video_rows(self) -> list[dict[str, str]]:
        rows: dict[str, dict[str, str]] = {}
        session = self.api.session
        if session is not None:
            for file in session.files:
                if file.role != "control_video" and file.media_type != "video/avi":
                    continue
                data = dict(file.metadata)
                data.setdefault("video_path", file.path)
                row = video_row(data)
                rows[recording_video_key(data, row)] = row
        for recording in self._recording_metadata_sources():
            row = video_row(recording)
            key = recording_video_key(recording, row)
            if prefer_video_row(rows.get(key), row):
                rows[key] = row
        return list(rows.values())

    def _recording_metadata_sources(self) -> list[dict[str, Any]]:
        recordings: list[dict[str, Any]] = []
        if self.last_result is not None:
            result_recording = self.last_result.metadata.get("recording")
            if isinstance(result_recording, dict):
                recordings.append(result_recording)
        for key in ("current_recording", "last_recording"):
            value = self.last_metadata.get(key)
            if isinstance(value, dict):
                recordings.append(value)
        value = self.last_metadata.get("recordings")
        if isinstance(value, list):
            recordings.extend(item for item in value if isinstance(item, dict))
        metadata_sources = getattr(self.api.engine, "recording_metadata_sources", None)
        if callable(metadata_sources):
            recordings.extend(item for item in metadata_sources() if isinstance(item, dict))
        return recordings
