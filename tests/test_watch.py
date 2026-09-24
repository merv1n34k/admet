"""The telemetry monitor and its owner lifecycle controls.

The monitor never constructs an engine or becomes a second instrument owner.
Emergency and shutdown keys send signals only to a fresh published owner PID.
It also cannot mislead: stale screens are announced and absent readings are not
drawn as zero.

The renderer is a pure function of the file contents, so the frame a person
would see is asserted against directly.
"""

import io
import json
import signal
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from admet.core.watch import (
    RULE,
    STALE_AFTER_S,
    _fit_frame,
    _signal_owner,
    _watch_keys,
    render,
    watch,
)


def _state(**overrides):
    state = {
        "runtime": {
            "pid": 4242,
            "mode": "simulated",
            "state": "running",
            "started_at": "2026-09-21T12:00:00.000+03:00",
            "heartbeat": "2026-09-21T12:00:00.000+03:00",
            "project": "/runs/oil.admetp",
        },
        "observation": {
            "project": {"open": True, "path": "/runs/oil.admetp"},
            "connection": {"fluidics": True, "simulated": True},
            "polling": True,
            "recording": {"active": False, "id": None, "fluidics_csv": None},
            "safety": {"armed": False, "tripped": False, "reason": "", "limits": {}},
            "validation": {"active": False, "id": None},
            "protocol": {"state": "idle", "step_index": None, "total_steps": None,
                         "step_name": "", "progress": None, "outcome": "",
                         "confirmation_message": "", "error": ""},
            "channels": [{
                "index": 0, "label": "Oil L", "mode": "flow",
                "requested_flow_ul_min": 100.0, "requested_pressure_mbar": None,
                "pressure_mbar": 742.3, "flow_ul_min": 98.4, "volume_ul": 21.6,
                "stable": True, "pressure_mean_mbar": 741.8, "pressure_std_mbar": 2.1,
                "flow_mean_ul_min": 98.2, "flow_std_ul_min": 1.4,
            }],
            "fluidics_configuration": {
                "corrections_applied": True,
                "channels": [{
                    "index": 0, "key": "oil_l", "label": "Oil L", "flow_unit": "L",
                    "corrections_applied": True, "calibration": "IPA",
                    "scale": 2.25, "offset": 0.1, "quadratic": 0.002,
                }],
            },
        },
    }
    for section, values in overrides.items():
        state["observation"][section] = values
    return state


class SafetyOfTheMonitorTests(unittest.TestCase):

    def test_it_imports_nothing_that_could_touch_the_instrument(self):
        # Checked against the import graph rather than the text, so a display
        # string saying "connected" is not mistaken for reaching a device.
        import ast

        import admet.core.watch

        tree = ast.parse(Path(admet.core.watch.__file__).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        self.assertEqual(
            {name for name in imported if name.startswith("admet")},
            {"admet.core.runtime"},
        )
        self.assertFalse({name for name in imported if "engine" in name or "fluigent" in name})

    def test_it_holds_no_service_and_no_engine(self):
        import admet.core.watch

        source = Path(admet.core.watch.__file__).read_text()

        self.assertNotIn("Admet(", source)
        self.assertNotIn("engine_action", source)
        self.assertNotIn("do(", source)

    def test_interactive_footer_offers_exact_lifecycle_controls(self):
        frame = render(_state(), interactive=True)

        self.assertIn("[q] QUIT", frame)
        self.assertIn("[↑↓/Pg] SCROLL", frame)
        self.assertIn("[E] E-STOP", frame)
        self.assertIn("[X] KILL", frame)
        self.assertIn("PHYSICAL E-STOP WINS", frame)

    def test_emergency_and_kill_signal_only_a_fresh_published_owner(self):
        from datetime import datetime

        state = _state()
        state["runtime"]["heartbeat"] = datetime.now().astimezone().isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "state.json").write_text(json.dumps(state))
            with unittest.mock.patch("admet.core.watch.os.kill") as kill:
                emergency = _signal_owner(tmp, signal.SIGUSR1, "emergency")
                shutdown = _signal_owner(tmp, signal.SIGTERM, "shutdown")

        self.assertEqual(emergency, "emergency")
        self.assertEqual(shutdown, "shutdown")
        self.assertEqual(
            kill.call_args_list,
            [unittest.mock.call(4242, signal.SIGUSR1), unittest.mock.call(4242, signal.SIGTERM)],
        )

    def test_a_stale_runtime_pid_is_never_signalled(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "state.json").write_text(json.dumps(_state()))
            with unittest.mock.patch("admet.core.watch.os.kill") as kill:
                result = _signal_owner(tmp, signal.SIGTERM, "shutdown")

        self.assertIn("REFUSED", result)
        kill.assert_not_called()


