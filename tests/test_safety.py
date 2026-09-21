"""The safety boundary.

Three things are asserted here, and each of them is the kind that only matters
once. That a breach is caught from the measurement rather than the setpoint,
because the failure this exists for -- a line that will not flow, so the
controller pushes harder -- is invisible in what was asked for. That a trip
latches and blocks a restart, because a latch that clears itself is one nobody
finds out about. And that the rig ends at zero however the process ends,
including on the interrupt an operator reaches for when something is wrong.
"""

import io
import json
import os
import tempfile
import threading
import time
import unittest
import unittest.mock

from admet.core.runtime import read_events, read_state
from admet.core.service import Admet
from admet.engines.acquisition.safety import (
    PressureWatchdog,
    SafetyTripped,
    SafetyUnsafe,
)
from admet.workflows.operations import Refused, operation


class WatchdogTests(unittest.TestCase):
    """The watchdog on its own, with no rig behind it."""

    def setUp(self):
        self.tripped: list[str] = []
        self.watchdog = PressureWatchdog(on_trip=self.tripped.append)

    def test_nothing_happens_until_it_is_armed(self):
        # A limit that is always on is one nobody chose.
        self.watchdog.check([5000.0, 5000.0])

        self.assertFalse(self.watchdog.describe()["tripped"])
        self.assertEqual(self.tripped, [])

    def test_a_breach_trips_and_says_which_channel_and_by_how_much(self):
        self.watchdog.arm({0: 1900.0})

        self.watchdog.check([1904.2, 0.0])

        state = self.watchdog.describe()
        self.assertTrue(state["tripped"])
        self.assertIn("channel 0", state["reason"])
        self.assertIn("1904.2", state["reason"])
        self.assertIn("1900", state["reason"])
        self.assertEqual(len(self.tripped), 1)

    def test_a_reading_at_the_limit_is_not_a_breach(self):
        self.watchdog.arm({0: 1900.0})

        self.watchdog.check([1900.0])

        self.assertFalse(self.watchdog.describe()["tripped"])

    def test_it_trips_once_however_many_readings_follow(self):
        self.watchdog.arm({0: 100.0})

        for _ in range(10):
            self.watchdog.check([500.0])

        self.assertEqual(len(self.tripped), 1)

    def test_only_the_armed_channels_are_watched(self):
        self.watchdog.arm({0: 100.0})

        self.watchdog.check([50.0, 9000.0])

        self.assertFalse(self.watchdog.describe()["tripped"])

    def test_the_readings_at_the_moment_of_the_trip_are_kept(self):
        self.watchdog.arm({1: 100.0})

        self.watchdog.check([12.0, 150.0, 3.0])

        self.assertEqual(
            self.watchdog.describe()["readings"], {"0": 12.0, "1": 150.0, "2": 3.0}
        )

    def test_a_trip_cannot_be_armed_over(self):
        self.watchdog.arm({0: 100.0})
        self.watchdog.check([500.0])

        with self.assertRaises(SafetyTripped):
            self.watchdog.arm({0: 5000.0})

    def test_resetting_while_still_over_the_limit_is_refused(self):
        # Otherwise the operator is handed back a rig about to trip again, and
        # learns to treat the latch as noise.
        self.watchdog.arm({0: 100.0})
        self.watchdog.check([500.0])

        with self.assertRaises(SafetyUnsafe) as caught:
            self.watchdog.reset([500.0])

        self.assertIn("still over its limits", str(caught.exception))
        self.assertTrue(self.watchdog.describe()["tripped"])

    def test_resetting_once_it_reads_safe_clears_the_latch(self):
        self.watchdog.arm({0: 100.0})
        self.watchdog.check([500.0])

        state = self.watchdog.reset([0.0])

        self.assertFalse(state["tripped"])
        self.assertFalse(state["armed"])

    def test_resetting_when_nothing_tripped_is_harmless(self):
        self.assertFalse(self.watchdog.reset([0.0])["tripped"])

    def test_something_other_than_pressure_can_latch_a_trip(self):
        state = self.watchdog.trip("the operator pressed the button")

        self.assertTrue(state["tripped"])
        self.assertEqual(state["reason"], "the operator pressed the button")

    def test_a_second_reason_does_not_overwrite_the_first(self):
        # The first thing that went wrong is the one worth keeping.
        self.watchdog.trip("first")
        self.watchdog.trip("second")

        self.assertEqual(self.watchdog.describe()["reason"], "first")


