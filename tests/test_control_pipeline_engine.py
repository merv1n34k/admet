import time
import unittest
from queue import Queue

from admet.engines.acquisition.pipeline import (
    PipelineEngine,
    PipelineEvent,
    PipelineState,
    PipelineStep,
    StepStatus,
)
from admet.engines.acquisition.triggers import (
    BoundedTrigger,
    ConfirmationTrigger,
    StabilityTrigger,
    TimeTrigger,
)


class FakeChannelManager:
    def __init__(self):
        self.calls = []

    def pipeline_zero_all(self):
        self.calls.append(("zero_all",))

    def pipeline_resume_all(self):
        self.calls.append(("resume_all",))

    def pipeline_release_all(self):
        self.calls.append(("release_all",))

    def pipeline_set_setpoint(self, channel_index, value):
        self.calls.append(("set", channel_index, value))

    def pipeline_set_pressure(self, channel_index, pressure_mbar):
        self.calls.append(("pressure", channel_index, pressure_mbar))

    def pipeline_release_channel(self, channel_index):
        self.calls.append(("release", channel_index))


class FakeAcquisition:
    def __init__(self):
        self.volumes = {0: 0.0}
        self.flows = {0: 0.0}

    def get_volume(self, sensor_index):
        return self.volumes.get(sensor_index, 0.0)

    def get_flow(self, sensor_index):
        return self.flows.get(sensor_index, 0.0)