class HonestyTests(unittest.TestCase):
    def test_an_absent_reading_is_a_dash_rather_than_a_zero(self):
        # A zero here would be indistinguishable from a channel sitting still.
        frame = render(_state(
            channels=[{
                "index": 0, "label": "Oil L", "mode": "off",
                "requested_flow_ul_min": None, "requested_pressure_mbar": None,
                "pressure_mbar": None, "flow_ul_min": None, "volume_ul": None,
                "stable": None, "pressure_std_mbar": None, "flow_std_ul_min": None,
            }],
            fluidics_configuration={"corrections_applied": False, "channels": []},
        ))

        self.assertIn("—", frame)
        self.assertNotIn("0.00", frame)

    def test_a_stale_heartbeat_is_announced_not_merely_old(self):
        stale = _state()
        stale["runtime"]["heartbeat"] = "2020-01-01T00:00:00.000+03:00"

        frame = render(stale)

        self.assertIn("STALE", frame)
        self.assertIn("not current", frame)

    def test_a_fresh_heartbeat_is_not_flagged(self):
        from datetime import datetime

        fresh = _state()
        fresh["runtime"]["heartbeat"] = datetime.now().astimezone().isoformat(
            timespec="milliseconds"
        )

        self.assertNotIn("STALE", render(fresh))

    def test_the_staleness_threshold_is_about_three_seconds(self):
        self.assertAlmostEqual(STALE_AFTER_S, 3.0)

    def test_nothing_publishing_says_so_and_says_what_to_start(self):
        frame = render(None, directory="/tmp/rt")

        self.assertIn("Nothing is publishing", frame)
        self.assertIn("serve --simulated", frame)

    def test_a_disconnected_rig_shows_no_channels_rather_than_empty_rows(self):
        frame = render(_state(channels=[], connection={"fluidics": False, "simulated": True}))

        self.assertIn("no channels", frame)


