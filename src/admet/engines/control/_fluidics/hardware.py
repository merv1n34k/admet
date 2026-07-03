from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import SENSOR_REAL_SMAX, SIM_INSTR_TYPE, SIM_INSTRUMENTS
from .sdk import FluigentSDK, PressureChannelInfo, SensorChannelInfo

log = logging.getLogger(__name__)


class FluigentConnectionError(RuntimeError):
    pass


@dataclass
class HardwareState:
    connected: bool = False
    simulated: bool = False
    controllers: list[dict] = field(default_factory=list)
    pressure_channels: list[PressureChannelInfo] = field(default_factory=list)
    sensor_channels: list[SensorChannelInfo] = field(default_factory=list)


class HardwareManager:
    def __init__(self, sdk: FluigentSDK):
        self._sdk = sdk
        self.state = HardwareState()

    @property
    def connected(self) -> bool:
        return self.state.connected

    def connect(self, *, simulated: bool = False) -> HardwareState:
        if self.state.connected:
            self.disconnect()

        if not simulated:
            detected = self.detect_instruments()
            if not detected:
                self._cleanup_failed_connect(simulated=False)
                raise FluigentConnectionError(
                    "No Fluigent instruments detected. Attach a Fluigent device or enable simulated hardware."
                )

        created_simulated = False
        self.state = HardwareState(simulated=simulated)
        try:
            if simulated:
                for instrument in SIM_INSTRUMENTS:
                    self._sdk.create_simulated_instrument(
                        SIM_INSTR_TYPE,
                        instrument["serial"],
                        0,
                        instrument["config"],
                    )
                created_simulated = True

            self._sdk.init()
            self._detect_channels()
            if not self.state.pressure_channels and not self.state.sensor_channels:
                raise FluigentConnectionError(
                    "No Fluigent pressure or flow channels detected."
                )
            if simulated:
                self._apply_real_sensor_ranges()
            self.state.connected = True
        except Exception:
            self._cleanup_failed_connect(simulated=created_simulated)
            raise
        log.info(
            "Connected fluidics hardware: %d pressure channels, %d sensor channels",
            len(self.state.pressure_channels),
            len(self.state.sensor_channels),
        )
        return self.state

    def disconnect(self) -> None:
        if not self.state.connected:
            return
        self._sdk.close()
        if self.state.simulated:
            for instrument in SIM_INSTRUMENTS:
                try:
                    self._sdk.remove_simulated_instrument(SIM_INSTR_TYPE, instrument["serial"])
                except Exception:
                    log.debug("Failed to remove simulated instrument", exc_info=True)
        self.state = HardwareState()

    def calibrate(self, pressure_index: int) -> None:
        if not self.state.connected:
            raise RuntimeError("Not connected")
        self._sdk.calibrate_pressure(pressure_index)

    def calibrate_all(self) -> None:
        for channel in self.state.pressure_channels:
            self.calibrate(channel.index)

    def _detect_channels(self) -> None:
        self.state.controllers = self._sdk.get_controllers_info()
        self.state.pressure_channels = self._sdk.get_pressure_channels_info()
        self.state.sensor_channels = self._sdk.get_sensor_channels_info()

    def detect_instruments(self) -> list[dict]:
        if not hasattr(self._sdk, "detect_instruments"):
            return [{"serial": None, "type": "unknown"}]
        return list(self._sdk.detect_instruments())

    def _cleanup_failed_connect(self, *, simulated: bool) -> None:
        try:
            self._sdk.close()
        except Exception:
            log.debug("Failed to close Fluigent SDK after failed connect", exc_info=True)
        if simulated:
            for instrument in SIM_INSTRUMENTS:
                try:
                    self._sdk.remove_simulated_instrument(SIM_INSTR_TYPE, instrument["serial"])
                except Exception:
                    log.debug("Failed to remove simulated instrument", exc_info=True)
        self.state = HardwareState()

    def _apply_real_sensor_ranges(self) -> None:
        for channel in self.state.sensor_channels:
            if channel.smax <= 0:
                continue
            for key, real_smax in SENSOR_REAL_SMAX.items():
                if key in channel.sensor_type:
                    scale = real_smax / channel.smax
                    self._sdk.set_sensor_custom_scale(
                        channel.index,
                        scale,
                        0.0,
                        0.0,
                        smax=real_smax,
                    )
                    channel.smax = real_smax
                    break
