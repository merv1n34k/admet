"""Read-only observations preserve unavailable measurements and event cursors."""

import time
import unittest

from admet.core.service import Admet
from admet.workflows.operations import operation


class DisconnectedTests(unittest.TestCase):
    """Nothing connected is a state to report, not an error and not zeros."""

    def setUp(self):
        self.observed = Admet().do("observe")

    def test_disconnected_observation_reports_guards_and_missing_data(self):
        self.assertFalse(self.observed["connection"]["fluidics"])
        self.assertTrue(self.observed["observed_at"])
        self.assertEqual(self.observed["channels"], [])
        self.assertNotIn("validation", self.observed)
        self.assertFalse(self.observed["safety"]["armed"])
        self.assertFalse(self.observed["safety"]["tripped"])
        guards = self.observed["guards"]
        self.assertFalse(guards["fluidics"]["met"])
        self.assertIn("run connect_fluidics first", guards["fluidics"]["why_not"])
        self.assertTrue(guards["idle"]["met"])
        self.assertEqual(guards["idle"]["why_not"], "")
        for name in ("observe", "protocol_events"):
            with self.subTest(operation=name):
                self.assertEqual(operation(name).requires, ())
                self.assertEqual(operation(name).kind, "read")
        self.assertEqual(Admet().do("protocol_events")["events"], [])


class OneAnswerTests(unittest.TestCase):
    """observe replaced read_status rather than sitting beside it."""

    def test_what_observe_reports_is_what_a_refusal_is_decided_on(self):
        # Not merely equal today: read from the same place, so they cannot
        # drift into disagreeing.
        from admet.workflows.operations import REQUIREMENTS

        admet = Admet()

        self.assertEqual(set(admet.do("observe")["guards"]), set(REQUIREMENTS))

    def test_a_refusal_repeats_what_observe_already_said(self):
        from admet.workflows.operations import Refused

        admet = Admet()
        admet.do("connect_fluidics", {"simulated": True})
        try:
            reported = admet.do("observe")["guards"]["corrections"]["why_not"]
            with self.assertRaises(Refused) as caught:
                admet.do("run_priming")
            self.assertIn(reported, str(caught.exception))
        finally:
            admet.do("disconnect_fluidics")

    def test_a_guard_changes_in_observe_once_it_is_satisfied(self):
        admet = Admet()
        self.assertFalse(admet.do("observe")["guards"]["fluidics"]["met"])

        admet.do("connect_fluidics", {"simulated": True})
        try:
            self.assertTrue(admet.do("observe")["guards"]["fluidics"]["met"])
        finally:
            admet.do("disconnect_fluidics")


