import threading
import time
import unittest
import io
from unittest.mock import Mock, patch

from admet.core.control_session import ControlSession
from admet.core.control import control, _interactive_frame


class FakeClient:
    def __init__(self, _directory):
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.closed = False

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        self.release.wait(2)
        return {"reason": "step_completed"}

    def close(self):
        self.closed = True
        self.release.set()


def drain(session):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = session.poll()
        if result is not None:
            return result
        time.sleep(0.001)
    raise AssertionError("no asynchronous result")


class ControlSessionTests(unittest.TestCase):
    def setUp(self):
        self.session = ControlSession("unused", FakeClient)
        self.addCleanup(self.session.close)
        drain(self.session)

    def test_execute_requires_displayed_plan_and_sends_only_id(self):
        session = self.session
        session.plan = {"plan_id": "plan_one", "state": "planned"}
        self.assertFalse(session.execute())
        session.reviewed_id = "plan_one"
        self.assertTrue(session.execute())
        drain(session)
        name, arguments = session.client.calls[-1]
        self.assertEqual(name, "control_protocol")
        self.assertEqual(arguments, {"action": "execute", "plan_id": "plan_one", "timeout_s": 0.1})

    def test_blocked_request_does_not_block_ui_or_detachment(self):
        session = self.session
        session.client.release.clear()
        begin = time.monotonic()
        self.assertTrue(session.action("confirm"))
        self.assertLess(time.monotonic() - begin, 0.1)
        self.assertFalse(session.action("confirm"))
        session.close()
        self.assertTrue(session.client.closed)

    def test_switching_plan_requires_another_review(self):
        session = self.session
        session.plan = {"plan_id": "a", "state": "planned"}
        session.reviewed_id = "a"
        session.next_plan({"planned_protocols": [session.plan, {"plan_id": "b", "state": "planned"}]})
        self.assertEqual(session.plan["plan_id"], "b")
        self.assertIsNone(session.reviewed_id)
        self.assertFalse(session.execute())

    def test_review_frame_displays_exact_plan_and_grants_execute_id(self):
        session = self.session
        session.plan = {
            "plan_id": "one", "state": "planned", "steps": [], "step_count": 0,
        }
        with patch("admet.core.control.read_state", return_value={}):
            frame = _interactive_frame("unused", False, 90, session, "review")
        self.assertIn("REVIEW PLAN", frame)
        self.assertIn("one", frame)
        self.assertEqual(session.reviewed_id, "one")


class ControlKeyboardTests(unittest.TestCase):
    def test_actions_and_emergency_keys_work_without_network_waits(self):
        session = Mock()
        session.notice = "Attached"
        session.poll.return_value = None
        session.review.return_value = True
        keys = iter(["v", "r", "y", "s", "p", "a", "c", "L", "E", "X", "escape", "q"])
        with (
            patch("admet.core.control.ControlSession", return_value=session),
            patch("admet.core.control._control_keys.__enter__", return_value=lambda _wait: next(keys)),
            patch("admet.core.control._control_keys.__exit__"),
            patch("admet.core.control._interactive_frame", return_value="header\nbody\nfooter"),
            patch("admet.core.control.read_state", return_value={
                "observation": {"protocol": {"state": "paused"}},
            }),
            patch("admet.core.control._signal_owner", return_value="requested") as signal_owner,
        ):
            control("unused", stream=io.StringIO())
        session.execute.assert_called_once()
        self.assertEqual([call.args[0] for call in session.action.call_args_list],
                         ["confirm", "skip", "resume", "abort"])
        self.assertEqual(signal_owner.call_count, 2)
        session.close.assert_called_once()
