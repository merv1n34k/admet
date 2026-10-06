import tempfile
import unittest
import warnings
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

from admet.engines.acquisition.fluidics.acquisition import FlowIntegral
from admet.engines.acquisition.fluidics import (
    AcquisitionThread,
    ChannelManager,
    CsvLogger,
    FluigentConnectionError,
    FluigentSDK,
    FluigentSDKUnavailableError,
    HardwareManager,
    PressureChannelInfo,
    SensorChannelInfo,
    vendored_sdk_python_path,
)
from admet.engines.acquisition.fluidics.config import SIM_INSTRUMENTS


class FakeFluidicsSDK:
    def __init__(self):
        self.calls = []
        self.pressures = [10.0, 20.0]
        self.flows = [60.0, -30.0]
        self.controllers = [{"sn": 1, "firmware": 2, "index": 0, "type": "LineUP"}]
        self.pressure_channels = [
            PressureChannelInfo(0, 1, 10, 0, "pressure", pmin=0.0, pmax=2000.0),
            PressureChannelInfo(1, 1, 11, 1, "pressure", pmin=0.0, pmax=2000.0),
        ]
        self.sensor_channels = [
            SensorChannelInfo(0, 1, 20, 0, "sensor", "Flow_L_dual", smin=0.0, smax=100.0),
            SensorChannelInfo(1, 1, 21, 1, "sensor", "Flow_M_dual", smin=0.0, smax=40.0),
        ]
        self.initialized = False

    def detect_instruments(self):
        return [{"serial": 1, "type": "LineUP"}]

    def create_simulated_instrument(self, instr_type, serial, firmware, config):
        self.calls.append(("create_sim", instr_type, serial, firmware, list(config)))

    def remove_simulated_instrument(self, instr_type, serial):
        self.calls.append(("remove_sim", instr_type, serial))

    def init(self, instruments=None):
        self.calls.append(("init", instruments))
        self.initialized = True

    def close(self):
        self.calls.append(("close",))
        self.initialized = False

    def get_controllers_info(self):
        return list(self.controllers)

    def get_pressure_channels_info(self):
        return list(self.pressure_channels)

    def get_sensor_channels_info(self):
        return list(self.sensor_channels)

    def set_sensor_custom_scale(self, sensor_index, a, b=0.0, c=0.0, smax=None):
        self.calls.append(("custom_scale", sensor_index, a, b, c, smax))

    def calibrate_pressure(self, pressure_index):
        self.calls.append(("calibrate", pressure_index))

    def set_sensor_regulation(self, sensor_index, pressure_index, setpoint):
        self.calls.append(("regulate", sensor_index, pressure_index, setpoint))

    def set_pressure(self, pressure_index, pressure):
        self.calls.append(("pressure", pressure_index, pressure))

    def get_pressure(self, pressure_index):
        return self.pressures[pressure_index]

    def get_sensor_value(self, sensor_index):
        return self.flows[sensor_index]


class FluidicsSdkTests(unittest.TestCase):
    def test_sdk_import_is_lazy_and_reports_missing_dependency(self):
        sdk = FluigentSDK()

        with patch("importlib.import_module", side_effect=ImportError("missing")):
            with self.assertRaises(FluigentSDKUnavailableError):
                sdk.get_pressure(0)

    def test_vendor_folder_stays_out_of_the_packages(self):
        import tomllib

        path = vendored_sdk_python_path()
        self.assertTrue((path / "Fluigent" / "SDK" / "__init__.py").exists())
        root = Path(__file__).resolve().parents[1]
        build = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]
        self.assertIn("src/admet/vendor", build["wheel"]["exclude"])
        self.assertIn("/src/admet/vendor", build["sdist"]["exclude"])

    def test_sdk_import_suppresses_vendor_pkg_resources_warning(self):
        sdk = FluigentSDK()

        def import_with_vendor_warning(name):
            self.assertEqual(name, "Fluigent.SDK")
            warnings.warn(
                "pkg_resources is deprecated as an API. See setuptools documentation.",
                UserWarning,
                stacklevel=2,
            )
            return SimpleNamespace(__version__="1.0", __file__="/tmp/Fluigent/SDK/__init__.py")

        with patch("importlib.import_module", side_effect=import_with_vendor_warning):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                status = sdk.preflight()

        self.assertTrue(status.available)
        self.assertEqual(caught, [])


