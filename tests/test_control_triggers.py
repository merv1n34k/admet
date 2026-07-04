import unittest

from admet.engines.acquisition.protocol import (
    ConditionTrigger,
    ConfirmationTrigger,
    PipelineStep,
    StepStatus,
    ThresholdTrigger,
    TimeTrigger,
    VolumeTrigger,
    create_trigger,
)


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
        step = PipelineStep("prime", {0: 10.0}, TimeTrigger(0.0))

        self.assertEqual(step.status, StepStatus.PENDING)
        self.assertEqual(step.on_complete, "hold")
        self.assertEqual(step.confirm_message, "")
        self.assertEqual(step.error_msg, "")


if __name__ == "__main__":
    unittest.main()