class ContentTests(unittest.TestCase):
    def test_fluid_configuration_and_corrections_are_visible(self):
        frame = render(_state())

        self.assertIn("FLUID CONFIGURATION", frame)
        self.assertIn("corrections APPLIED", frame)
        self.assertIn("oil_l", frame)
        self.assertIn("IPA", frame)
        self.assertIn("2.25", frame)
        self.assertIn("0.002", frame)

    def test_the_mode_is_unmissable(self):
        self.assertIn("SIMULATED", render(_state()))

        live = _state()
        live["runtime"]["mode"] = "live"
        self.assertIn("LIVE", render(live))

    def test_a_channel_row_carries_what_was_asked_and_what_was_measured(self):
        frame = render(_state())

        self.assertIn("Oil L", frame)
        self.assertIn("100.0", frame)   # requested
        self.assertIn("742.3", frame)   # measured pressure
        self.assertIn("98.40", frame)   # measured flow
        self.assertIn("21.60", frame)   # volume

    def test_rolling_spread_is_shown_beside_the_reading(self):
        self.assertIn("±2.1", render(_state()))
        self.assertIn("±1.40", render(_state()))

    def test_camera_has_a_separate_section_and_missing_values_are_dashes(self):
        frame = render(_state(camera={
            "connected": False, "live": False, "model": None, "serial_number": None,
            "frame_width": None, "frame_height": None, "pixel_format": None,
            "configured_frame_rate_hz": None, "measured_frame_rate_hz": None,
            "frame_count": None, "dropped_frame_count": None,
            "latest_frame_age_s": None, "recording": False,
        }))

        self.assertIn("CAMERA", frame)
        self.assertIn("disconnected", frame)
        self.assertIn("frames —", frame)

    def test_a_plan_and_its_complete_steps_appear_before_execution(self):
        frame = render(_state(planned_protocols=[{
            "plan_id": "plan_abc123", "operation_id": "validate_oil_capacity",
            "state": "planned", "created_at": "2026-09-21T12:00:00+03:00",
            "step_count": 1, "expected_duration_s": 30.0,
            "armed_safety_limits": {"pressure_mbar": {"0": 1850.0}},
            "required_confirmations": ["Confirm Oil-L"], "warnings": [],
            "unmet_guards": [], "digest": "0123456789abcdef",
            "steps": [{
                "number": 1, "name": "confirm oil mapping",
                "flow_setpoints_ul_min": {}, "pressure_setpoints_mbar": {},
                "trigger_type": "time", "timeout_s": None,
                "on_complete": "hold", "confirmation": "Confirm Oil-L",
            }],
        }]))

        self.assertIn("PLANNED PROTOCOLS", frame)
        self.assertIn("plan_abc123", frame)
        self.assertIn("validate_oil_capacity", frame)
        self.assertIn("TRIGGER", frame)
        self.assertIn("CONDITION", frame)
        self.assertIn("CONFIRM: Confirm Oil-L", frame)
        self.assertIn("0123456789ab", frame)

    def test_step_table_prioritises_setpoints_trigger_condition_and_duration(self):
        frame = render(_state(planned_protocols=[{
            "plan_id": "plan_values", "operation_id": "run_steps", "state": "planned",
            "created_at": "2026-09-21T12:00:00+03:00", "step_count": 1,
            "expected_duration_s": 30.0,
            "armed_safety_limits": {"pressure_mbar": {}},
            "required_confirmations": [], "warnings": [], "assumptions": [],
            "unmet_guards": [], "digest": "abcdef0123456789",
            "steps": [{
                "number": 1, "name": "a name that is less important than values",
                "flow_setpoints_ul_min": {"0": 100.0}, "pressure_setpoints_mbar": {},
                "trigger_type": "volume",
                "trigger_params": {"sensor_index": 0, "target_volume_ul": 50.0},
                "timeout_s": None, "expected_duration_s": 30.0,
                "on_complete": "zero", "confirmation": None,
            }],
        }]))

        self.assertIn("UNIT", frame)
        self.assertIn("CONTROL", frame)
        self.assertIn("ch0", frame)
        self.assertIn("flow", frame)
        self.assertIn("100.0 uL/min", frame)
        self.assertIn("volume", frame)
        self.assertIn("50.0 uL", frame)
        self.assertIn("30.0s", frame)
        self.assertNotIn("a name that is less important", frame)

    def test_simultaneous_channels_are_one_aligned_control_row_per_unit(self):
        frame = render(_state(planned_protocols=[{
            "plan_id": "plan_parallel", "operation_id": "run_steps", "state": "planned",
            "created_at": "2026-09-21T12:00:00+03:00", "step_count": 1,
            "expected_duration_s": 30.0,
            "armed_safety_limits": {"pressure_mbar": {}},
            "required_confirmations": [], "warnings": [], "assumptions": [],
            "unmet_guards": [], "digest": "abcdef0123456789",
            "steps": [{
                "number": 1, "name": "all channels",
                "flow_setpoints_ul_min": {"0": 100.0, "1": 50.0},
                "pressure_setpoints_mbar": {"2": 400.0},
                "trigger_type": "time", "trigger_params": {"duration_s": 30.0},
                "timeout_s": None, "expected_duration_s": 30.0,
                "on_complete": "zero", "confirmation": None,
            }],
        }]))
        step_lines = [line for line in frame.splitlines() if "ch0" in line or "ch1" in line or "ch2" in line]

        self.assertTrue(any("ch0" in line and "flow" in line and "100.0 uL/min" in line for line in step_lines))
        self.assertTrue(any("ch1" in line and "flow" in line and "50.0 uL/min" in line for line in step_lines))
        self.assertTrue(any("ch2" in line and "pressure" in line and "400.0 mbar" in line for line in step_lines))
        self.assertTrue(all(len(line) <= len(RULE) for line in step_lines))

    def test_confirmation_trigger_is_shortened_to_confirm(self):
        frame = render(_state(planned_protocols=[{
            "plan_id": "plan_confirm", "operation_id": "run_steps", "state": "planned",
            "created_at": "2026-09-21T12:00:00+03:00", "step_count": 1,
            "expected_duration_s": 0.0,
            "armed_safety_limits": {"pressure_mbar": {}},
            "required_confirmations": ["Check mapping"], "warnings": [],
            "assumptions": [], "unmet_guards": [], "digest": "abcdef0123456789",
            "steps": [{
                "number": 1, "name": "confirm", "flow_setpoints_ul_min": {},
                "pressure_setpoints_mbar": {}, "trigger_type": "confirmation",
                "trigger_params": {"message": "Check mapping"}, "timeout_s": None,
                "expected_duration_s": 0.0, "on_complete": "zero",
                "confirmation": "Check mapping",
            }],
        }]))

        self.assertIn("confirm", frame)
        self.assertNotIn("confirmation", frame)

    def test_plan_labels_explain_duration_and_pressure_limit(self):
        frame = render(_state(planned_protocols=[{
            "plan_id": "plan_safe", "operation_id": "run_steps", "state": "planned",
            "created_at": "2026-09-21T12:00:00+03:00", "step_count": 1,
            "expected_duration_s": None,
            "armed_safety_limits": {"pressure_mbar": {}},
            "required_confirmations": [], "warnings": [], "assumptions": [],
            "unmet_guards": [], "digest": "abcdef0123456789", "steps": [],
        }]))

        self.assertIn("EST. DURATION", frame)
        self.assertIn("PRESSURE LIMIT: UNARMED", frame)

    def test_long_plan_notes_are_folded_inside_the_monitor_width(self):
        prompt = "Confirm " + "a very long physical mapping description " * 4
        frame = render(_state(planned_protocols=[{
            "plan_id": "plan_long", "operation_id": "run_steps", "state": "planned",
            "created_at": "2026-09-21T12:00:00+03:00", "step_count": 1,
            "expected_duration_s": None,
            "armed_safety_limits": {"pressure_mbar": {}},
            "required_confirmations": [prompt], "warnings": [prompt],
            "assumptions": [prompt], "unmet_guards": [],
            "digest": "abcdef0123456789",
            "steps": [{
                "number": 1, "name": "confirm mapping",
                "flow_setpoints_ul_min": {}, "pressure_setpoints_mbar": {},
                "trigger_type": "time", "timeout_s": None,
                "expected_duration_s": 0.0, "on_complete": "zero",
                "confirmation": prompt,
            }],
        }]))
        plan_lines = frame.split("  PLANNED PROTOCOLS", 1)[1].split(RULE, 1)[0].splitlines()

        self.assertTrue(all(len(line) <= len(RULE) for line in plan_lines))
        self.assertGreater(sum("physical mapping" in line for line in plan_lines), 1)

    def test_the_running_step_and_its_progress_are_shown(self):
        frame = render(_state(protocol={
            "state": "running", "step_index": 1, "total_steps": 8,
            "step_name": "Oil 100 uL/min sample", "progress": 0.6,
            "outcome": "running", "confirmation_message": "", "error": "",
        }))

        self.assertIn("step 2/8", frame)
        self.assertIn("Oil 100 uL/min sample", frame)
        self.assertIn("60%", frame)
        self.assertIn("█", frame)

    def test_a_question_for_the_operator_is_shown_with_how_to_answer(self):
        frame = render(_state(protocol={
            "state": "running", "step_index": 0, "total_steps": 3, "step_name": "confirm",
            "progress": 0.0, "outcome": "running",
            "confirmation_message": "Is channel 0 the oil line?", "error": "",
        }))

        self.assertIn("Is channel 0 the oil line?", frame)
        self.assertIn("confirm_protocol", frame)
        self.assertIn("skip_protocol", frame)
        self.assertIn("pause_protocol", frame)
        self.assertIn("stop_protocol", frame)

    def test_an_error_is_shown(self):
        frame = render(_state(protocol={
            "state": "error", "step_index": 0, "total_steps": 1, "step_name": "x",
            "progress": 0.0, "outcome": "error", "confirmation_message": "",
            "error": "sensor stopped responding",
        }))

        self.assertIn("sensor stopped responding", frame)

    def test_a_safety_trip_is_shown_with_its_reason(self):
        frame = render(_state(safety={
            "armed": True, "tripped": True, "reason": "oil reached 1904 mbar",
            "limits": {"oil_pressure_mbar": 1900},
        }))

        self.assertIn("TRIPPED", frame)
        self.assertIn("oil reached 1904 mbar", frame)

    def test_armed_limits_are_shown(self):
        frame = render(_state(safety={
            "armed": True, "tripped": False, "reason": "",
            "limits": {"oil_pressure_mbar": 1900},
        }))

        self.assertIn("armed", frame)
        self.assertIn("1900", frame)

    def test_a_recording_shows_where_it_is_being_written(self):
        frame = render(_state(recording={
            "active": True, "id": "oil_20260921", "fluidics_csv": "/runs/x/records/a.csv",
        }))

        self.assertIn("oil_20260921", frame)
        self.assertIn("/runs/x/records/a.csv", frame)

    def test_a_validation_shows_its_target_and_classification(self):
        frame = render(_state(validation={
            "active": True, "id": "oilcap_01", "state": "sampling",
            "current_target_ul_min": 100.0, "classification": "capacity_limited",
            "artifacts": {"summary": "/runs/x/checks/oilcap_01.json"}, "error": "",
        }))

        self.assertIn("oilcap_01", frame)
        self.assertIn("100.0", frame)
        self.assertIn("capacity_limited", frame)
        self.assertIn("/runs/x/checks/oilcap_01.json", frame)

    def test_history_says_what_happened_not_its_bookkeeping(self):
        frame = render(_state(), [{
            "seq": 9, "at": "2026-09-21T12:51:37.516+03:00", "type": "protocol",
            "detail": {"sequence": 61, "monotonic": 12.5, "state": "running",
                       "outcome": "timed_out", "step_index": 1, "step_name": "settle at 150"},
        }])

        self.assertIn("timed_out · settle at 150", frame)
        self.assertNotIn("monotonic", frame)


