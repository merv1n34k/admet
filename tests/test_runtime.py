"""Runtime telemetry: one owner, whole files, and a history that only grows.

The runtime directory is how a human in a second terminal sees the process that
owns the instrument. It is one-way on purpose -- nothing read from here can
command anything -- so these tests are about the three guarantees a reader
depends on: that only one process writes, that state.json is never half a
picture, and that events.jsonl records changes rather than ticks.
"""

import io
import json
import os
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from admet.core.runtime import (
    RuntimeBusy,
    RuntimeOwner,
    RuntimePublisher,
    heartbeat_age_s,
    read_events,
    read_state,
)


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())

    def test_the_first_process_takes_the_runtime(self):
        with RuntimeOwner(self.directory) as owner:
            self.assertTrue(owner.path.is_file())

    def test_a_second_process_is_refused_and_told_who_holds_it(self):
        # This is the whole point: being refused here is cheap, whereas two
        # processes discovering the conflict by opening one instrument is not.
        with RuntimeOwner(self.directory):
            with self.assertRaises(RuntimeBusy) as caught:
                RuntimeOwner(self.directory).acquire()

        self.assertIn(str(os.getpid()), str(caught.exception))
        self.assertIn("one process owns the instrument", str(caught.exception))

    def test_the_claim_records_who_and_when(self):
        with RuntimeOwner(self.directory) as owner:
            claim = json.loads(owner.path.read_text())

        self.assertEqual(claim["pid"], os.getpid())
        self.assertTrue(claim["started_at"])

    def test_releasing_lets_the_next_process_in(self):
        first = RuntimeOwner(self.directory)
        first.acquire()
        first.release()

        with RuntimeOwner(self.directory):
            pass

    def test_a_lock_file_left_by_a_crash_does_not_block_anyone(self):
        # An exclusively-created file would: nobody is there to delete it. The
        # flock is released by the kernel when the process dies, so the file
        # being present means nothing on its own.
        (self.directory / "owner.lock").write_text(json.dumps({"pid": 999999}))

        with RuntimeOwner(self.directory) as owner:
            self.assertEqual(json.loads(owner.path.read_text())["pid"], os.getpid())


