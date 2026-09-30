from types import SimpleNamespace
from unittest.mock import patch
import unittest

from admet.engines.acquisition.triggers import (
    StabilityTrigger,
    create_trigger,
)

def _drive(trigger, flow_fn, limit_s=4.0):
    clock = SimpleNamespace(monotonic=lambda: now)
    with patch("admet.engines.acquisition.triggers.time", clock):
        for tick in range(int(limit_s * 100) + 1):
            now = 100 + tick / 100
            if trigger.check(lambda index: flow_fn(index, tick / 100), lambda _: 0.0):
                return tick / 100
    return None


class StabilityTriggerTests(unittest.TestCase):
    def test_steady_flow_settles_after_the_window(self):
        trigger = StabilityTrigger(0, window_s=0.2, timeout_s=3.0)

        elapsed = _drive(trigger, lambda _index, _elapsed: 250.0)

        self.assertIsNotNone(elapsed)
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertFalse(trigger.timed_out)

    def test_a_flow_that_never_settles_times_out_rather_than_hanging(self):
        # what a saturated controller looks like: it never reaches the setpoint
        trigger = StabilityTrigger(0, window_s=0.2, timeout_s=0.5)

        elapsed = _drive(trigger, lambda _index, elapsed: 60 + (elapsed * 313 % 40))

        self.assertIsNotNone(elapsed, "trigger hung instead of timing out")
        self.assertTrue(trigger.timed_out)

    def test_a_drifting_flow_is_not_steady(self):
        trigger = StabilityTrigger(0, window_s=0.2, timeout_s=0.6)

        _drive(trigger, lambda _index, elapsed: 100 + elapsed * 50)

        self.assertTrue(trigger.timed_out)

    def test_reset_clears_the_history(self):
        trigger = StabilityTrigger(0, window_s=0.2, timeout_s=0.3)
        _drive(trigger, lambda _index, _elapsed: 250.0)

        trigger.reset()

        self.assertFalse(trigger.timed_out)
        self.assertFalse(trigger.check(lambda _index: 250.0, lambda _index: 0.0))

    def test_registered_with_the_trigger_factory(self):
        self.assertIsInstance(create_trigger("stability", {"sensor_index": 0}), StabilityTrigger)

    def test_progress_fills_towards_giving_up_rather_than_towards_the_window(self):
        # The old reading sat at 100% for the rest of the wait, which is what made
        # an unsettled sweep look stalled.
        with patch("admet.engines.acquisition.triggers.time") as clock:
            clock.monotonic.return_value = 100
            trigger = StabilityTrigger(0, window_s=0.05, timeout_s=4.0)
            trigger.check(lambda _: 250.0, lambda _: 0.0)
            clock.monotonic.return_value = 100.2
            trigger.check(lambda _: 250.0, lambda _: 0.0)
            self.assertAlmostEqual(trigger.progress(), 0.05)


if __name__ == "__main__":
    unittest.main()
