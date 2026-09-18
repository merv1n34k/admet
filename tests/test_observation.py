"""Observation: knowing what the rig is doing without taking it from anyone.

Both bounded queues used to be the only way to see current measurements and
protocol events, and both fill and then silently drop. The data queue reaches
its cap in about five seconds, the event queue in about two -- after which
confirmation prompts, completions and errors were lost. These tests are about
the state that sits beside those queues rather than inside them.
"""

import threading
import time
import unittest
from queue import Queue

from admet.engines.acquisition.fluidics.acquisition import AcquisitionThread
from admet.engines.acquisition.pipeline import (
    PipelineEngine,
    PipelineEvent,
    PipelineState,
    PipelineStep,
    StepOutcome,
    StepStatus,
)
from admet.engines.acquisition.triggers import StabilityTrigger, TimeTrigger

from test_control_pipeline_engine import FakeAcquisition, FakeChannelManager


class FakeSDK:
    """Enough of the SDK to be polled. Flow is whatever the test sets."""

    def __init__(self, flow=0.0):
        self.flow = flow

    def get_pressure(self, index):
        return 100.0 + index

    def get_sensor_value(self, index):
        return self.flow


def _thread(queue_size=5, recent=10, sensors=1):
    return AcquisitionThread(
        FakeSDK(),
        pressure_count=sensors,
        sensor_count=sensors,
        data_queue=Queue(maxsize=queue_size),
        interval_ms=1,
        recent_samples=recent,
    )


class SnapshotObservationTests(unittest.TestCase):
    def test_the_latest_reading_keeps_updating_after_the_queue_is_full(self):
        # This is the defect: past the cap, poll_once stopped enqueueing, and
        # the queue was the only place a current reading existed.
        acquisition = _thread(queue_size=3)

        for flow in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0):
            acquisition._sdk.flow = flow
            acquisition.poll_once()

        self.assertTrue(acquisition._data_queue.full())
        self.assertEqual(acquisition.latest_snapshot().flows[0], 6.0)

    def test_reading_the_latest_does_not_remove_it(self):
        acquisition = _thread()
        acquisition.poll_once()

        first = acquisition.latest_snapshot()

        self.assertIsNotNone(first)
        self.assertIs(acquisition.latest_snapshot(), first)

    def test_there_is_no_reading_before_the_first_poll(self):
        # Better than a fabricated zero, which reads as a measurement.
        self.assertIsNone(_thread().latest_snapshot())

    def test_observation_does_not_drain_the_queue(self):
        # Another consumer's data must still be there afterwards.
        acquisition = _thread(queue_size=10)
        for _ in range(4):
            acquisition.poll_once()

        acquisition.latest_snapshot()
        acquisition.recent_snapshots()

        self.assertEqual(acquisition._data_queue.qsize(), 4)

    def test_the_recent_window_is_bounded_and_oldest_first(self):
        acquisition = _thread(queue_size=100, recent=3)

        for flow in (1.0, 2.0, 3.0, 4.0, 5.0):
            acquisition._sdk.flow = flow
            acquisition.poll_once()

        window = acquisition.recent_snapshots()
        self.assertEqual([snapshot.flows[0] for snapshot in window], [3.0, 4.0, 5.0])

    def test_a_sampling_window_can_ask_for_fewer(self):
        acquisition = _thread(queue_size=100, recent=10)
        for flow in (1.0, 2.0, 3.0):
            acquisition._sdk.flow = flow
            acquisition.poll_once()

        self.assertEqual(
            [snapshot.flows[0] for snapshot in acquisition.recent_snapshots(limit=2)], [2.0, 3.0]
        )

    def test_polling_and_observing_at_once_is_safe(self):
        acquisition = _thread(queue_size=2, recent=50)
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                acquisition.poll_once()

        worker = threading.Thread(target=poll, daemon=True)
        worker.start()
        try:
            for _ in range(200):
                acquisition.latest_snapshot()
                acquisition.recent_snapshots(limit=5)
        finally:
            stop.set()
            worker.join(timeout=2)

        self.assertIsNotNone(acquisition.latest_snapshot())


class RestlessAcquisition(FakeAcquisition):
    """Flow that never holds still, so a settle can only ever time out."""

    def __init__(self):
        super().__init__()
        self._reading = 0.0

    def get_flow(self, sensor_index):
        self._reading += 10.0
        return self._reading


def _run(steps, tick_s=0.001, queue_size=0, acquisition=None):
    events: Queue[PipelineEvent] = Queue(maxsize=queue_size) if queue_size else Queue()
    engine = PipelineEngine(
        steps,
        FakeChannelManager(),
        acquisition or FakeAcquisition(),
        events,
        {0: 0},
        tick_s=tick_s,
    )
    engine.start()
    engine.join(timeout=10)
    return engine