class LayoutTests(unittest.TestCase):
    def test_live_frame_is_clipped_to_the_terminal_without_scrolling(self):
        frame = "\n".join(f"line {index} is deliberately wide" for index in range(20))

        fitted = _fit_frame(frame, columns=12, rows=5)

        self.assertEqual(len(fitted.splitlines()), 5)
        self.assertTrue(all(len(line) <= 12 for line in fitted.splitlines()))
        self.assertEqual(fitted.splitlines()[-1], "line 19 is d")

    def test_render_uses_the_live_terminal_width_for_section_rules(self):
        frame = render(_state(), width=120)

        self.assertIn("─" * 120, frame)

    def test_vertical_offset_scrolls_the_body_but_pins_the_footer(self):
        frame = "\n".join([*(f"body {index}" for index in range(8)), "controls"])

        fitted = _fit_frame(frame, columns=20, rows=4, offset=3)

        self.assertEqual(fitted.splitlines(), ["body 3", "body 4", "body 5", "controls"])

    def test_arrow_sequences_are_buffered_even_when_the_terminal_splits_them(self):
        keys = _watch_keys(io.StringIO())
        keys._fd = 10
        with (
            unittest.mock.patch("select.select", return_value=([object()], [], [])),
            unittest.mock.patch("admet.core.watch.os.read", side_effect=[b"\x1b", b"[B"]),
        ):
            first = keys._pressed()
            second = keys._pressed()

        self.assertIsNone(first)
        self.assertEqual(second, "down")

    def test_key_reader_waits_only_until_the_next_refresh(self):
        keys = _watch_keys(io.StringIO())
        keys._fd = 10
        with unittest.mock.patch("select.select", return_value=([], [], [])) as selected:
            result = keys._pressed(0.125)

        self.assertIsNone(result)
        selected.assert_called_once_with([unittest.mock.ANY], [], [], 0.125)

    def test_buffered_key_after_an_arrow_is_returned_without_waiting_again(self):
        keys = _watch_keys(io.StringIO())
        keys._fd = 10
        with (
            unittest.mock.patch("select.select", return_value=([object()], [], [])) as selected,
            unittest.mock.patch("admet.core.watch.os.read", return_value=b"\x1b[Bq"),
        ):
            first = keys._pressed(0.5)
            second = keys._pressed(0.5)

        self.assertEqual(first, "down")
        self.assertEqual(second, "q")
        self.assertEqual(selected.call_count, 1)

    def test_clipping_counts_visible_width_not_colour_sequences(self):
        fitted = _fit_frame("\x1b[31mabcdefghijk\x1b[0m", columns=5, rows=1)

        self.assertIn("abcde", fitted)
        self.assertNotIn("f", fitted)

    def test_colour_adds_no_width(self):
        # Padding a string that already holds escape codes counts them as
        # width, which pulls every row out of line.
        plain = render(_state(), colour=False).splitlines()
        painted = render(_state(), colour=True).splitlines()

        def visible(line):
            import re

            return len(re.sub(r"\x1b\[[0-9;]*m", "", line))

        self.assertEqual([len(line) for line in plain], [visible(line) for line in painted])

    def test_no_colour_when_the_output_is_not_a_terminal(self):
        out = io.StringIO()

        watch("/nonexistent", once=True, stream=out)

        self.assertNotIn("\x1b[", out.getvalue())


