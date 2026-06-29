import tempfile
import unittest
from pathlib import Path

from admet.core.engine import EngineContext
from admet.engines.control.engine import FluidicsControlEngine
from admet.engines.control.camera import Camera
from admet.engines.control.camera.camera import CameraAvailability
from admet.engines.control.fluidics import PressureChannelInfo, SensorChannelInfo
from admet.engines.control.fluidics.config import ProtocolStep
from admet.engines.control.settings import CONTROL_ENGINE_SETTINGS


class FakeControlSDK:
    def __init__(self):
        self.calls = []
        self.pressure_channels = [
            PressureChannelInfo(0, 1, 10, 0, "pressure", pmin=0.0, pmax=2000.0),
            PressureChannelInfo(1, 1, 11, 1, "pressure", pmin=0.0, pmax=2000.0),
        ]
        self.sensor_channels = [
            SensorChannelInfo(0, 1, 20, 0, "sensor", "Flow_L_dual", smin=0.0, smax=100.0),
            SensorChannelInfo(1, 1, 21, 1, "sensor", "Flow_M_dual", smin=0.0, smax=40.0),
        ]

    def create_simulated_instrument(self, instr_type, serial, firmware, config):
        self.calls.append(("create_sim", instr_type, serial, firmware, list(config)))

    def remove_simulated_instrument(self, instr_type, serial):
        self.calls.append(("remove_sim", instr_type, serial))

    def init(self, instruments=None):
        self.calls.append(("init", instruments))

    def close(self):
        self.calls.append(("close",))

    def get_controllers_info(self):
        return [{"sn": 1, "firmware": 2, "index": 0, "type": "LineUP"}]

    def get_pressure_channels_info(self):
        return list(self.pressure_channels)

    def get_sensor_channels_info(self):
        return list(self.sensor_channels)

    def set_sensor_custom_scale(self, sensor_index, a, b=0.0, c=0.0, smax=None):
        self.calls.append(("custom_scale", sensor_index, a, b, c, smax))

    def set_sensor_regulation(self, sensor_index, pressure_index, setpoint):
        self.calls.append(("regulate", sensor_index, pressure_index, setpoint))

    def set_pressure(self, pressure_index, pressure):
        self.calls.append(("pressure", pressure_index, pressure))

    def get_pressure(self, pressure_index):
        return 0.0

    def get_sensor_value(self, sensor_index):
        return 0.0

    def calibrate_pressure(self, pressure_index):
        self.calls.append(("calibrate", pressure_index))


class EmptyCameraFactory:
    def EnumerateDevices(self):
        return []

    def CreateDevice(self, device):
        return device


class EmptyPylon:
    GrabStrategy_LatestImageOnly = "latest"
    GrabStrategy_OneByOne = "one_by_one"
    TimeoutHandling_Return = "return"

    class TlFactory:
        factory = EmptyCameraFactory()

        @classmethod
        def GetInstance(cls):
            return cls.factory


class FakeEngineCamera:
    connected = True

    def __init__(self):
        self.set_calls = []
        self.applied = {}

    def preflight(self, pylon_camemu=None):
        return CameraAvailability(
            pypylon_available=True,
            refresh_ok=True,
            camera_count=1,
            cameras=("Basler Test (123)",),
            pylon_camemu=pylon_camemu or "",
            pylon_module_loaded=True,
            message="1 Basler camera(s) detected.",
        )

    def set_parameter(self, name, value):
        self.set_calls.append((name, value))
        return True

    def apply_settings(self, settings):
        self.applied.update(settings)
        return True

    def get_resulting_framerate(self):
        return 120.0

    def get_settings(self, names):
        return {name: {"value": self.applied.get(name)} for name in names if name in self.applied}


