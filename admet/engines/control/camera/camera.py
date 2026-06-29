from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


class PypylonUnavailableError(ImportError):
    """Raised when pypylon is required but unavailable."""


@dataclass(frozen=True)
class CameraAvailability:
    pypylon_available: bool
    refresh_ok: bool
    camera_count: int = 0
    cameras: tuple[str, ...] = ()
    transport_layers: tuple[str, ...] = ()
    pylon_camemu: str = ""
    pylon_module_loaded: bool = False
    message: str = ""
    error_type: str = ""


class Camera:
    def __init__(self, pylon_module: Any | None = None):
        self._pylon = pylon_module
        self.device = None
        self._is_grabbing = False

    @property
    def connected(self) -> bool:
        try:
            return bool(self.device and self.device.IsOpen())
        except Exception:
            return False

    def enumerate_cameras(self) -> list[str]:
        try:
            factory = self._pylon_module().TlFactory.GetInstance()
            return [self._format_device(device) for device in factory.EnumerateDevices()]
        except PypylonUnavailableError:
            raise
        except Exception as exc:
            log.debug("Camera enumeration failed: %s", exc)
            return []

    def preflight(self, pylon_camemu: str | None = None) -> CameraAvailability:
        module_was_loaded = self._pylon is not None
        previous_camemu = os.environ.get("PYLON_CAMEMU", "")
        changed_after_load = False
        if pylon_camemu is not None and pylon_camemu.strip():
            changed_after_load = module_was_loaded and pylon_camemu.strip() != previous_camemu
            os.environ["PYLON_CAMEMU"] = pylon_camemu.strip()
        active_camemu = os.environ.get("PYLON_CAMEMU", "")
        try:
            pylon = self._pylon_module()
            factory = pylon.TlFactory.GetInstance()
            cameras = tuple(self._format_device(device) for device in factory.EnumerateDevices())
            transport_layers = self._enumerate_transport_layers(factory)
        except PypylonUnavailableError as exc:
            return CameraAvailability(
                pypylon_available=False,
                refresh_ok=False,
                pylon_camemu=active_camemu,
                pylon_module_loaded=module_was_loaded,
                message=str(exc.__cause__ or exc),
                error_type=type(exc.__cause__ or exc).__name__,
            )

        camera_count = len(cameras)
        if camera_count:
            message = f"{camera_count} Basler camera(s) detected."
        else:
            message = "No Basler cameras are currently enumerated; refresh after attaching one."
        if changed_after_load:
            message = f"{message} Restart the app if camera driver environment changed."
        return CameraAvailability(
            pypylon_available=True,
            refresh_ok=True,
            camera_count=camera_count,
            cameras=cameras,
            transport_layers=transport_layers,
            pylon_camemu=active_camemu,
            pylon_module_loaded=self._pylon is not None,
            message=message,
        )

    def open(self, camera_index: int = 0, *, apply_defaults: bool = True) -> bool:
        try:
            pylon = self._pylon_module()
            factory = pylon.TlFactory.GetInstance()
            devices = factory.EnumerateDevices()
            if not devices or camera_index >= len(devices):
                return False

            self.device = pylon.InstantCamera(factory.CreateDevice(devices[camera_index]))
            self.device.Open()
            if apply_defaults:
                self.init_settings()
            return True
        except PypylonUnavailableError:
            raise
        except Exception as exc:
            log.error("Failed to open camera: %s", exc)
            self.device = None
            return False

    def init_settings(self) -> None:
        if not self.device:
            return
        try:
            self.device.UserSetSelector.Value = "Default"
            self.device.UserSetLoad.Execute()
            self.set_parameter("DeviceLinkThroughputLimitMode", "Off")
            self.set_parameter("MaxNumBuffer", 50)
            for auto_feature in ("ExposureAuto", "GainAuto", "BalanceWhiteAuto"):
                self.set_parameter(auto_feature, "Off")
        except Exception as exc:
            log.warning("Could not apply initial camera settings: %s", exc)

    def close(self) -> None:
        if not self.device:
            return
        try:
            self.stop_grabbing()
            if self.device.IsOpen():
                self.device.Close()
        finally:
            self.device = None

    def set_parameter(self, param_name: str, value: Any) -> bool:
        try:
            if not self.device or not hasattr(self.device, param_name):
                return False
            param = getattr(self.device, param_name)
            if hasattr(param, "SetValue"):
                param.SetValue(value)
                return True
        except Exception as exc:
            log.debug("Failed to set camera parameter %s: %s", param_name, exc)
        return False

    def get_parameter(self, param_name: str, *, value_only: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {}
        try:
            if not self.device or not hasattr(self.device, param_name):
                return result
            param = getattr(self.device, param_name)
            if hasattr(param, "Value"):
                result["value"] = param.Value
                if value_only:
                    return result
            for attr, key in (("Min", "min"), ("Max", "max"), ("Inc", "inc"), ("Symbolics", "symbolics")):
                if hasattr(param, attr):
                    result[key] = getattr(param, attr)
        except Exception:
            return {}
        return result

    def apply_settings(self, settings: dict[str, Any] | None) -> bool:
        if not self.device or settings is None:
            return False
        was_grabbing = self._is_grabbing
        try:
            if self._is_grabbing:
                self.stop_grabbing()
            for name, value in settings.items():
                self.set_parameter(name, value)
            return True
        except Exception as exc:
            log.error("Camera configuration failed: %s", exc)
            return False
        finally:
            if was_grabbing:
                self.start_grabbing()

    def get_settings(self, params: list[str] | None) -> dict[str, dict[str, Any]]:
        if not self.device or params is None:
            return {}
        return {name: self.get_parameter(name) for name in params}

    def start_grabbing(self, *, latest_only: bool = True) -> None:
        if not self.device:
            return
        if self.device.IsGrabbing():
            self._is_grabbing = True
            return

        pylon = self._pylon_module()
        strategy = pylon.GrabStrategy_LatestImageOnly if latest_only else pylon.GrabStrategy_OneByOne
        try:
            self.device.StartGrabbing(strategy)
            self._is_grabbing = True
        except Exception as exc:
            log.error("Failed to start camera grabbing: %s", exc)
            self._is_grabbing = False

    def stop_grabbing(self) -> None:
        if not self.device:
            return
        try:
            if self.device.IsGrabbing():
                self.device.StopGrabbing()
            self._is_grabbing = False
        except Exception as exc:
            log.error("Failed to stop camera grabbing: %s", exc)
            self._is_grabbing = False

    def grab_frame(self, timeout_ms: int = 5) -> np.ndarray | None:
        if not self.device or not self.device.IsGrabbing():
            return None

        result = None
        try:
            result = self.device.RetrieveResult(
                timeout_ms,
                self._pylon_module().TimeoutHandling_Return,
            )
            if result and result.GrabSucceeded():
                return result.GetArray()
            return None
        except Exception:
            return None
        finally:
            if result:
                result.Release()

    def get_resulting_framerate(self) -> float:
        for name in ("ResultingFrameRate", "ResultingFrameRateAbs"):
            param = self.get_parameter(name, value_only=True)
            if param and "value" in param:
                return float(param.get("value", 0.0))
        return 0.0

    def _pylon_module(self):
        if self._pylon is not None:
            return self._pylon
        try:
            from pypylon import pylon
        except ImportError as exc:
            raise PypylonUnavailableError(
                "pypylon is required for Basler camera control."
            ) from exc
        self._pylon = pylon
        return self._pylon

    def _format_device(self, device: Any) -> str:
        try:
            return f"{device.GetModelName()} ({device.GetSerialNumber()})"
        except Exception:
            return "Unknown Camera"

    def _enumerate_transport_layers(self, factory: Any) -> tuple[str, ...]:
        if not hasattr(factory, "EnumerateTls"):
            return ()
        try:
            return tuple(self._format_transport_layer(tl) for tl in factory.EnumerateTls())
        except Exception as exc:
            log.debug("Transport layer enumeration failed: %s", exc)
            return ()

    def _format_transport_layer(self, transport_layer: Any) -> str:
        values = []
        for attr in ("GetFriendlyName", "GetDeviceClass", "GetFullName", "GetInternalName"):
            if not hasattr(transport_layer, attr):
                continue
            try:
                value = getattr(transport_layer, attr)()
            except Exception:
                continue
            if value:
                values.append(str(value))
        if not values:
            return "Unknown Transport"
        deduped = list(dict.fromkeys(values))
        return " / ".join(deduped)