class ConnectedTests(unittest.TestCase):
    def setUp(self):
        self.admet = Admet()
        self.admet.do("connect_fluidics", {"simulated": True})
        self.admet.do("apply_corrections")
        self.addCleanup(self.admet.do, "disconnect_fluidics")

    def test_every_channel_is_reported_with_its_label(self):
        channels = self.admet.do("observe")["channels"]

        self.assertEqual(len(channels), 3)
        self.assertEqual(channels[0]["label"], "Oil L")
        self.assertEqual(channels[0]["index"], 0)

    def test_fluid_configuration_reports_applied_corrections(self):
        configuration = self.admet.do("observe")["fluidics_configuration"]

        self.assertTrue(configuration["corrections_applied"])
        self.assertEqual(
            [channel["key"] for channel in configuration["channels"]],
            ["oil_l", "cells_m", "beads_m"],
        )
        oil = configuration["channels"][0]
        self.assertEqual(oil["flow_unit"], "L")
        self.assertEqual(oil["calibration"], "H2O")
        self.assertEqual(oil["scale"], 1.0)
        self.assertEqual(oil["offset"], 0.0)
        self.assertEqual(oil["quadratic"], 0.0)

    def test_a_channel_carries_what_the_instrument_says_it_is(self):
        # The operator confirms the oil mapping against this, so it comes from
        # the instrument rather than from the configured order.
        detected = self.admet.do("observe")["channels"][0]["detected"]

        self.assertEqual(detected["sensor_index"], 0)
        self.assertIsNotNone(detected["pressure_max_mbar"])
        self.assertIsNotNone(detected["sensor_type"])

    def test_measurements_arrive_once_polling_has_read_something(self):
        deadline = time.monotonic() + 5.0
        channel = self.admet.do("observe")["channels"][0]
        while channel["flow_ul_min"] is None and time.monotonic() < deadline:
            time.sleep(0.1)
            channel = self.admet.do("observe")["channels"][0]

        self.assertIsNotNone(channel["flow_ul_min"])
        self.assertIsNotNone(channel["pressure_mbar"])
        self.assertIsNotNone(channel["flow_mean_ul_min"])

        for name in (
            "sample_count", "window_duration_s", "flow_min_ul_min", "flow_max_ul_min",
            "pressure_min_mbar", "pressure_max_mbar", "pressure_limit_mbar",
            "pressure_margin_mbar", "pressure_percent_of_limit", "measurement_age_s",
            "stability_rule",
        ):
            with self.subTest(name=name):
                self.assertIn(name, channel)

    def test_disconnected_camera_values_are_null(self):
        camera = self.admet.do("observe")["camera"]

        self.assertFalse(camera["connected"])
        for name in (
            "model", "serial_number", "transport", "device_identity", "frame_width",
            "frame_height", "pixel_format", "configured_frame_rate_hz",
            "measured_frame_rate_hz", "exposure_us", "gain", "frame_count",
            "dropped_frame_count", "latest_frame_age_s", "recording_path",
        ):
            with self.subTest(name=name):
                self.assertIsNone(camera[name])

    def test_a_requested_flow_is_reported_as_requested(self):
        self.admet.do("set_channel_flow", {"channel_index": 0, "channel_flow_ul_min": 4.0})

        channel = self.admet.do("observe")["channels"][0]
        deadline = time.monotonic() + 2.0
        while channel["flow_error_ul_min"] is None and time.monotonic() < deadline:
            time.sleep(0.05)
            channel = self.admet.do("observe")["channels"][0]

        self.assertEqual(channel["mode"], "flow")
        self.assertEqual(channel["requested_flow_ul_min"], 4.0)
        self.assertIsNone(channel["requested_pressure_mbar"])
        self.assertIsNotNone(channel["flow_error_ul_min"])
        self.assertIsNotNone(channel["flow_error_percent"])
        self.admet.do("stop_channel", {"channel_index": 0})


class RunningProtocolTests(unittest.TestCase):
    def setUp(self):
        self.admet = Admet()
        self.admet.do("connect_fluidics", {"simulated": True})
        self.admet.do("apply_corrections")
        self.admet.do("run_steps", {"steps": [
            {"name": "hold oil", "sensor_setpoints": {"0": 5.0},
             "trigger_type": "time", "trigger_params": {"duration_s": 30.0}},
        ], "tick_s": 0.05})
        time.sleep(0.4)
        self.addCleanup(self.admet.do, "disconnect_fluidics")
        self.addCleanup(self._stop)

    def _stop(self):
        if self.admet.state()["running"]:
            self.admet.do("stop_protocol")

    def test_the_running_step_is_reported(self):
        protocol = self.admet.do("observe")["protocol"]

        self.assertEqual(protocol["state"], "running")
        self.assertEqual(protocol["step_name"], "hold oil")
        self.assertEqual(protocol["total_steps"], 1)
        self.assertGreater(protocol["event_sequence"], 0)

    def test_events_can_be_read_from_where_the_caller_left_off(self):
        first = self.admet.do("protocol_events", {"limit": 5})
        newer = self.admet.do(
            "protocol_events", {"after_sequence": first["latest_sequence"], "limit": 5}
        )

        self.assertTrue(first["events"])
        self.assertTrue(
            all(event["sequence"] > first["latest_sequence"] for event in newer["events"])
        )

    def test_the_answer_is_bounded_by_the_limit(self):
        self.assertLessEqual(len(self.admet.do("protocol_events", {"limit": 2})["events"]), 2)

    def test_reading_events_leaves_them_for_the_next_reader(self):
        once = self.admet.do("protocol_events", {"after_sequence": 0, "limit": 5})["events"]
        twice = self.admet.do("protocol_events", {"after_sequence": 0, "limit": 5})["events"]

        self.assertEqual([e["sequence"] for e in once], [e["sequence"] for e in twice])

    def test_an_event_carries_its_outcome_and_both_clocks(self):
        event = self.admet.do("protocol_events", {"limit": 1})["events"][0]

        self.assertIn(event["outcome"], {"running", "completed", "timed_out", "skipped",
                                         "cancelled", "error"})
        self.assertTrue(event["at"])
        self.assertGreater(event["monotonic"], 0)

    def test_observing_does_not_disturb_the_run(self):
        for _ in range(20):
            self.admet.do("observe")

        self.assertTrue(self.admet.state()["running"])


if __name__ == "__main__":
    unittest.main()