def _publisher(directory, observation, events=None, **kwargs):
    return RuntimePublisher(
        directory,
        observe=lambda: dict(observation),
        events_since=lambda sequence: [e for e in (events or []) if e["sequence"] > sequence],
        mode="simulated",
        **kwargs,
    )


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.observation = {
            "project": {"open": False, "path": None},
            "connection": {"fluidics": False, "simulated": True},
            "polling": False,
            "recording": {"active": False},
            "safety": {"armed": False, "tripped": False},
            "validation": {"state": "idle"},
            "channels": [],
        }

    def test_the_header_says_who_is_serving_and_how(self):
        publisher = _publisher(self.directory, self.observation, pid=4242)
        publisher.start()
        self.addCleanup(publisher.stop)

        header = read_state(self.directory)["runtime"]

        self.assertEqual(header["pid"], 4242)
        self.assertEqual(header["mode"], "simulated")
        self.assertEqual(header["state"], "running")
        self.assertTrue(header["started_at"])
        self.assertTrue(header["heartbeat"])

    def test_what_was_observed_reaches_the_file(self):
        publisher = _publisher(self.directory, self.observation)
        publisher.publish_once()

        self.assertEqual(
            read_state(self.directory)["observation"]["connection"]["simulated"], True
        )

    def test_the_state_file_is_replaced_whole(self):
        # Read while it is being rewritten, repeatedly. Every read must be a
        # complete document or nothing at all -- never half of one.
        publisher = _publisher(self.directory, self.observation, interval_s=0.005)
        publisher.start()
        self.addCleanup(publisher.stop)

        for _ in range(300):
            state = read_state(self.directory)
            if state is not None:
                self.assertIn("runtime", state)
                self.assertIn("observation", state)

    def test_no_temporary_file_is_left_behind(self):
        publisher = _publisher(self.directory, self.observation)
        publisher.publish_once()

        self.assertEqual([p.name for p in self.directory.glob(".*tmp")], [])

    def test_the_heartbeat_moves(self):
        publisher = _publisher(self.directory, self.observation)
        publisher.publish_once()
        first = read_state(self.directory)["runtime"]["heartbeat"]
        time.sleep(0.01)
        publisher.publish_once()

        self.assertNotEqual(read_state(self.directory)["runtime"]["heartbeat"], first)
        self.assertLess(heartbeat_age_s(read_state(self.directory)), 5.0)

    def test_a_stopped_publisher_says_so_rather_than_going_quiet(self):
        # Left saying running, a reader could only infer the truth from a stale
        # heartbeat, which is guessing.
        publisher = _publisher(self.directory, self.observation)
        publisher.start()

        publisher.stop(reason="server shut down")

        header = read_state(self.directory)["runtime"]
        self.assertEqual(header["state"], "stopped")
        self.assertFalse(publisher.describe()["publishing"])


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.observation = {
            "project": {"open": False, "path": None},
            "connection": {"fluidics": False, "simulated": True},
            "polling": False,
            "recording": {"active": False},
            "safety": {"armed": False, "tripped": False},
            "validation": {"state": "idle"},
        }

    def test_nothing_is_appended_while_nothing_changes(self):
        publisher = _publisher(self.directory, self.observation)
        publisher.publish_once()
        settled = len(read_events(self.directory, limit=0))

        for _ in range(10):
            publisher.publish_once()

        self.assertEqual(len(read_events(self.directory, limit=0)), settled)

    def test_a_change_is_recorded_once(self):
        publisher = _publisher(self.directory, self.observation)
        publisher.publish_once()
        before = len(read_events(self.directory, limit=0))

        self.observation["connection"] = {"fluidics": True, "simulated": True}
        publisher.publish_once()
        publisher.publish_once()
        publisher.publish_once()

        entries = read_events(self.directory, limit=0)
        self.assertEqual(len(entries) - before, 1)
        self.assertEqual(entries[-1]["type"], "connection")
        self.assertTrue(entries[-1]["detail"]["fluidics"])

    def test_protocol_events_are_published_once_each(self):
        events = [
            {"sequence": 1, "outcome": "running", "step_name": "one"},
            {"sequence": 2, "outcome": "completed", "step_name": "one"},
        ]
        publisher = _publisher(self.directory, self.observation, events=events)

        publisher.publish_once()
        publisher.publish_once()

        published = [e for e in read_events(self.directory, limit=0) if e["type"] == "protocol"]
        self.assertEqual([e["detail"]["sequence"] for e in published], [1, 2])

    def test_every_entry_is_sequenced_and_stamped(self):
        publisher = _publisher(self.directory, self.observation)
        publisher.publish_once()

        entries = read_events(self.directory, limit=0)
        self.assertEqual([e["seq"] for e in entries], sorted(e["seq"] for e in entries))
        self.assertTrue(all(entry["at"] for entry in entries))

    def test_the_history_only_grows(self):
        publisher = _publisher(self.directory, self.observation)
        publisher.publish_once()
        first = read_events(self.directory, limit=0)

        self.observation["polling"] = True
        publisher.publish_once()

        self.assertEqual(read_events(self.directory, limit=0)[: len(first)], first)


class ReaderTests(unittest.TestCase):
    def test_nothing_published_yet_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(read_state(tmp))
            self.assertEqual(read_events(tmp), [])
            self.assertIsNone(heartbeat_age_s(None))

    def test_a_partial_last_line_does_not_lose_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(json.dumps({"seq": 1, "at": "now", "type": "process"}) + "\n{ half")

            self.assertEqual(len(read_events(tmp)), 1)


class ServeTests(unittest.TestCase):
    """Runtime-backed serve delegates to the detachable relay."""

    def test_a_runtime_session_uses_the_detachable_relay(self):
        from admet.mcp import server as mcp_server

        directory = Path(tempfile.mkdtemp())
        stdin = io.StringIO("")
        output = io.StringIO()
        with unittest.mock.patch.object(mcp_server, "_serve_relay", return_value=0) as relay:
            result = mcp_server.serve(
                simulated=True, runtime=str(directory), stdin=stdin, stdout=output
            )

        self.assertEqual(result, 0)
        relay.assert_called_once()
        self.assertIs(relay.call_args.kwargs["source"], stdin)
        self.assertIs(relay.call_args.kwargs["sink"], output)

    def test_the_runtime_lock_is_released_for_the_next_owner(self):
        directory = Path(tempfile.mkdtemp())
        with RuntimeOwner(directory):
            pass

        with RuntimeOwner(directory):
            pass

    def test_serving_without_a_runtime_publishes_nothing(self):
        from admet.mcp.server import serve

        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch("sys.stderr", io.StringIO()):
                serve(simulated=True, stdin=io.StringIO(""), stdout=io.StringIO())

            self.assertIsNone(read_state(tmp))


if __name__ == "__main__":
    unittest.main()