class FluidicsControlEngineTests(unittest.TestCase):
    def test_engine_defaults_keep_priming_protocol(self):
        self.assertEqual(CONTROL_ENGINE_SETTINGS.defaults()["pipeline_name"], "Priming")

    def test_connect_configures_channels_and_returns_status(self):
        sdk = FakeControlSDK()
        engine = FluidicsControlEngine(sdk)

        result = engine.run_action(
            "connect_fluidics",
            {"simulated": True, "start_polling": False},
        )

        metadata = result.result_set.metadata
        self.assertTrue(metadata["connected"])
        self.assertTrue(metadata["simulated"])
        self.assertEqual(metadata["pressure_channels"], 2)
        self.assertEqual(metadata["sensor_channels"], 2)
        self.assertEqual(len(engine.channel_manager.channels), 2)
        self.assertFalse(metadata["polling_active"])
        self.assertIn(("init", None), sdk.calls)

        engine.run_action("disconnect_fluidics", {})
        self.assertFalse(engine.hardware.connected)
        self.assertIn(("close",), sdk.calls)

    def test_start_recording_uses_context_workdir(self):
        sdk = FakeControlSDK()
        engine = FluidicsControlEngine(sdk)
        engine.run_action("connect_fluidics", {"simulated": False, "start_polling": False})

        with tempfile.TemporaryDirectory() as tmpdir:
            result = engine.run_action(
                "start_recording",
                {"log_dir": "fluidics-logs"},
                EngineContext(workdir=tmpdir),
            )
            csv_path = Path(result.artifacts["csv_path"])

        self.assertEqual(csv_path.parent.name, "fluidics-logs")
        self.assertTrue(result.result_set.metadata["recording_active"])
        engine.run_action("stop_recording", {})
        self.assertFalse(engine.recording_active)

    def test_build_pipeline_expands_group_repeats(self):
        engine = FluidicsControlEngine(FakeControlSDK())
        steps = [
            ProtocolStep(
                "solo",
                {0: 1.0},
                "time",
                {"duration_s": 1.0},
            ),
            ProtocolStep(
                "g1",
                {0: 1.0},
                "time",
                {"duration_s": 1.0},
                group="x",
                repeat=2,
            ),
            ProtocolStep(
                "g2",
                {0: 2.0},
                "time",
                {"duration_s": 1.0},
                group="x",
                repeat=2,
            ),
        ]

        pipeline = engine.build_pipeline_from_steps(steps)

        self.assertEqual([step.name for step in pipeline], ["solo", "g1", "g2", "g1", "g2"])

    def test_calibrate_action_uses_all_pressure_channels(self):
        sdk = FakeControlSDK()
        engine = FluidicsControlEngine(sdk)
        engine.run_action("connect_fluidics", {"simulated": False, "start_polling": False})

        result = engine.run_action("calibrate", {})

        self.assertEqual(result.result_set.records[0].values["action"], "calibrate")
        self.assertIn(("calibrate", 0), sdk.calls)
        self.assertIn(("calibrate", 1), sdk.calls)

    def test_camera_refresh_and_connect_are_nonfatal_without_device(self):
        engine = FluidicsControlEngine(FakeControlSDK())
        engine.camera = Camera(EmptyPylon)

        refresh = engine.run_action("refresh_cameras", {})
        connect = engine.run_action("connect_camera", {})

        self.assertTrue(refresh.result_set.metadata["pypylon_available"])
        self.assertEqual(refresh.result_set.metadata["camera_count"], 0)
        self.assertNotIn("pylon_camemu", refresh.result_set.metadata)
        self.assertNotIn("pylon_camemu", refresh.result_set.metadata["camera"])
        self.assertFalse(connect.result_set.metadata["camera_connect_ok"])
        self.assertIn("not currently available", connect.result_set.metadata["camera_connect_message"])

    def test_apply_camera_settings_uses_pylonguy_parameter_mapping(self):
        engine = FluidicsControlEngine(FakeControlSDK())
        camera = FakeEngineCamera()
        engine.camera = camera

        result = engine.run_action(
            "apply_camera_settings",
            {
                "camera_width": 512,
                "camera_height": 256,
                "camera_offset_x": 17,
                "camera_offset_y": 31,
                "camera_binning_h": 2,
                "camera_binning_v": 3,
                "camera_exposure_us": 200,
                "camera_gain": 2,
                "camera_pixel_format": "Mono10p",
                "camera_readout": "Fast",
                "camera_waterfall": True,
                "camera_framerate_enabled": True,
                "camera_framerate_hz": 500,
                "camera_throughput_enabled": True,
                "camera_throughput_mbps": 125,
            },
        )

        self.assertTrue(result.result_set.metadata["camera_settings_ok"])
        self.assertIn(("OffsetX", 0), camera.set_calls)
        self.assertIn(("OffsetY", 0), camera.set_calls)
        self.assertEqual(camera.applied["Width"], 512)
        self.assertEqual(camera.applied["Height"], 1)
        self.assertEqual(camera.applied["OffsetX"], 16)
        self.assertEqual(camera.applied["OffsetY"], 32)
        self.assertEqual(camera.applied["BinningHorizontal"], 2)
        self.assertEqual(camera.applied["BinningVertical"], 3)
        self.assertEqual(camera.applied["ExposureTime"], 200)
        self.assertEqual(camera.applied["Gain"], 2)
        self.assertEqual(camera.applied["PixelFormat"], "Mono10p")
        self.assertEqual(camera.applied["SensorReadoutMode"], "Fast")
        self.assertTrue(camera.applied["AcquisitionFrameRateEnable"])
        self.assertEqual(camera.applied["AcquisitionFrameRate"], 500)
        self.assertEqual(camera.applied["DeviceLinkThroughputLimitMode"], "On")
        self.assertEqual(camera.applied["DeviceLinkThroughputLimit"], 125_000_000)


if __name__ == "__main__":
    unittest.main()