class PipelineEngineTests(unittest.TestCase):
    def test_completes_step_and_applies_zero(self):
        manager = FakeChannelManager()
        events: Queue[PipelineEvent] = Queue()
        step = PipelineStep(
            "run_priming",
            {0: 5.0},
            TimeTrigger(0.0),
            on_complete="zero",
        )
        engine = PipelineEngine(
            [step],
            manager,
            FakeAcquisition(),
            events,
            {0: 2},
            tick_s=0.001,
        )

        engine.start()
        engine.join(timeout=1.0)

        self.assertFalse(engine.is_alive())
        self.assertEqual(engine.state, PipelineState.COMPLETED)
        self.assertEqual(step.status, StepStatus.COMPLETED)
        self.assertIn(("set", 2, 5.0), manager.calls)
        self.assertIn(("set", 2, 0.0), manager.calls)
        self.assertEqual(manager.calls[-1], ("release_all",))
        self.assertEqual(_last_event(events).state, PipelineState.COMPLETED)

    def test_a_stopped_pipeline_does_not_stay_stopping(self):
        # Stopping is what it is doing, not where it ends up. Left as the final
        # state it reads as still running, and every gated action stays shut --
        # which is how a finished stage became impossible to leave.
        manager = FakeChannelManager()
        events: Queue[PipelineEvent] = Queue()
        steps = [PipelineStep(f"hold {index}", {0: 5.0}, TimeTrigger(5.0)) for index in range(3)]
        engine = PipelineEngine([*steps], manager, FakeAcquisition(), events, {0: 2}, tick_s=0.001)

        engine.start()
        time.sleep(0.05)
        engine.stop()
        engine.join(timeout=2.0)

        self.assertFalse(engine.is_alive())
        self.assertEqual(engine.state, PipelineState.IDLE)
        self.assertNotEqual(engine.state, PipelineState.STOPPING)
        self.assertEqual(manager.calls[-1], ("release_all",))

    def test_confirmation_step_can_be_skipped(self):
        manager = FakeChannelManager()
        events: Queue[PipelineEvent] = Queue()
        step = PipelineStep(
            "operator check",
            {0: 1.0},
            TimeTrigger(0.0),
            confirm_message="Proceed?",
        )
        engine = PipelineEngine(
            [step],
            manager,
            FakeAcquisition(),
            events,
            {0: 0},
            tick_s=0.001,
        )

        engine.start()
        self.assertTrue(_wait_for_confirmation(events, "Proceed?"))
        engine.skip_step()
        engine.join(timeout=1.0)

        self.assertFalse(engine.is_alive())
        self.assertEqual(engine.state, PipelineState.COMPLETED)
        self.assertEqual(step.status, StepStatus.SKIPPED)
        self.assertNotIn(("set", 0, 1.0), manager.calls)
        self.assertIn(("zero_all",), manager.calls)
        self.assertEqual(manager.calls[-1], ("release_all",))

    def test_pause_and_abort_zero_immediately_at_a_confirmation_gate(self):
        for action in ("pause", "stop"):
            with self.subTest(action=action):
                manager = FakeChannelManager()
                events: Queue[PipelineEvent] = Queue()
                step = PipelineStep(
                    "operator check", {}, TimeTrigger(0.0), confirm_message="Proceed?"
                )
                engine = PipelineEngine(
                    [step], manager, FakeAcquisition(), events, {0: 0}, tick_s=0.001
                )
                engine.start()
                self.assertTrue(_wait_for_confirmation(events, "Proceed?"))

                getattr(engine, action)()

                self.assertIn(("zero_all",), manager.calls)
                engine.stop()
                engine.join(timeout=1.0)

    def test_proceed_while_paused_cannot_apply_setpoints_until_resumed(self):
        manager = FakeChannelManager()
        events: Queue[PipelineEvent] = Queue()
        step = PipelineStep(
            "operator check", {0: 5.0}, TimeTrigger(0.0), confirm_message="Proceed?"
        )
        engine = PipelineEngine(
            [step], manager, FakeAcquisition(), events, {0: 0}, tick_s=0.001
        )
        engine.start()
        self.assertTrue(_wait_for_confirmation(events, "Proceed?"))

        engine.pause()
        engine.confirm_pending()
        time.sleep(0.02)
        self.assertNotIn(("set", 0, 5.0), manager.calls)

        engine.resume()
        engine.join(timeout=1.0)
        self.assertIn(("set", 0, 5.0), manager.calls)

    def test_resume_puts_the_step_flow_back(self):
        manager = FakeChannelManager()
        events: Queue[PipelineEvent] = Queue()
        step = PipelineStep("dose", {0: 5.0}, TimeTrigger(0.3), on_complete="zero")
        engine = PipelineEngine([step], manager, FakeAcquisition(), events, {0: 2}, tick_s=0.001)
        engine.start()
        time.sleep(0.05)

        engine.pause()
        engine.resume()
        engine.join(timeout=2.0)

        sets = [call for call in manager.calls if call[0] == "set"]
        self.assertEqual(sets, [("set", 2, 5.0), ("set", 2, 5.0), ("set", 2, 0.0)])
        self.assertEqual(step.status, StepStatus.COMPLETED)

    def test_a_pause_does_not_count_against_the_step(self):
        manager = FakeChannelManager()
        events: Queue[PipelineEvent] = Queue()
        step = PipelineStep("timed", {0: 5.0}, BoundedTrigger(TimeTrigger(0.2), 0.4), on_complete="zero")
        engine = PipelineEngine([step], manager, FakeAcquisition(), events, {0: 2}, tick_s=0.001)
        started = time.monotonic()
        engine.start()
        time.sleep(0.05)

        engine.pause()
        time.sleep(0.5)                                   # longer than the step's whole limit
        engine.resume()
        engine.join(timeout=2.0)

        self.assertEqual(engine.state, PipelineState.COMPLETED)
        self.assertEqual(step.status, StepStatus.COMPLETED)
        self.assertGreaterEqual(time.monotonic() - started, 0.7)   # 0.2 s of flow plus the pause

    def test_a_stability_step_that_gives_up_is_recorded_not_fatal(self):
        manager = FakeChannelManager()
        events: Queue[PipelineEvent] = Queue()
        acquisition = FakeAcquisition()
        trigger = BoundedTrigger(StabilityTrigger(0, window_s=1.0, timeout_s=0.05), 5.0)
        step = PipelineStep("settle", {0: 5.0}, trigger)
        engine = PipelineEngine([step], manager, acquisition, events, {0: 2}, tick_s=0.001)
        engine.start()
        engine.join(timeout=2.0)

        self.assertEqual(engine.state, PipelineState.COMPLETED)
        self.assertEqual(step.status, StepStatus.TIMED_OUT)

    def test_confirmation_trigger_needs_exactly_one_operator_answer(self):
        manager = FakeChannelManager()
        events: Queue[PipelineEvent] = Queue()
        step = PipelineStep(
            "operator check",
            {},
            ConfirmationTrigger("Check the mapping"),
            confirm_message="Check the mapping",
            on_complete="zero",
        )
        engine = PipelineEngine(
            [step], manager, FakeAcquisition(), events, {0: 0}, tick_s=0.001
        )

        engine.start()
        self.assertTrue(_wait_for_confirmation(events, "Check the mapping"))
        engine.confirm_pending()
        engine.join(timeout=1.0)

        self.assertFalse(engine.is_alive())
        self.assertEqual(engine.state, PipelineState.COMPLETED)
        self.assertEqual(step.status, StepStatus.COMPLETED)
        self.assertEqual(manager.calls[-1], ("release_all",))

    def test_pressure_step_applies_and_zeros_pressure(self):
        manager = FakeChannelManager()
        events: Queue[PipelineEvent] = Queue()
        step = PipelineStep(
            "pressure wash",
            {},
            TimeTrigger(0.0),
            pressure_setpoints={0: 2000.0, 1: 2000.0},
            on_complete="zero",
        )
        engine = PipelineEngine(
            [step],
            manager,
            FakeAcquisition(),
            events,
            {},
            tick_s=0.001,
        )

        engine.start()
        engine.join(timeout=1.0)

        self.assertIn(("pressure", 0, 2000.0), manager.calls)
        self.assertIn(("pressure", 1, 2000.0), manager.calls)
        self.assertIn(("pressure", 0, 0.0), manager.calls)
        self.assertIn(("pressure", 1, 0.0), manager.calls)
        self.assertEqual(engine.state, PipelineState.COMPLETED)


def _last_event(events: Queue[PipelineEvent]) -> PipelineEvent:
    last = None
    while not events.empty():
        last = events.get()
    if last is None:
        raise AssertionError("no events emitted")
    return last


def _wait_for_confirmation(events: Queue[PipelineEvent], message: str) -> bool:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        while not events.empty():
            event = events.get()
            if event.confirmation_message == message:
                return True
        time.sleep(0.001)
    return False


if __name__ == "__main__":
    unittest.main()
