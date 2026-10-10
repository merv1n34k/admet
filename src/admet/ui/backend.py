"""Standalone Qt access to the same project and protocol service."""

from __future__ import annotations

import threading
from pathlib import Path

from admet.core.run import RunResult
from admet.core.service import Admet
from admet.engines.acquisition.settings import CORRECTION_PARAM_NAMES


class DesktopBackend:
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

    @property
    def calibration(self):
        """Each channel's corrections per liquid, as calibrated on this rig."""
        return dict(self.session.metadata.get("calibration") or {}) if self.session else {}

    def save_calibration(self, calibration):
        # Only the project's own lock, not the command lock: a connect holds
        # that for seconds, and the screen saves calibration while it waits.
        if self.service.project is not None:
            self.service.project.update_metadata(calibration=calibration)

    @property
    def liquids(self):
        """Liquids added in this project; the defaults are not listed here."""
        entries = self.session.metadata.get("liquids") if self.session else None
        return list(entries) if isinstance(entries, list) else []

    @property
    def profiles(self):
        """Correction profiles added in this project; the defaults are not listed here."""
        entries = self.session.metadata.get("profiles") if self.session else None
        return list(entries) if isinstance(entries, list) else []

    def _project(self, doing):
        if self.service.project is None:
            raise RuntimeError(f"Open a project before {doing}")
        return self.service.project

    def _all_liquid_ids(self):
        from admet.engines.acquisition.fluidics.liquids import default_liquids

        return {liquid.id for liquid in default_liquids()} | {entry.get("id") for entry in self.liquids}

    def _all_profile_ids(self):
        from admet.engines.acquisition.fluidics.liquids import default_profiles

        return {profile.id for profile in default_profiles()} | {entry.get("id") for entry in self.profiles}

    def add_liquid(self, name, density, viscosity=0.0):
        from admet.engines.acquisition.fluidics.liquids import check_liquid, unique_id

        project = self._project("adding a liquid")
        with project.lock:
            entry = check_liquid({"name": name, "density": density, "viscosity": viscosity})
            entry.update(id=unique_id(entry["name"], self._all_liquid_ids()), updated_at=_now())
            project.update_metadata(liquids=[*self.liquids, entry])
            return entry

    def update_liquid(self, liquid_id, **changes):
        from admet.engines.acquisition.fluidics.liquids import check_liquid

        project = self._project("editing a liquid")
        with project.lock:
            liquids = self.liquids
            index = next((i for i, entry in enumerate(liquids) if entry.get("id") == liquid_id), None)
            if index is None:
                raise ValueError(f"{liquid_id} is not a liquid of this project")
            liquids[index] = {**check_liquid({**liquids[index], **changes}), "updated_at": _now()}
            project.update_metadata(liquids=liquids)
            return liquids[index]

    def remove_liquid(self, liquid_id):
        project = self._project("removing a liquid")
        with project.lock:
            liquids = [entry for entry in self.liquids if entry.get("id") != liquid_id]
            if len(liquids) == len(self.liquids):
                raise ValueError(f"{liquid_id} is not a liquid of this project")
            users = [entry.get("name") for entry in self.profiles if entry.get("liquid") == liquid_id]
            if users:
                raise ValueError(f"Profile {users[0]} uses this liquid; remove or change it first")
            project.update_metadata(liquids=liquids)

    def add_profile(self, **fields):
        from admet.engines.acquisition.fluidics.liquids import check_profile, unique_id

        project = self._project("adding a profile")
        with project.lock:
            entry = check_profile(fields, self._all_liquid_ids())
            entry.update(id=unique_id(entry["name"], self._all_profile_ids()), updated_at=_now())
            project.update_metadata(profiles=[*self.profiles, entry])
            return entry

    def update_profile(self, profile_id, **changes):
        from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNEL_UNITS
        from admet.engines.acquisition.fluidics.liquids import channel_profile, check_profile

        project = self._project("editing a profile")
        with project.lock:
            profiles = self.profiles
            index = next((i for i, entry in enumerate(profiles) if entry.get("id") == profile_id), None)
            if index is None:
                raise ValueError(f"{profile_id} is not a profile of this project")
            entry = {**check_profile({**profiles[index], **changes}, self._all_liquid_ids()), "updated_at": _now()}
            wrong_unit = [prefix for prefix, unit in FLUIDIC_CHANNEL_UNITS.items()
                          if channel_profile(self.calibration, prefix) == profile_id and unit != entry["unit"]]
            if wrong_unit:
                raise ValueError(f"{entry['name']} is in use on a {FLUIDIC_CHANNEL_UNITS[wrong_unit[0]]} channel")
            profiles[index] = entry
            project.update_metadata(profiles=profiles)
            return entry

    def remove_profile(self, profile_id):
        from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNEL_UNITS
        from admet.engines.acquisition.fluidics.liquids import channel_profile

        project = self._project("removing a profile")
        with project.lock:
            profiles = [entry for entry in self.profiles if entry.get("id") != profile_id]
            if len(profiles) == len(self.profiles):
                raise ValueError(f"{profile_id} is not a profile of this project")
            if any(channel_profile(self.calibration, prefix) == profile_id for prefix in FLUIDIC_CHANNEL_UNITS):
                raise ValueError("A channel uses this profile; choose another on the Rig first")
            project.update_metadata(profiles=profiles)

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
            self.service.project.update_metadata(acquisition_mode=mode)
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
                    self.service.project.update_metadata(qt_checkup=checkup)
                else:
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
        action = job.action
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
        return self.service.emergency_stop("Qt operator emergency stop")

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


def _now():
    """When a liquid or profile was last changed, to the second, local time."""
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
