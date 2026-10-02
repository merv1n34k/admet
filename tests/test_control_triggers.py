import unittest

from admet.engines.acquisition.pipeline import (
    PipelineStep,
    StepStatus,
)
from admet.engines.acquisition.triggers import (
    ConditionTrigger,
    ConfirmationTrigger,
    ThresholdTrigger,
    TimeTrigger,
    VolumeTrigger,
    create_trigger,
)


class VolumeModeTests(unittest.TestCase):
    """integral stops at the target; adaptive stops early by the expected tail."""

    def run_until_stop(self, trigger, flow=60.0, step_ul=0.05, flows=None):
        trigger.reset()
        volume = 0.0
        readings = iter(flows) if flows is not None else None
        while True:
            reading = next(readings, flow) if readings is not None else flow
            if trigger.check(lambda _sensor: reading, lambda _sensor: volume):
                return volume
            volume += step_ul

    def test_integral_stops_at_the_target(self):
        stopped = self.run_until_stop(VolumeTrigger(0, 10.0, mode="integral", tail_s=0.7))

        self.assertAlmostEqual(stopped, 10.0, delta=0.05)

    def test_adaptive_stops_early_by_the_expected_tail(self):
        # 0.7 s of flow at 60 uL/min still arrives after the stop: 0.7 uL.
        stopped = self.run_until_stop(VolumeTrigger(0, 10.0, mode="adaptive", tail_s=0.7))

        self.assertAlmostEqual(stopped, 10.0 - 0.7, delta=0.05)

    def test_adaptive_uses_the_learned_tail_when_none_is_fixed(self):
        trigger = VolumeTrigger(0, 10.0)
        trigger.tail_source = lambda: 0.7

        self.assertAlmostEqual(self.run_until_stop(trigger), 9.3, delta=0.05)
        self.assertEqual(trigger.tail_used_s, 0.7)

    def test_adaptive_without_a_tail_behaves_as_integral(self):
        # The first run on a channel: nothing learned yet.
        self.assertAlmostEqual(self.run_until_stop(VolumeTrigger(0, 10.0)), 10.0, delta=0.05)

    def test_adaptive_is_the_default(self):
        self.assertEqual(VolumeTrigger(0, 10.0).mode, "adaptive")

    def test_one_misread_does_not_stop_the_step_early(self):
        # A 5000 uL/min misread would predict a huge tail; the median ignores it.
        flows = [60.0] * 50 + [5000.0] + [60.0] * 500
        stopped = self.run_until_stop(VolumeTrigger(0, 10.0, tail_s=0.7), flows=flows)

        self.assertAlmostEqual(stopped, 9.3, delta=0.05)

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            VolumeTrigger(0, 10.0, mode="setpoints")


class TailTrackerTests(unittest.TestCase):
    def test_k_is_the_median_of_recent_stops(self):
        from admet.engines.acquisition.engine import TailTracker

        tracker = TailTracker(keep=3)
        self.assertIsNone(tracker.k(1))
        for k in (0.5, 0.9, 0.7, 5.0):
            tracker.record(1, k)

        self.assertEqual(tracker.k(1), 0.9)          # 0.9, 0.7, 5.0 kept
        self.assertEqual(tracker.describe(1)["tail_samples"], 3)


class TriggerBehaviorTests(unittest.TestCase):
    def test_time_volume_and_threshold_triggers_progress_to_completion(self):
        time_trigger = TimeTrigger(duration_s=0.0)
        time_trigger.reset()
        self.assertTrue(time_trigger.check(lambda _: 0.0, lambda _: 0.0))
        self.assertEqual(time_trigger.progress(), 1.0)
        self.assertIn("0", time_trigger.description())

        volume_trigger = VolumeTrigger(sensor_index=0, target_volume_ul=100.0)
        volume_trigger.reset()
        self.assertFalse(volume_trigger.check(lambda _: 0.0, lambda _: 10.0))
        self.assertTrue(volume_trigger.check(lambda _: 0.0, lambda _: 110.0))
        self.assertAlmostEqual(volume_trigger.progress(), 1.0)
        self.assertIn("sensor 0", volume_trigger.description())

        threshold_trigger = ThresholdTrigger(
            sensor_index=0,
            target=100.0,
            tolerance_pct=5.0,
            stable_duration_s=0.0,
        )
        threshold_trigger.reset()
        self.assertFalse(threshold_trigger.check(lambda _: 0.0, lambda _: 0.0))
        self.assertTrue(threshold_trigger.check(lambda _: 100.0, lambda _: 0.0))
        self.assertEqual(threshold_trigger.progress(), 1.0)

    def test_condition_trigger_handles_bounds_and_latches(self):
        trigger = ConditionTrigger(sensor_index=0, min_value=10.0, max_value=50.0)
        trigger.reset()

        self.assertFalse(trigger.check(lambda _: 5.0, lambda _: 0.0))
        self.assertFalse(trigger.check(lambda _: 60.0, lambda _: 0.0))
        self.assertTrue(trigger.check(lambda _: 30.0, lambda _: 0.0))
        self.assertTrue(trigger.check(lambda _: 5.0, lambda _: 0.0))
        self.assertEqual(trigger.progress(), 1.0)

    def test_confirmation_trigger_blocks_until_confirmed_and_resets(self):
        trigger = ConfirmationTrigger(message="Proceed?")
        trigger.reset()

        self.assertEqual(trigger.message, "Proceed?")
        self.assertFalse(trigger.check(lambda _: 0.0, lambda _: 0.0))
        trigger.confirm()
        self.assertTrue(trigger.check(lambda _: 0.0, lambda _: 0.0))
        self.assertEqual(trigger.progress(), 1.0)
        trigger.reset()
        self.assertFalse(trigger.check(lambda _: 0.0, lambda _: 0.0))

    def test_create_trigger_maps_protocol_specs(self):
        specs = (
            ("time", {"duration_s": 5.0}, TimeTrigger),
            ("volume", {"sensor_index": 0, "target_volume_ul": 10.0}, VolumeTrigger),
            ("threshold", {"sensor_index": 0, "target": 100.0}, ThresholdTrigger),
            ("condition", {"sensor_index": 0, "min_value": 50.0}, ConditionTrigger),
            ("confirmation", {"message": "OK?"}, ConfirmationTrigger),
        )

        for trigger_type, params, expected_type in specs:
            with self.subTest(trigger_type=trigger_type):
                self.assertIsInstance(create_trigger(trigger_type, params), expected_type)
        with self.assertRaises(ValueError):
            create_trigger("invalid", {})

    def test_pipeline_step_defaults_match_engine_contract(self):
        step = PipelineStep("run_priming", {0: 10.0}, TimeTrigger(0.0))

        self.assertEqual(step.status, StepStatus.PENDING)
        self.assertEqual(step.on_complete, "hold")
        self.assertEqual(step.confirm_message, "")
        self.assertEqual(step.error_msg, "")


if __name__ == "__main__":
    unittest.main()