class HardwareManagerTests(unittest.TestCase):
    def test_connect_simulated_creates_instruments_and_corrects_sensor_ranges(self):
        sdk = FakeFluidicsSDK()
        manager = HardwareManager(sdk)

        state = manager.connect(simulated=True)

        self.assertTrue(state.connected)
        self.assertTrue(state.simulated)
        self.assertEqual(len([call for call in sdk.calls if call[0] == "create_sim"]), len(SIM_INSTRUMENTS))
        self.assertEqual(state.sensor_channels[0].smax, 5000.0)
        self.assertEqual(state.sensor_channels[1].smax, 80.0)
        self.assertIn(
            ("init", [instrument["serial"] for instrument in SIM_INSTRUMENTS]),
            sdk.calls,
        )
        self.assertTrue(any(call[0] == "custom_scale" for call in sdk.calls))

        manager.disconnect()

        self.assertFalse(manager.connected)
        self.assertIn(("close",), sdk.calls)
        self.assertEqual(len([call for call in sdk.calls if call[0] == "remove_sim"]), len(SIM_INSTRUMENTS))

    def test_calibrate_requires_connection(self):
        with self.assertRaises(RuntimeError):
            HardwareManager(FakeFluidicsSDK()).calibrate(0)

    def test_missing_real_instrument_does_not_block_later_simulated_connect(self):
        class NoInstrumentSDK(FakeFluidicsSDK):
            def detect_instruments(self):
                return []

        sdk = NoInstrumentSDK()
        manager = HardwareManager(sdk)

        with self.assertRaises(FluigentConnectionError):
            manager.connect(simulated=False)

        self.assertFalse(manager.connected)
        self.assertFalse(manager.state.simulated)
        self.assertNotIn(("init", None), sdk.calls)

        state = manager.connect(simulated=True)

        self.assertTrue(state.connected)
        self.assertTrue(state.simulated)
        self.assertIn(
            ("init", [instrument["serial"] for instrument in SIM_INSTRUMENTS]),
            sdk.calls,
        )


class ChannelManagerTests(unittest.TestCase):
    def test_every_command_records_when_it_started(self):
        import time

        manager = ChannelManager(FakeFluidicsSDK())
        manager.configure_channels([(0, 0)])
        self.assertEqual(manager.channels[0].command_started, 0.0)
        for command in (lambda: manager.user_set_flow_regulation(0, 25.0), lambda: manager.user_zero(0),
                        lambda: manager.user_set_pressure(0, 80.0), lambda: manager.pipeline_set_setpoint(0, 50.0)):
            before = time.monotonic()
            command()
            self.assertGreaterEqual(manager.channels[0].command_started, before)

    def test_user_and_pipeline_ownership_rules(self):
        sdk = FakeFluidicsSDK()
        manager = ChannelManager(sdk)
        manager.configure_channels([(0, 0)])

        manager.user_set_flow_regulation(0, 25.0)
        self.assertIn(("regulate", 0, 0, 25.0), sdk.calls)
        self.assertEqual(manager.channels[0].owner, "user")

        manager.pipeline_set_setpoint(0, 50.0)
        with self.assertRaisesRegex(RuntimeError, "controlled by the protocol"):
            manager.user_set_pressure(0, 100.0)
        with self.assertRaisesRegex(RuntimeError, "controlled by the protocol"):
            manager.user_set_flow_regulation(0, 99.0)
        with self.assertRaisesRegex(RuntimeError, "controlled by the protocol"):
            manager.user_stop_regulation(0)
        self.assertEqual(manager.channels[0].base_setpoint, 25)
        self.assertEqual(manager.channels[0].active_setpoint, 50)
        self.assertNotIn(("pressure", 0, 100.0), sdk.calls)
        self.assertEqual(manager.channels[0].owner, "pipeline")

        manager.pipeline_zero_all()
        self.assertIn(("regulate", 0, 0, 0.0), sdk.calls)
        manager.user_set_pressure(0, 100.0)
        self.assertIn(("pressure", 0, 100.0), sdk.calls)

        manager.pipeline_release_all()
        channel = manager.channels[0]
        self.assertEqual(channel.owner, "user")
        self.assertEqual(channel.mode, "off")

    def test_emergency_stop_clears_user_state(self):
        sdk = FakeFluidicsSDK()
        manager = ChannelManager(sdk)
        manager.configure_channels([(0, 0)])
        manager.user_set_flow_regulation(0, 25.0)

        manager.emergency_stop_all()

        channel = manager.channels[0]
        self.assertEqual(channel.base_setpoint, 0.0)
        self.assertEqual(channel.active_setpoint, 0.0)
        self.assertEqual(channel.mode, "off")
        self.assertIn(("pressure", 0, 0.0), sdk.calls)