class EmergencyStopTests(unittest.TestCase):
    def setUp(self):
        self.admet = Admet()

    def test_it_needs_nothing_to_be_true_first(self):
        # This is the call whose job is to work when other things have not.
        self.assertEqual(operation("emergency_stop").requires, ())

    def test_it_works_with_nothing_connected(self):
        stopped = self.admet.do("emergency_stop", {"stop_reason": "testing"})

        self.assertEqual(stopped["protocol"], "not running")
        self.assertTrue(stopped["safety"]["tripped"])

    def test_calling_it_twice_is_the_same_as_once(self):
        first = self.admet.do("emergency_stop", {"stop_reason": "first"})
        second = self.admet.do("emergency_stop", {"stop_reason": "second"})

        self.assertTrue(second["safety"]["tripped"])
        self.assertEqual(second["safety"]["reason"], first["safety"]["reason"])

    def test_it_stops_a_running_protocol_and_zeroes_the_channels(self):
        self.admet.do("connect_fluidics", {"simulated": True})
        self.admet.do("apply_corrections")
        self.addCleanup(self.admet.do, "disconnect_fluidics")
        self.admet.do("run_steps", {"steps": [
            {"name": "hold", "sensor_setpoints": {"0": 50.0},
             "trigger_type": "time", "trigger_params": {"duration_s": 30.0}},
        ], "tick_s": 0.05})
        time.sleep(0.3)

        stopped = self.admet.do("emergency_stop", {"stop_reason": "operator"})

        self.assertTrue(stopped["channels_zeroed"])
        self.assertEqual(stopped["protocol"], "stopped")
        self.assertFalse(self.admet.state()["running"])
        modes = {channel["mode"] for channel in self.admet.do("observe")["channels"]}
        self.assertEqual(modes, {"off"})
        self.admet.do("reset_safety")

    def test_the_reason_is_visible_through_observe(self):
        self.admet.do("emergency_stop", {"stop_reason": "outlet came off"})

        safety = self.admet.do("observe")["safety"]

        self.assertTrue(safety["tripped"])
        self.assertIn("outlet came off", safety["reason"])
        self.assertTrue(safety["at"])


class LatchTests(unittest.TestCase):
    """A tripped rig does not start flowing again by being asked nicely."""

    def setUp(self):
        self.admet = Admet()
        self.admet.do("connect_fluidics", {"simulated": True})
        self.admet.do("apply_corrections")
        self.addCleanup(self.admet.do, "disconnect_fluidics")
        self.admet.do("emergency_stop", {"stop_reason": "testing the latch"})

    def test_flow_is_refused_while_the_latch_is_set(self):
        for name, settings in (
            ("set_channel_flow", {"channel_index": 0, "channel_flow_ul_min": 10.0}),
            ("run_priming", {}),
            ("run_steps", {"steps": [{"name": "x", "trigger_type": "time",
                                      "trigger_params": {"duration_s": 1.0}}]}),
        ):
            with self.subTest(operation=name):
                with self.assertRaises(Refused) as caught:
                    self.admet.do(name, settings)
                self.assertIn("safety latch has tripped", str(caught.exception))

    def test_observe_says_the_latch_is_why(self):
        guard = self.admet.do("observe")["guards"]["safe"]

        self.assertFalse(guard["met"])
        self.assertIn("reset_safety", guard["why_not"])

    def test_stopping_and_reading_stay_available(self):
        # A latch that blocked these would leave the operator holding a tripped
        # rig with no way to deal with it.
        self.admet.do("observe")
        self.admet.do("stop_channel", {"channel_index": 0})
        self.admet.do("emergency_stop")

    def test_resetting_lets_the_rig_run_again(self):
        self.admet.do("reset_safety")

        self.assertTrue(self.admet.do("observe")["guards"]["safe"]["met"])
        self.admet.do("set_channel_flow", {"channel_index": 0, "channel_flow_ul_min": 5.0})
        self.admet.do("stop_channel", {"channel_index": 0})


