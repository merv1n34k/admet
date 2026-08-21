import time
import unittest

from admet.engines.acquisition.protocol import (
    PIPELINES,
    protocol_names,
    StabilityTrigger,
    build_characterise_protocol,
    build_protocol,
    create_trigger,
)

SETTINGS = {"run_oil_flow_ul_min": 300.0, "run_aqueous_total_flow_ul_min": 80.0}


def _drive(trigger, flow_fn, limit_s=4.0):
    start = time.monotonic()
    while time.monotonic() - start < limit_s:
        if trigger.check(flow_fn, lambda _index: 0.0):
            return time.monotonic() - start
        time.sleep(0.01)
    return None


class StabilityTriggerTests(unittest.TestCase):
    def test_steady_flow_settles_after_the_window(self):
        trigger = StabilityTrigger(0, window_s=0.2, timeout_s=3.0)

        elapsed = _drive(trigger, lambda _index: 250.0)

        self.assertIsNotNone(elapsed)
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertFalse(trigger.timed_out)

    def test_a_flow_that_never_settles_times_out_rather_than_hanging(self):
        # what a saturated controller looks like: it never reaches the setpoint
        trigger = StabilityTrigger(0, window_s=0.2, timeout_s=0.5)

        elapsed = _drive(trigger, lambda _index: 60 + (time.monotonic() * 313 % 40))

        self.assertIsNotNone(elapsed, "trigger hung instead of timing out")
        self.assertTrue(trigger.timed_out)

    def test_a_drifting_flow_is_not_steady(self):
        start = time.monotonic()
        trigger = StabilityTrigger(0, window_s=0.2, timeout_s=0.6)

        _drive(trigger, lambda _index: 100 + (time.monotonic() - start) * 50)

        self.assertTrue(trigger.timed_out)

    def test_reset_clears_the_history(self):
        trigger = StabilityTrigger(0, window_s=0.2, timeout_s=0.3)
        _drive(trigger, lambda _index: 250.0)

        trigger.reset()

        self.assertFalse(trigger.timed_out)
        self.assertFalse(trigger.check(lambda _index: 250.0, lambda _index: 0.0))

    def test_registered_with_the_trigger_factory(self):
        self.assertIsInstance(create_trigger("stability", {"sensor_index": 0}), StabilityTrigger)


class CharacteriseProtocolTests(unittest.TestCase):
    def test_holds_the_phase_ratio_at_every_step(self):
        steps = build_characterise_protocol(SETTINGS)

        ratios = {
            round(step.sensor_setpoints[0] / step.sensor_setpoints[1], 6)
            for step in steps
            if step.sensor_setpoints.get(1)
        }
        self.assertEqual(len(ratios), 1)

    def test_sweeps_up_to_the_working_flow(self):
        steps = build_characterise_protocol(SETTINGS)

        oil = [step.sensor_setpoints[0] for step in steps]
        self.assertEqual(oil[:5], [60.0, 120.0, 180.0, 240.0, 300.0])
        self.assertEqual(oil[-1], 0.0, "the sweep must end with the channels off")

    def test_every_measurement_step_waits_for_stability(self):
        steps = build_characterise_protocol(SETTINGS)

        self.assertTrue(all(step.trigger_type == "stability" for step in steps[:-1]))

    def test_the_last_step_zeroes_the_channels(self):
        steps = build_characterise_protocol(SETTINGS)

        self.assertEqual(steps[-1].on_complete, "zero")
        self.assertEqual(set(steps[-1].sensor_setpoints.values()), {0.0})

    def test_reachable_through_build_protocol_without_a_stored_step_list(self):
        self.assertEqual(len(build_protocol("Characterise", SETTINGS)), 6)
        self.assertNotIn("Characterise", PIPELINES)

    def test_every_buildable_protocol_is_an_allowed_pipeline_name(self):
        # the pipeline_name choice validates against protocol_names(), so a name that
        # builds but is not listed there is rejected at run time
        every_setting = {
            **SETTINGS,
            "set_count": 1,
            "replicate_count": 1,
            "run_volume_ul": 150.0,
            "prime_oil_volume_ul": 40.0,
            "prime_aqueous_volume_ul": 5.0,
            "wash_oil_flow_ul_min": 250.0,
            "wash_aqueous_total_flow_ul_min": 160.0,
            "wash_oil_volume_ul": 500.0,
            "wash_pressure_mbar": 2000.0,
            "wash_pressure_duration_s": 120.0,
        }

        for name in protocol_names():
            self.assertTrue(build_protocol(name, every_setting), name)


if __name__ == "__main__":
    unittest.main()