class FlowIntegralTests(unittest.TestCase):
    """Bad readings are left out of the volume, and bridged, not lost."""

    def _volume(self, flows, dt=0.11):
        integral = FlowIntegral()
        for index, flow in enumerate(flows):
            integral.add(index * dt, flow)
        return integral

    def test_a_spike_like_the_rig_produces_does_not_change_the_volume(self):
        clean = self._volume([5.0] * 50 + [5.2] * 50).volume
        spiked = self._volume([5.0] * 50 + [-1530.0] + [5.2] * 49)

        self.assertAlmostEqual(spiked.volume, clean, delta=abs(clean) * 0.001)
        self.assertEqual([q for _, q in spiked.rejected], [-1530.0])

    def test_a_spike_within_the_sensor_range_is_rejected(self):
        integral = self._volume([67.0] * 20 + [217.0] + [67.0] * 20)

        self.assertEqual([q for _, q in integral.rejected], [217.0])

    def test_a_two_reading_burst_is_rejected(self):
        integral = self._volume([5.0] * 20 + [300.0, -1539.0] + [5.0] * 20)

        self.assertEqual(sorted(q for _, q in integral.rejected), [-1539.0, 300.0])

    def test_a_spike_as_the_very_first_reading_is_rejected(self):
        # A filter that trusts its first reading would judge everything after
        # against the spike, and count the spike as flow.
        integral = self._volume([300.0] + [20.0] * 40)

        self.assertEqual([q for _, q in integral.rejected], [300.0])
        self.assertAlmostEqual(integral.volume, 20.0 * 39 * 0.11 / 60, places=6)

    def test_a_real_step_is_kept(self):
        integral = self._volume([0.0] * 20 + [60.0] * 20)

        self.assertEqual(list(integral.rejected), [])

    def test_the_volume_does_not_wait_for_the_filter(self):
        # The last RADIUS readings are still undecided; counting them
        # provisionally keeps a volume step from stopping late.
        integral = self._volume([60.0] * 11)

        self.assertAlmostEqual(integral.volume, 60.0 * 10 * 0.11 / 60, places=6)

    def test_rejected_readings_are_reported_by_the_acquisition(self):
        sdk = FakeFluidicsSDK()
        acquisition = AcquisitionThread(sdk, pressure_count=2, sensor_count=2, data_queue=Queue(),
                                        interval_ms=100, stability_window_samples=2)
        for flow in [60.0] * 5 + [-1530.0] + [60.0] * 5:
            sdk.flows = [flow, -30.0]
            acquisition.poll_once()

        self.assertEqual([q for _, q in acquisition.rejected_readings(0)], [-1530.0])


class AcquisitionTests(unittest.TestCase):
    def _polled(self, sdk, readings, interval_s):
        """Poll at a controlled pace, as a rig whose reads take interval_s."""
        now = [100.0]
        acquisition = AcquisitionThread(
            sdk, pressure_count=2, sensor_count=2, data_queue=Queue(maxsize=readings + 1),
            interval_ms=100, stability_window_samples=2,
        )
        snapshots = []
        with patch("admet.engines.acquisition.fluidics.acquisition.time.monotonic",
                   side_effect=lambda: now[0]):
            for _ in range(readings):
                snapshots.append(acquisition.poll_once())
                now[0] += interval_s
        return acquisition, snapshots

    def test_poll_once_emits_snapshot(self):
        sdk = FakeFluidicsSDK()
        acquisition, (first, second) = self._polled(sdk, 2, 0.1)

        self.assertEqual(first.pressures, [10.0, 20.0])
        self.assertEqual(second.flows, [60.0, -30.0])
        self.assertTrue(second.stability)
        self.assertEqual(acquisition.get_flow(1), -30.0)

    def test_volume_follows_real_elapsed_time_not_the_nominal_interval(self):
        # Reads on the rig are ~110 ms apart; counting them as 100 ms made
        # volume steps stop ~10 % late.
        sdk = FakeFluidicsSDK()
        acquisition, _ = self._polled(sdk, 92, 0.11)    # 91 intervals x 0.11 s = 10.01 s

        self.assertAlmostEqual(acquisition.get_volume(0), 60.0 * 10.01 / 60, places=6)

    def test_backflow_subtracts_from_volume(self):
        sdk = FakeFluidicsSDK()
        acquisition, _ = self._polled(sdk, 11, 0.1)     # 1 s at -30 uL/min

        self.assertAlmostEqual(acquisition.get_volume(1), -0.5, places=6)

    def test_the_first_reading_counts_no_volume(self):
        sdk = FakeFluidicsSDK()
        acquisition, (first,) = self._polled(sdk, 1, 0.1)

        self.assertEqual(first.volumes_ul, [0.0, 0.0])

    def test_csv_logger_writes_header_and_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = CsvLogger(tmpdir)
            path = Path(logger.start(pressure_count=1, sensor_count=1))
            logger.write_row(
                "12:00:00.000",
                1.234,
                [10.0],
                [2.5],
                volumes=[0.5],
                stability=[True],
            )
            logger.stop()

            contents = path.read_text(encoding="utf-8")

        self.assertIn("pressure_0_mbar", contents)
        self.assertIn("12:00:00.000,1.234,10.00,2.500,0.500,1", contents)
        self.assertEqual(logger.row_count, 1)


if __name__ == "__main__":
    unittest.main()
