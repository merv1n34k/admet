import time
import unittest

from admet.engines.acquisition.fluidics.config import (
    BEADS_M_SENSOR,
    CELLS_M_SENSOR,
    OIL_L_SENSOR,
)
from admet.engines.acquisition.protocol import (
    PIPELINES,
    protocol_names,
    StabilityTrigger,
    build_characterise_protocol,
    build_gravimetry_protocol,
    build_protocol,
    create_trigger,
)

GRAVIMETRY_SETTINGS = {
    "gravimetric_target_ul": 100.0,
    "gravimetric_flow_ul_min": 250.0,
    "gravimetric_replicates": 2,
}

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
            "gravimetric_target_ul": 100.0,
            "gravimetric_flow_ul_min": 250.0,
            "gravimetric_replicates": 3,
        }

        for name in protocol_names():
            self.assertTrue(build_protocol(name, every_setting), name)


class GravimetryProtocolTests(unittest.TestCase):
    def test_every_channel_is_dispensed_for_every_replicate(self):
        steps = build_gravimetry_protocol(GRAVIMETRY_SETTINGS)

        dispenses = [step for step in steps if step.name.startswith("Dispense")]
        self.assertEqual(len(dispenses), 6)
        self.assertEqual(
            [next(iter(step.sensor_setpoints)) for step in dispenses],
            [OIL_L_SENSOR, OIL_L_SENSOR, CELLS_M_SENSOR, CELLS_M_SENSOR, BEADS_M_SENSOR, BEADS_M_SENSOR],
        )

    def test_a_dispense_pushes_the_target_volume_at_the_dispense_flow(self):
        step = build_gravimetry_protocol(GRAVIMETRY_SETTINGS)[0]

        self.assertEqual(step.sensor_setpoints, {OIL_L_SENSOR: 250.0})
        self.assertEqual(step.trigger_type, "volume")
        self.assertEqual(step.trigger_params["target_volume_ul"], 100.0)
        self.assertEqual(step.on_complete, "zero")

    def test_every_dispense_is_gated_before_and_after(self):
        # Before, so the tube on the outlet is the one that was weighed empty;
        # after, so a dispense is never followed by another before it is weighed.
        steps = build_gravimetry_protocol(GRAVIMETRY_SETTINGS)

        self.assertTrue(all(step.confirm_message for step in steps))
        self.assertEqual([step.name for step in steps[:2]], ["Dispense Oil L 1", "Weigh Oil L 1"])
        self.assertIn("weigh an empty tube", steps[0].confirm_message)
        self.assertIn("enter empty and full", steps[1].confirm_message)

    def test_the_replicate_count_drives_the_step_count(self):
        one = build_gravimetry_protocol({**GRAVIMETRY_SETTINGS, "gravimetric_replicates": 1})
        three = build_gravimetry_protocol({**GRAVIMETRY_SETTINGS, "gravimetric_replicates": 3})

        self.assertEqual(len(one), 6)
        self.assertEqual(len(three), 18)

    def test_gravimetry_is_reachable_through_build_protocol(self):
        self.assertEqual(
            build_protocol("Gravimetry", GRAVIMETRY_SETTINGS),
            build_gravimetry_protocol(GRAVIMETRY_SETTINGS),
        )
        self.assertIn("Gravimetry", protocol_names())
        self.assertNotIn("Gravimetry", PIPELINES)


if __name__ == "__main__":
    unittest.main()