class EventObservationTests(unittest.TestCase):
    def test_events_keep_arriving_after_the_queue_is_full(self):
        steps = [PipelineStep(f"step {i}", {0: 1.0}, TimeTrigger(0.0)) for i in range(8)]

        engine = _run(steps, queue_size=3)

        self.assertTrue(engine._event_queue.full())
        self.assertEqual(engine.latest_event().state, PipelineState.COMPLETED)
        self.assertGreater(engine.latest_event().sequence, 3)

    def test_every_event_carries_a_rising_sequence_and_two_clocks(self):
        engine = _run([PipelineStep("one", {0: 1.0}, TimeTrigger(0.0))])

        events = engine.events_after(0, limit=100)
        sequences = [event.sequence for event in events]

        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(sequences), len(set(sequences)))
        self.assertTrue(all(event.at for event in events))
        self.assertTrue(all(event.monotonic > 0 for event in events))

    def test_asking_for_what_is_new_returns_only_that(self):
        engine = _run([PipelineStep(f"s{i}", {0: 1.0}, TimeTrigger(0.0)) for i in range(3)])
        everything = engine.events_after(0, limit=500)
        midpoint = everything[len(everything) // 2].sequence

        newer = engine.events_after(midpoint, limit=500)

        self.assertTrue(all(event.sequence > midpoint for event in newer))
        self.assertEqual(newer, [e for e in everything if e.sequence > midpoint])

    def test_the_answer_is_bounded_by_the_limit(self):
        engine = _run([PipelineStep(f"s{i}", {0: 1.0}, TimeTrigger(0.0)) for i in range(6)])

        self.assertEqual(len(engine.events_after(0, limit=2)), 2)

    def test_reading_events_does_not_remove_them(self):
        engine = _run([PipelineStep("one", {0: 1.0}, TimeTrigger(0.0))], queue_size=50)
        before = engine._event_queue.qsize()

        engine.events_after(0, limit=500)
        engine.latest_event()

        self.assertEqual(engine._event_queue.qsize(), before)
        self.assertEqual(engine.events_after(0, limit=500), engine.events_after(0, limit=500))

    def test_nothing_to_read_before_the_first_event(self):
        engine = PipelineEngine([], FakeChannelManager(), None, Queue(), {})

        self.assertIsNone(engine.latest_event())
        self.assertEqual(engine.events_after(0), [])


class OutcomeTests(unittest.TestCase):
    """A step that gave up must not look like a step that succeeded."""

    def test_a_settle_that_times_out_is_not_recorded_as_completed(self):
        # The trigger fires either way. Only the outcome tells them apart.
        never_settles = StabilityTrigger(
            sensor_index=0, tolerance_ul_min=0.1, window_s=0.02, timeout_s=0.1
        )
        step = PipelineStep("settle", {0: 5.0}, never_settles)

        engine = _run([step], tick_s=0.005, acquisition=RestlessAcquisition())

        self.assertEqual(step.status, StepStatus.TIMED_OUT)
        outcomes = {event.outcome for event in engine.events_after(0, limit=500)}
        self.assertIn(StepOutcome.TIMED_OUT, outcomes)
        self.assertNotIn(StepOutcome.COMPLETED, {
            event.outcome
            for event in engine.events_after(0, limit=500)
            if event.step_name == "settle"
        })

    def test_a_settle_that_holds_still_is_recorded_as_completed(self):
        settles = StabilityTrigger(
            sensor_index=0, tolerance_ul_min=100.0, window_s=0.01, timeout_s=5.0
        )
        step = PipelineStep("settle", {0: 5.0}, settles)

        _run([step], tick_s=0.005)

        self.assertEqual(step.status, StepStatus.COMPLETED)

    def test_a_stopped_step_is_cancelled_rather_than_skipped(self):
        # Skipping is a choice about one step; stopping ends the run. Recording
        # both as skipped loses which happened.
        step = PipelineStep("long", {0: 1.0}, TimeTrigger(30.0))
        engine = PipelineEngine(
            [step], FakeChannelManager(), FakeAcquisition(), Queue(), {0: 0}, tick_s=0.005
        )
        engine.start()
        time.sleep(0.05)
        engine.stop()
        engine.join(timeout=5)

        self.assertEqual(step.status, StepStatus.CANCELLED)
        self.assertEqual(engine.latest_event().outcome, StepOutcome.CANCELLED)

    def test_a_skipped_step_says_so(self):
        step = PipelineStep("long", {0: 1.0}, TimeTrigger(30.0))
        engine = PipelineEngine(
            [step], FakeChannelManager(), FakeAcquisition(), Queue(), {0: 0}, tick_s=0.005
        )
        engine.start()
        time.sleep(0.05)
        engine.skip_step()
        engine.join(timeout=5)

        self.assertEqual(step.status, StepStatus.SKIPPED)

    def test_the_outcomes_are_the_ones_the_spec_names(self):
        self.assertEqual(
            {outcome.value for outcome in StepOutcome},
            {"running", "completed", "timed_out", "skipped", "cancelled", "error"},
        )


if __name__ == "__main__":
    unittest.main()
