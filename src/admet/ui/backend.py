"""Standalone Qt access to the same project and protocol service."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

from admet.core.run import RunResult
from admet.core.compat import preflight_metadata
from admet.core.service import Admet
from admet.engines.acquisition.settings import CORRECTION_PARAM_NAMES


class DesktopBackend:
    aliases = {
        "camera_status": "read_status",
        "refresh_cameras": "list_cameras",
        "apply_camera_settings": "set_camera_settings",
        "cleanup_shutdown": "shutdown_instrument",
    }

    def __init__(self, *, simulated=False, service=None):
        self.service = service or Admet()
        self.simulated = simulated
        self.engine = self.service.engine("acquisition")
        self.lock = threading.RLock()
        self._previews = {}

    def preview(self, scope, document):
        with self.lock:
            previous = self._previews.pop(scope, None)
            if previous:
                self.service.discard_protocol_preview(previous)
            plan = self.service.plan_protocol("run_json_protocol", {
                "protocol": document, "include_video": self.acquisition_mode == "camera_fluidics",
            })
            self._previews[scope] = plan["plan_id"]
            return plan

    @property
    def settings(self):
        return self.engine.settings

    @property
    def acquisition_mode(self):
        return self.session.metadata.get("acquisition_mode", "fluidics_only") if self.session else "fluidics_only"

    def set_acquisition_mode(self, mode):
        if mode not in {"fluidics_only", "camera_fluidics"}:
            raise ValueError("Unknown acquisition mode")
        with self.lock:
            self.service._require_project_idle()
            if not self.session:
                raise RuntimeError("Open a project before choosing acquisition mode")
            if self.engine.recording_active:
                raise RuntimeError("Stop recording before changing acquisition mode")
            if mode == self.acquisition_mode:
                return
            project = self.service.project
            project.session = replace(project.session, metadata={**project.session.metadata, "acquisition_mode": mode})
            project.save()
            self._discard_previews()

    @property
    def session(self):
        return self.service.project.session if self.service.project else None

    @property
    def workdir(self):
        return str(self.service.project.path) if self.service.project else None

    def create_project(self, path):
        with self.lock:
            project = self.service.create_project(path)
            self._discard_previews()
            return project

    def open_project(self, path):
        with self.lock:
            project = self.service.open_project(path)
            self._discard_previews()
            return project

    def _discard_previews(self):
        for plan_id in self._previews.values():
            self.service.discard_protocol_preview(plan_id)
        self._previews.clear()

    def save_project(self, *, checkup=None):
        with self.lock:
            self.service._require_project_idle()
            if self.service.project:
                if checkup is not None:
                    project = self.service.project
                    project.session = replace(project.session, metadata=preflight_metadata(
                        project.session.metadata, checkup,
                    ))
                self.service.project.save()

    def measurements(self, run_id, changes=None):
        with self.lock:
            return self.service.protocol_store().measurements(run_id, changes)

    def measurement_runs(self):
        with self.lock:
            return self.service.protocol_store().measurement_runs() if self.service.project else []

    def call(self, operation, settings=None):
        with self.lock:
            settings = dict(settings or {})
            methods = {
                "plan_protocol": self.service.plan_protocol,
                "planned_protocols": self.service.planned_protocols,
                "cancel_protocol_plan": self.service.cancel_protocol_plan,
                "control_protocol": self.service.control_protocol,
            }
            if operation in methods:
                return methods[operation](**settings)
            if operation in {"connect_fluidics", "disconnect_fluidics", "apply_corrections",
                             "connect_camera", "list_cameras", "set_channel_flow",
                             "set_channel_pressure"}:
                from admet.core.run import RunJob

                return self.run(RunJob("desktop", "acquisition", operation, settings)).metadata
            from admet.workflows.operations import operation as lookup

            if lookup(operation).starts_protocol:
                raise RuntimeError("Build and review a plan before execution")
            return self.service.do(operation, settings)

    def run(self, job):
        action = self.aliases.get(job.action, job.action)
        settings = dict(job.settings)
        with self.lock:
            if action == "read_status":
                return self.service.run("acquisition", action)
            if action == "shutdown_instrument":
                return RunResult(job.id, "acquisition", action, metadata=self.shutdown())
            if self.simulated:
                if action in {"verify_backend", "verify_fluigent"}:
                    return RunResult(job.id, "acquisition", action, metadata={
                        "fluigent_detect_ok": True,
                        "fluigent_device_message": "Simulated session; physical discovery disabled",
                    })
                if action in {"list_cameras", "connect_camera"}:
                    raise RuntimeError("Camera discovery is disabled in simulation; no real SDK calls")
                if action == "connect_fluidics":
                    settings["simulated"] = True
            if action == "run_protocol":
                raise RuntimeError("Open a JSON protocol, review its plan, then Execute")
            if action in {"set_channel_flow", "set_channel_pressure", "stop_channel"}:
                self._validate_manual_channel(action, settings)
                return self.service.run("acquisition", action, settings, job_id=job.id)
            if self.service.state()["running"] and action not in {
                "stop_protocol", "pause_protocol", "resume_protocol", "confirm_protocol",
                "skip_protocol", "shutdown_instrument", "stop_camera_live",
            }:
                raise RuntimeError("Finish or abort the protocol before changing the rig")
            if action in {"connect_fluidics", "disconnect_fluidics", "apply_corrections"}:
                allowed = {"simulated"} if action == "connect_fluidics" else set()
                if action == "apply_corrections":
                    allowed = set(CORRECTION_PARAM_NAMES)
                result = self.service.do(action, {k: v for k, v in settings.items() if k in allowed})
                return RunResult(job.id, "acquisition", action, metadata=result)
            return self.service.run("acquisition", action, settings, job_id=job.id)

    def _validate_manual_channel(self, action, settings):
        from admet.workflows.json_protocol import normalize, validate_channels

        if not self.engine.hardware.connected:
            raise RuntimeError("Fluidics hardware is not connected")
        index = settings.get("channel_index")
        channels = self.engine.channel_manager.channels
        if type(index) is not int or not 0 <= index < len(channels):
            raise ValueError("Select a connected channel index")
        if channels[index].owner != "user" and self.engine.pipeline_state != "paused":
            raise RuntimeError(f"Channel {index} is controlled by the protocol; pause or finish it first")
        if action == "stop_channel":
            return
        flow = action == "set_channel_flow"
        if flow and not self.service.state()["corrections"]:
            raise RuntimeError("Apply fluidics corrections before setting flow")
        value = settings.get("channel_flow_ul_min" if flow else "channel_pressure_mbar")
        document = normalize({"name": "manual", "steps": [{
            "name": "Manual control", "sensor_setpoints" if flow else "pressure_setpoints": {str(index): value},
            "trigger_type": "time", "trigger_params": {"duration_s": 0},
        }]})
        validate_channels(document, self.service._cached_channel_mapping())

    def emergency_stop(self):
        # Deliberately independent of the normal command lock.
        return self.engine.emergency_stop("Qt operator emergency stop")

    def shutdown(self):
        # Zero first, even if another command is blocked.
        result = self.emergency_stop()
        with self.lock:
            shutdown = self.service.run("acquisition", "shutdown_instrument").metadata
            self.service.planned_protocols()
        errors = [*result.get("errors", []), *shutdown.get("cleanup_errors", [])]
        if errors:
            raise RuntimeError("; ".join(map(str, errors)))
        return shutdown

    def plan_file(self, path):
        with self.lock:
            return self.service.plan_protocol_file(str(Path(path)))
