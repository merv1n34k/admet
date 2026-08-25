import time
import unittest
from queue import Queue

from admet.engines.acquisition.protocol import (
    PipelineEngine,
    PipelineEvent,
    PipelineState,
    PipelineStep,
    StepStatus,
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
            "prime",
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
