import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from admet.core.engine import EngineContext
from admet.engines.control.engine import FluidicsControlEngine
from admet.engines.control.camera import Camera
from admet.engines.control.camera.camera import CameraAvailability
from admet.engines.control.fluidics import PressureChannelInfo, SensorChannelInfo
from admet.engines.control.fluidics.config import (
    ProtocolStep,
    build_dropseq_protocol,
    build_priming_protocol,
    build_wash_protocol,
)


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

    def detect_instruments(self):
        return [{"serial": 1, "type": "LineUP"}]

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

    def set_sensor_calibration(self, sensor_index, calibration):
        self.calls.append(("sensor_calibration", sensor_index, calibration))

    def set_sensor_regulation(self, sensor_index, pressure_index, setpoint):
        self.calls.append(("regulate", sensor_index, pressure_index, setpoint))

    def set_sensor_regulation_response(self, sensor_index, response_s):
        self.calls.append(("sensor_response", sensor_index, response_s))

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

    def preflight(self):
        return CameraAvailability(
            pypylon_available=True,
            refresh_ok=True,
            camera_count=1,
            cameras=("Basler Test (123)",),
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


class FakeVideoWriter:
    def __init__(self, video_dir, prefix, width, height, fps):
        self.video_dir = Path(video_dir)
        self.prefix = prefix
        self.width = width
        self.height = height
        self.fps = fps
        self.frame_count = 3
        self.path = self.video_dir / f"{prefix}.avi"

    def start(self):
        self.video_dir.mkdir(parents=True, exist_ok=True)
        return True

    def write(self, frame):
        return True

    def stop(self):
        self.path.write_bytes(b"AVI")
        return str(self.path)


class FakeCameraAcquisition:
    def __init__(self):
        self.recording = False
        self.writer = None
        self.callback = None
        self.preview_enabled = True
        self.last_recording_frames = None
        self.last_writer_frame_count = None

    def is_alive(self):
        return True

    def start_recording(self, writer, *, max_frames=None, max_time=None):
        self.writer = writer
        self.recording = writer.start()
        return self.recording

    def stop_recording(self):
        self.last_recording_frames = 3
        self.last_writer_frame_count = self.writer.frame_count
        self.recording = False
        self.writer.stop()
        self.writer = None
        return self.last_recording_frames

    def set_recording_complete_callback(self, callback):
        self.callback = callback

    def set_preview_enabled(self, enabled):
        self.preview_enabled = enabled

    def frame_processed(self):
        pass


class FakeFrameAcknowledger:
    def __init__(self):
        self.processed = 0

    def frame_processed(self):
        self.processed += 1


class FluidicsControlEngineTests(unittest.TestCase):
    def test_declared_actions_have_handlers(self):
        engine = FluidicsControlEngine(FakeControlSDK())

        self.assertEqual({action.id for action in engine.actions}, set(engine._action_handlers))

    def test_connect_configures_channels_and_returns_status(self):
        sdk = FakeControlSDK()
        engine = FluidicsControlEngine(sdk)

        result = engine.run_action(
            "connect_fluidics",
            {"simulated": True, "start_polling": False},
        )

        metadata = result.result_set.metadata
        self.assertEqual(metadata["action"], "connect_fluidics")
        self.assertTrue(metadata["connected"])
        self.assertTrue(metadata["simulated"])
        self.assertEqual(metadata["pressure_channels"], 2)
        self.assertEqual(metadata["sensor_channels"], 2)
        self.assertEqual(result.result_set.records, ())
        self.assertEqual(len(engine.channel_manager.channels), 2)
        self.assertFalse(metadata["polling_active"])
        self.assertIn(("init", None), sdk.calls)

        engine.run_action("disconnect_fluidics", {})
        self.assertFalse(engine.hardware.connected)
        self.assertIn(("close",), sdk.calls)

    def test_missing_real_fluigent_returns_warning_and_preserves_simulated_connect(self):
        class NoInstrumentSDK(FakeControlSDK):
            def detect_instruments(self):
                return []

        sdk = NoInstrumentSDK()
        engine = FluidicsControlEngine(sdk)

        preflight = engine.run_action("verify_backend", {})
        self.assertEqual(preflight.result_set.metadata["fluigent_instrument_count"], 0)

        missing = engine.run_action(
            "connect_fluidics",
            {"simulated": False, "start_polling": False},
        )
        metadata = missing.result_set.metadata

        self.assertFalse(metadata["fluigent_connect_ok"])
        self.assertFalse(metadata["connected"])
        self.assertFalse(engine.hardware.connected)
        self.assertEqual(engine.channel_manager.channels, [])
        self.assertNotIn(("init", None), sdk.calls)

        connected = engine.run_action(
            "connect_fluidics",
            {"simulated": True, "start_polling": False},
        )

        self.assertTrue(connected.result_set.metadata["fluigent_connect_ok"])
        self.assertTrue(connected.result_set.metadata["connected"])
        self.assertTrue(connected.result_set.metadata["simulated"])

    def test_start_recording_uses_context_workdir(self):
        sdk = FakeControlSDK()
        engine = FluidicsControlEngine(sdk)
        engine.run_action("connect_fluidics", {"simulated": False, "start_polling": False})

        with tempfile.TemporaryDirectory() as tmpdir:
            result = engine.run_action(
                "start_recording",
                {
                    "recording_root": "media/control/control_20260701_120000",
                    "recording_label": "set01_rep01",
                },
                EngineContext(workdir=tmpdir),
            )
            csv_path = Path(result.artifacts["csv_path"])
            self.assertTrue(result.result_set.metadata["recording_active"])
            engine.run_action("stop_recording", {})

        self.assertEqual(csv_path.parent.name, "fluidics")
        self.assertEqual(csv_path.parent.parent.parent.name, "control")
        self.assertEqual(csv_path.parent.parent.name, "control_20260701_120000")
        self.assertTrue(csv_path.name.startswith("set01_rep01_"))
        self.assertFalse(engine.recording_active)

    def test_start_recording_pairs_camera_video_and_fluidics_csv(self):
        sdk = FakeControlSDK()
        engine = FluidicsControlEngine(sdk, video_writer_factory=FakeVideoWriter)
        camera = FakeCameraAcquisition()
        engine._camera._acquisition = camera
        engine._camera._on_frame(np.zeros((12, 16), dtype=np.uint8))
        engine.run_action("connect_fluidics", {"simulated": False, "start_polling": False})

        with tempfile.TemporaryDirectory() as tmpdir:
            result = engine.run_action(
                "start_recording",
                {
                    "recording_root": "media/control/control_20260701_120000",
                    "recording_label": "set01_rep02",
                    "camera_video_fps": 120.0,
                    "camera_preview_off_recording": True,
                },
                EngineContext(workdir=tmpdir),
            )
            stop = engine.run_action("stop_recording", {})
            second = engine.run_action(
                "start_recording",
                {
                    "recording_root": "media/control/control_20260701_120000",
                    "recording_label": "set01_rep03",
                    "camera_video_fps": 120.0,
                    "camera_preview_off_recording": True,
                },
                EngineContext(workdir=tmpdir),
            )
            second_stop = engine.run_action("stop_recording", {})
            report_dir = Path(result.artifacts["report_dir"])
            metadata_path = report_dir / "metadata.json"
            metadata_exists = metadata_path.exists()
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

        self.assertTrue(result.artifacts["csv_path"].endswith(".csv"))
        self.assertEqual(second.artifacts["report_dir"], result.artifacts["report_dir"])
        self.assertEqual(report_dir.parent.name, "control")
        self.assertEqual(report_dir.name, "control_20260701_120000")
        self.assertTrue(metadata_exists)
        self.assertTrue(Path(stop.artifacts["video_path"]).name.startswith("set01_rep02_"))
        self.assertTrue(Path(second_stop.artifacts["video_path"]).name.startswith("set01_rep03_"))
        self.assertEqual(metadata["recording_count"], 2)
        self.assertEqual(Path(stop.artifacts["recording"]["fluidics_csv"]).parent.name, "fluidics")
        self.assertEqual(stop.artifacts["recording"]["width"], 16)
        self.assertEqual(stop.artifacts["recording"]["height"], 12)
        self.assertEqual(stop.artifacts["recording"]["converted_fps"], 120.0)
        self.assertGreater(stop.artifacts["recording"]["acquisition_fps"], 0.0)
        self.assertFalse(engine.recording_active)
        self.assertTrue(camera.preview_enabled)

    def test_camera_frame_delivery_waits_for_ui_acknowledgement(self):
        engine = FluidicsControlEngine(FakeControlSDK())
        acknowledger = FakeFrameAcknowledger()
        engine._camera._acquisition = acknowledger
        frames = []
        unsubscribe = engine.subscribe_camera_frames(frames.append)
        source = np.ones((2, 3), dtype=np.uint8)

        engine._camera._on_frame(source)

        self.assertEqual(len(frames), 1)
        self.assertIsNot(frames[0], source)
        self.assertEqual(acknowledger.processed, 0)

        engine.acknowledge_camera_frame()

        self.assertEqual(acknowledger.processed, 1)
        unsubscribe()
        engine._camera._on_frame(source)
        self.assertEqual(acknowledger.processed, 2)

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

    def test_priming_protocol_uses_configured_dispense_volumes(self):
        engine = FluidicsControlEngine(FakeControlSDK())

        pipeline = engine.build_pipeline_from_steps(
            build_priming_protocol(
                {
                    "prime_oil_volume_ul": 55.0,
                    "prime_aqueous_volume_ul": 8.0,
                }
            )
        )

        self.assertEqual(getattr(pipeline[0].trigger, "_target_ul"), 55.0)
        self.assertEqual(getattr(pipeline[1].trigger, "_target_ul"), 8.0)
        self.assertEqual(getattr(pipeline[2].trigger, "_target_ul"), 8.0)

    def test_dropseq_protocol_splits_total_aqueous_flow(self):
        steps = build_dropseq_protocol(
            {
                "set_count": 1,
                "replicate_count": 1,
                "run_volume_ul": 150.0,
                "run_aqueous_total_flow_ul_min": 100.0,
            }
        )

        self.assertEqual(steps[0].sensor_setpoints, {0: 300.0, 1: 50.0, 2: 50.0})
        self.assertIn("Cells M/Beads M 50", steps[0].confirm_message)

    def test_wash_protocol_uses_configured_volume_pressure_and_duration(self):
        engine = FluidicsControlEngine(FakeControlSDK())

        pipeline = engine.build_pipeline_from_steps(
            build_wash_protocol(
                {
                    "wash_oil_flow_ul_min": 260.0,
                    "wash_aqueous_total_flow_ul_min": 180.0,
                    "wash_oil_volume_ul": 600.0,
                    "wash_pressure_mbar": 1800.0,
                    "wash_pressure_duration_s": 90.0,
                }
            )
        )

        self.assertEqual(pipeline[0].sensor_setpoints, {0: 260.0, 1: 90.0, 2: 90.0})
        self.assertEqual(pipeline[1].pressure_setpoints, {0: 1800.0, 1: 1800.0, 2: 1800.0})
        self.assertEqual(getattr(pipeline[1].trigger, "_duration_s"), 90.0)

    def test_fluidics_control_actions_use_configured_channels(self):
        sdk = FakeControlSDK()
        engine = FluidicsControlEngine(sdk)
        engine.run_action("connect_fluidics", {"simulated": False, "start_polling": False})

        calibrate = engine.run_action("calibrate", {})
        corrections = engine.run_action(
            "apply_corrections",
            {
                "oil_l_calibration": "IPA",
                "oil_l_scale": 2.25,
                "oil_l_offset": 0.0,
                "oil_l_quadratic": 0.0,
                "cells_m_calibration": "H2O",
                "cells_m_scale": 1.0,
                "cells_m_offset": 0.0,
                "cells_m_quadratic": 0.0,
                "beads_m_calibration": "H2O",
                "beads_m_scale": 1.0,
                "beads_m_offset": 0.0,
                "beads_m_quadratic": 0.0,
            },
        )
        engine.run_action("set_channel_flow", {"channel_index": 1, "channel_flow_ul_min": 67.0})
        engine.run_action("set_channel_pressure", {"channel_index": 0, "channel_pressure_mbar": 120.0})
        engine.run_action("set_channel_response", {"channel_index": 1, "channel_response_s": 4})
        engine.run_action("stop_channel", {"channel_index": 1})

        self.assertEqual(calibrate.result_set.metadata["action"], "calibrate")
        self.assertEqual(corrections.result_set.metadata["action"], "apply_corrections")
        self.assertIn(("calibrate", 0), sdk.calls)
        self.assertIn(("calibrate", 1), sdk.calls)
        self.assertIn(("sensor_calibration", 0, 2), sdk.calls)
        self.assertIn(("sensor_calibration", 1, 1), sdk.calls)
        self.assertIn(("custom_scale", 0, 2.25, 0.0, 0.0, None), sdk.calls)
        self.assertIn(("custom_scale", 1, 1.0, 0.0, 0.0, None), sdk.calls)
        self.assertIn(("regulate", 1, 1, 67.0), sdk.calls)
        self.assertIn(("pressure", 0, 120.0), sdk.calls)
        self.assertIn(("sensor_response", 1, 4), sdk.calls)
        self.assertIn(("pressure", 1, 0.0), sdk.calls)

    def test_camera_refresh_and_connect_are_nonfatal_without_device(self):
        engine = FluidicsControlEngine(FakeControlSDK())
        engine.camera = Camera(EmptyPylon)

        refresh = engine.run_action("refresh_cameras", {})
        connect = engine.run_action("connect_camera", {})

        self.assertTrue(refresh.result_set.metadata["pypylon_available"])
        self.assertEqual(refresh.result_set.metadata["camera_count"], 0)
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