class WatchdogOnTheRigTests(unittest.TestCase):
    def test_a_breach_stops_the_run_without_anyone_asking(self):
        # Armed below what the simulator reaches under load, so the trip comes
        # from a measurement rather than from the test calling anything.
        admet = Admet()
        admet.do("connect_fluidics", {"simulated": True})
        admet.do("apply_corrections")
        self.addCleanup(admet.do, "disconnect_fluidics")
        admet.arm_pressure_limits({0: 40.0})
        admet.do("run_steps", {"steps": [
            {"name": "push hard", "sensor_setpoints": {"0": 200.0},
             "trigger_type": "time", "trigger_params": {"duration_s": 20.0}},
        ], "tick_s": 0.05})

        deadline = time.monotonic() + 8
        while not admet.safety_state()["tripped"] and time.monotonic() < deadline:
            time.sleep(0.05)

        safety = admet.safety_state()
        self.assertTrue(safety["tripped"], "the watchdog never tripped")
        self.assertIn("over its 40 mbar limit", safety["reason"])
        self.assertFalse(admet.state()["running"])

    def test_the_watchdog_reads_measurements_not_setpoints(self):
        # Asking for a pressure above the limit is not itself a breach; only
        # the rig actually reaching it is.
        watchdog = PressureWatchdog(on_trip=lambda reason: None)
        watchdog.arm({0: 100.0})

        watchdog.check([10.0])

        self.assertFalse(watchdog.describe()["tripped"])


class ShutdownTests(unittest.TestCase):
    """However the process ends, the rig ends at zero."""

    def _session(self, end):
        from admet.mcp.server import serve

        runtime = tempfile.mkdtemp()
        read_fd, write_fd = os.pipe()
        stdin, stdin_w = os.fdopen(read_fd, "r"), os.fdopen(write_fd, "w")
        raised: list[str] = []

        def run():
            try:
                serve(simulated=True, runtime=runtime, stdin=stdin, stdout=io.StringIO())
            except BaseException as exc:
                raised.append(type(exc).__name__)

        with unittest.mock.patch("sys.stderr", io.StringIO()):
            server = threading.Thread(target=run, daemon=True)
            server.start()
            time.sleep(0.3)
            for name, arguments in (
                ("connect_fluidics", {}),
                ("apply_corrections", {}),
                ("set_channel_flow", {"channel_index": 0, "channel_flow_ul_min": 80.0}),
            ):
                stdin_w.write(json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }) + "\n")
                stdin_w.flush()
            time.sleep(0.6)
            end(stdin_w)
            server.join(timeout=10)
        stdin.close()
        return runtime, raised

    def test_the_rig_is_zeroed_when_stdin_closes(self):
        runtime, raised = self._session(lambda writer: writer.close())

        state = read_state(runtime)
        self.assertEqual(raised, [])
        self.assertEqual(state["runtime"]["state"], "stopped")
        self.assertEqual({c["mode"] for c in state["observation"]["channels"]}, {"off"})

    def test_why_it_stopped_is_recorded(self):
        runtime, _raised = self._session(lambda writer: writer.close())

        reasons = [
            entry["detail"].get("reason")
            for entry in read_events(runtime, limit=0)
            if entry["type"] in {"safety", "process"} and isinstance(entry["detail"], dict)
        ]

        self.assertTrue(any("stdin closed" in str(reason) for reason in reasons))

    def test_shutdown_runs_even_when_the_stream_fails(self):
        # An exception on the way out must not skip stopping the rig.
        from admet.mcp.server import serve

        runtime = tempfile.mkdtemp()

        class Exploding(io.StringIO):
            def __iter__(self):
                raise RuntimeError("the stream went away")

        with unittest.mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(RuntimeError):
                serve(simulated=True, runtime=runtime, stdin=Exploding(), stdout=io.StringIO())

        state = read_state(runtime)
        self.assertEqual(state["runtime"]["state"], "stopped")
        self.assertTrue(state["observation"]["safety"]["tripped"])

    def test_an_interrupt_stops_the_rig_too(self):
        # The key an operator reaches for when something is wrong.
        from admet.mcp.server import serve

        runtime = tempfile.mkdtemp()

        class Interrupted(io.StringIO):
            def __iter__(self):
                raise KeyboardInterrupt

        with unittest.mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                serve(simulated=True, runtime=runtime, stdin=Interrupted(), stdout=io.StringIO())

        self.assertEqual(read_state(runtime)["runtime"]["state"], "stopped")


class HardwareIsOptInTests(unittest.TestCase):
    def test_serving_is_simulated_or_live_and_never_neither(self):
        from admet.app import build_parser

        with self.assertRaises(SystemExit):
            with unittest.mock.patch("sys.stderr", io.StringIO()):
                build_parser().parse_args(["serve"])

    def test_a_simulated_server_refuses_real_hardware(self):
        from admet.mcp.server import AdmetServer, SimulationRefused

        with self.assertRaises(SimulationRefused):
            AdmetServer(simulated=True)._simulated("connect_fluidics", {"simulated": False})


if __name__ == "__main__":
    unittest.main()
