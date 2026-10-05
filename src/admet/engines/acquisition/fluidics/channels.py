from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from .sdk import FluigentSDK

log = logging.getLogger(__name__)


@dataclass
class ChannelState:
    sensor_index: int
    pressure_index: int
    base_setpoint: float = 0.0
    active_setpoint: float = 0.0
    owner: str = "user"
    regulation_active: bool = False
    mode: str = "off"
    pressure_setpoint: float = 0.0


class ChannelManager:
    def __init__(self, sdk: FluigentSDK):
        self._sdk = sdk
        self._lock = threading.Lock()
        self._channels: list[ChannelState] = []
        self._pipeline_paused = False

    @property
    def channels(self) -> list[ChannelState]:
        with self._lock:
            return list(self._channels)

    def configure_channels(self, sensor_pressure_pairs: list[tuple[int, int]]) -> None:
        with self._lock:
            self._channels = [
                ChannelState(sensor_index=sensor_index, pressure_index=pressure_index)
                for sensor_index, pressure_index in sensor_pressure_pairs
            ]

    def user_set_flow_regulation(
        self,
        channel_idx: int,
        setpoint: float,
        *,
        activate: bool = True,
    ) -> None:
        with self._lock:
            channel = self._channels[channel_idx]
            self._require_user_control(channel)
            channel.base_setpoint = setpoint
            channel.pressure_setpoint = 0.0

            channel.active_setpoint = setpoint
            channel.mode = "flow"
            if activate:
                channel.regulation_active = True
                self._sdk.set_sensor_regulation(
                    channel.sensor_index,
                    channel.pressure_index,
                    setpoint,
                )

    def user_set_pressure(self, channel_idx: int, pressure_mbar: float) -> None:
        with self._lock:
            channel = self._channels[channel_idx]
            self._require_user_control(channel)

            channel.mode = "pressure"
            channel.pressure_setpoint = pressure_mbar
            channel.base_setpoint = 0.0
            channel.active_setpoint = 0.0
            channel.regulation_active = False
            self._sdk.set_pressure(channel.pressure_index, pressure_mbar)

    def user_zero(self, channel_idx: int) -> None:
        """Stop a channel by what controls it: flow regulated to 0, or pressure to 0."""
        with self._lock:
            channel = self._channels[channel_idx]
            self._require_user_control(channel)
            channel.base_setpoint = channel.active_setpoint = 0.0
            if channel.mode == "flow":
                channel.regulation_active = True
                self._sdk.set_sensor_regulation(channel.sensor_index, channel.pressure_index, 0.0)
            else:
                channel.pressure_setpoint = 0.0
                self._sdk.set_pressure(channel.pressure_index, 0.0)

    def user_stop_regulation(self, channel_idx: int) -> None:
        with self._lock:
            channel = self._channels[channel_idx]
            self._require_user_control(channel)

            channel.regulation_active = False
            channel.base_setpoint = 0.0
            channel.active_setpoint = 0.0
            channel.mode = "off"
            channel.pressure_setpoint = 0.0
            self._sdk.set_pressure(channel.pressure_index, 0.0)

    def _require_user_control(self, channel):
        if channel.owner != "user" and not self._pipeline_paused:
            raise RuntimeError("Channel is controlled by the protocol; pause or finish it first")

    def pipeline_set_setpoint(self, channel_idx: int, setpoint: float) -> None:
        with self._lock:
            channel = self._channels[channel_idx]
            channel.owner = "pipeline"
            channel.active_setpoint = setpoint
            channel.regulation_active = True
            channel.mode = "flow"
            channel.pressure_setpoint = 0.0
            self._sdk.set_sensor_regulation(
                channel.sensor_index,
                channel.pressure_index,
                setpoint,
            )

    def pipeline_set_pressure(self, channel_idx: int, pressure_mbar: float) -> None:
        with self._lock:
            channel = self._channels[channel_idx]
            channel.owner = "pipeline"
            channel.active_setpoint = 0.0
            channel.regulation_active = False
            channel.mode = "pressure"
            channel.pressure_setpoint = pressure_mbar
            self._sdk.set_pressure(channel.pressure_index, pressure_mbar)

    def pipeline_release_channel(self, channel_idx: int) -> None:
        with self._lock:
            channel = self._channels[channel_idx]
            if channel.owner != "pipeline":
                return

            if channel.mode == "pressure":
                self._sdk.set_pressure(channel.pressure_index, 0.0)
            channel.owner = "user"
            channel.active_setpoint = channel.base_setpoint
            if channel.base_setpoint > 0 or channel.regulation_active:
                channel.mode = "flow"
                self._sdk.set_sensor_regulation(
                    channel.sensor_index,
                    channel.pressure_index,
                    channel.base_setpoint,
                )
            else:
                channel.regulation_active = False
                channel.mode = "off"
                channel.pressure_setpoint = 0.0

    def pipeline_zero_all(self) -> None:
        with self._lock:
            self._pipeline_paused = True
            for channel in self._channels:
                if channel.owner == "pipeline":
                    channel.active_setpoint = 0.0
                    if channel.mode == "pressure":
                        channel.pressure_setpoint = 0.0
                        self._sdk.set_pressure(channel.pressure_index, 0.0)
                    else:
                        self._sdk.set_sensor_regulation(
                            channel.sensor_index,
                            channel.pressure_index,
                            0.0,
                        )

    def pipeline_resume_all(self) -> None:
        with self._lock:
            self._pipeline_paused = False

    def pipeline_release_all(self) -> None:
        with self._lock:
            self._pipeline_paused = False
            for channel in self._channels:
                if channel.owner == "pipeline":
                    if channel.mode == "pressure":
                        self._sdk.set_pressure(channel.pressure_index, 0.0)
                    else:
                        self._sdk.set_sensor_regulation(
                            channel.sensor_index,
                            channel.pressure_index,
                            0.0,
                        )
                    channel.owner = "user"
                    channel.active_setpoint = 0.0
                    channel.regulation_active = False
                    channel.mode = "off"
                    channel.pressure_setpoint = 0.0

    def emergency_stop_all(self) -> None:
        with self._lock:
            for channel in self._channels:
                self._sdk.set_pressure(channel.pressure_index, 0.0)
                channel.active_setpoint = 0.0
                channel.regulation_active = False
                channel.owner = "user"
                channel.mode = "off"
                channel.pressure_setpoint = 0.0
                channel.base_setpoint = 0.0