class OnceTests(unittest.TestCase):
    def test_it_draws_one_frame_and_returns(self):
        out = io.StringIO()

        code = watch("/nonexistent", once=True, stream=out)

        self.assertEqual(code, 0)
        self.assertIn("Nothing is publishing", out.getvalue())

    def test_it_reads_what_a_server_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "state.json").write_text(json.dumps(_state()))
            out = io.StringIO()

            watch(tmp, once=True, stream=out)

            self.assertIn("Oil L", out.getvalue())
            self.assertIn("742.3", out.getvalue())

    def test_it_writes_no_terminal_control_sequences_when_drawing_once(self):
        # --once is for pipes and tests, so it must not move a cursor or hide one.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "state.json").write_text(json.dumps(_state()))
            out = io.StringIO()

            watch(tmp, once=True, stream=out)

            self.assertNotIn("\x1b[?25l", out.getvalue())
            self.assertNotIn("\x1b[H", out.getvalue())


class CommandTests(unittest.TestCase):
    def test_watch_is_the_third_command(self):
        from admet.app import build_parser

        commands = build_parser()._subparsers._group_actions[0].choices

        self.assertEqual(set(commands), {"describe", "serve", "watch"})

    def test_it_requires_a_runtime_directory(self):
        from admet.app import build_parser

        with self.assertRaises(SystemExit):
            with unittest.mock.patch("sys.stderr", io.StringIO()):
                build_parser().parse_args(["watch"])

    def test_once_is_offered(self):
        from admet.app import build_parser

        args = build_parser().parse_args(["watch", "--runtime", "/tmp/rt", "--once"])

        self.assertTrue(args.once)
        self.assertEqual(args.runtime, "/tmp/rt")


if __name__ == "__main__":
    unittest.main()
