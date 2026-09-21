"""The oil-capacity validation.

The classification is the product, so most of this tests it directly against
made-up measurements: a run is only allowed to be called a pass for reasons
that are true of the numbers, never because the protocol thread ended.

The rest drives the whole loop against the simulator, including the operator
gate and the pressure trip.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from admet.core.service import Admet
from admet.workflows.operations import Refused, operation
from admet.workflows.validation import (
    CAPACITY_LIMITED,
    INVALID,
    MAX_TRIP_MBAR,
    OIL_CHANNEL,
    PASS,
    UNSTABLE,
    build_steps,
    classify,
    confirmation_message,
    read_rows,
    summarise_target,
)

SETTINGS = {
    "configuration": "bypass_chip",
    "flow_targets_ul_min": [50.0, 100.0],
    "settle_tolerance_ul_min": 5.0,
    "settle_window_s": 5.0,
    "settle_timeout_s": 30.0,
    "sample_window_s": 10.0,
    "oil_pressure_trip_mbar": 1900.0,
    "minimum_flow_fraction": 0.85,
    "tick_s": 0.2,
}

CHANNEL = {
    "index": 0,
    "label": "Oil L",
    "detected": {
        "sensor_index": 0,
        "sensor_type": "Flow_L_dual",
        "sensor_max_ul_min": 5000.0,
        "pressure_index": 0,
        "pressure_max_mbar": 2000.0,
    },
}


def _target(requested, *, settled=True, flow=None, pressure=None, samples=10):
    """A measured target, as the summary would have built it."""
    flow = flow if flow is not None else requested
    pressure = pressure if pressure is not None else 500.0
    return {
        "requested_ul_min": requested,
        "settled": settled,
        "settle_outcome": "completed" if settled else "timed_out",
        "flow_ul_min": {"mean": flow, "std": 1.0, "min": flow - 1, "max": flow + 1},
        "pressure_mbar": {"mean": pressure, "std": 2.0, "min": pressure - 5, "max": pressure},
        "samples": samples,
        "sample_duration_s": 10.0,
        "flow_fraction": flow / requested if requested else None,
        "held_the_flow": (flow / requested) >= 0.85 if requested else False,
        "from_elapsed_s": 0.0,
        "to_elapsed_s": 10.0,
    }


class ShapeTests(unittest.TestCase):
    def test_the_steps_climb_and_never_jump_from_nothing_to_the_target(self):
        steps, plan = build_steps(SETTINGS, CHANNEL)

        setpoints = [step.sensor_setpoints.get(OIL_CHANNEL, 0.0) for step in steps]
        self.assertEqual(setpoints, sorted(setpoints))
        self.assertEqual([entry["role"] for entry in plan][:2], ["confirm", "lead_in"])
        self.assertLess(setpoints[1], 50.0)

    def test_a_single_target_still_gets_a_lead_in(self):
        # Otherwise one target means going straight from nothing to all of it.
        _steps, plan = build_steps({**SETTINGS, "flow_targets_ul_min": [150.0]}, CHANNEL)

        self.assertEqual([entry["role"] for entry in plan],
                         ["confirm", "lead_in", "settle", "sample"])
        self.assertEqual(plan[1]["target_ul_min"], 75.0)

    def test_each_target_settles_before_it_is_sampled(self):
        _steps, plan = build_steps(SETTINGS, CHANNEL)

        roles = [entry["role"] for entry in plan]
        for index, role in enumerate(roles):
            if role == "sample":
                self.assertEqual(roles[index - 1], "settle")

    def test_targets_are_run_lowest_first_and_deduplicated(self):
        _steps, plan = build_steps(
            {**SETTINGS, "flow_targets_ul_min": [100.0, 50.0, 100.0]}, CHANNEL
        )

        sampled = [entry["target_ul_min"] for entry in plan if entry["role"] == "sample"]
        self.assertEqual(sampled, [50.0, 100.0])

    def test_the_last_step_takes_the_channel_back_to_zero(self):
        steps, _plan = build_steps(SETTINGS, CHANNEL)

        self.assertEqual(steps[-1].on_complete, "zero")

    def test_the_operator_is_asked_before_anything_flows(self):
        steps, _plan = build_steps(SETTINGS, CHANNEL)

        self.assertTrue(steps[0].confirm_message)
        self.assertEqual(steps[0].sensor_setpoints, {})

    def test_the_question_quotes_the_instrument_not_the_configuration(self):
        # The mistake being guarded against is the two disagreeing.
        message = confirmation_message("bypass_chip", CHANNEL)

        self.assertIn("Flow_L_dual", message)
        self.assertIn("2000.0 mbar", message)
        self.assertIn("bypass_chip", message)
        self.assertIn("outlet", message)

    def test_no_targets_is_a_mistake_worth_reporting(self):
        with self.assertRaises(ValueError):
            build_steps({**SETTINGS, "flow_targets_ul_min": []}, CHANNEL)


class ClassificationTests(unittest.TestCase):
    """What the numbers showed, never whether the thread finished."""

    def test_everything_settled_and_held_is_a_pass(self):
        verdict, why = classify(
            [_target(50.0), _target(100.0)], tripped=False, trip_mbar=1900.0
        )

        self.assertEqual(verdict, PASS)
        self.assertIn("settled", why)

    def test_a_trip_is_capacity_limited(self):
        verdict, _why = classify([_target(50.0)], tripped=True, trip_mbar=1900.0)

        self.assertEqual(verdict, CAPACITY_LIMITED)

    def test_a_trip_explains_its_own_missing_targets(self):
        # The run stopped, so the targets above it were never reached. Filing
        # the clearest result as a broken run would be the worst outcome here.
        verdict, why = classify(
            [_target(50.0), _target(300.0, samples=0, settled=False)],
            tripped=True,
            trip_mbar=1900.0,
        )

        self.assertEqual(verdict, CAPACITY_LIMITED)
        self.assertIn("300 uL/min was never reached", why)
        self.assertIn("highest target reached was 50", why)

    def test_not_settling_at_the_pressure_ceiling_is_capacity_limited(self):
        verdict, why = classify(
            [_target(150.0, settled=False, pressure=1890.0)], tripped=False, trip_mbar=1900.0
        )

        self.assertEqual(verdict, CAPACITY_LIMITED)
        self.assertIn("at the limit", why)

    def test_not_settling_with_pressure_to_spare_is_unstable(self):
        verdict, why = classify(
            [_target(150.0, settled=False, pressure=300.0)], tripped=False, trip_mbar=1900.0
        )

        self.assertEqual(verdict, UNSTABLE)
        self.assertIn("never settled", why)

    def test_settling_short_of_the_flow_asked_for_is_capacity_limited(self):
        verdict, why = classify(
            [_target(100.0, flow=60.0)], tripped=False, trip_mbar=1900.0
        )

        self.assertEqual(verdict, CAPACITY_LIMITED)
        self.assertIn("60%", why)

    def test_missing_data_without_a_trip_is_invalid(self):
        verdict, why = classify(
            [_target(50.0, samples=0)], tripped=False, trip_mbar=1900.0
        )

        self.assertEqual(verdict, INVALID)
        self.assertIn("no recorded rows", why)

    def test_nothing_measured_is_invalid(self):
        self.assertEqual(classify([], tripped=False, trip_mbar=1900.0)[0], INVALID)

    def test_a_stated_reason_makes_it_invalid_whatever_the_numbers_say(self):
        verdict, why = classify(
            [_target(50.0)], tripped=False, trip_mbar=1900.0,
            reason="the channel mapping was never confirmed",
        )

        self.assertEqual(verdict, INVALID)
        self.assertIn("mapping", why)


class WindowTests(unittest.TestCase):
    """Rows belong to the target whose sampling window they fall in."""

    ROWS = [
        {"elapsed_s": 1.0, "flow_ul_min": 10.0, "pressure_mbar": 100.0},
        {"elapsed_s": 2.0, "flow_ul_min": 50.0, "pressure_mbar": 500.0},
        {"elapsed_s": 3.0, "flow_ul_min": 52.0, "pressure_mbar": 520.0},
        {"elapsed_s": 9.0, "flow_ul_min": 99.0, "pressure_mbar": 900.0},
    ]

    def test_only_the_rows_inside_the_window_are_used(self):
        summary = summarise_target(
            {"target_ul_min": 50.0, "settled": True, "settle_outcome": "completed",
             "from_elapsed_s": 2.0, "to_elapsed_s": 3.0},
            self.ROWS,
            minimum_flow_fraction=0.85,
        )

        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["flow_ul_min"]["mean"], 51.0)
        self.assertEqual(summary["pressure_mbar"]["max"], 520.0)

    def test_the_flow_fraction_is_measured_over_requested(self):
        summary = summarise_target(
            {"target_ul_min": 100.0, "settled": True, "settle_outcome": "completed",
             "from_elapsed_s": 9.0, "to_elapsed_s": 9.0},
            self.ROWS,
            minimum_flow_fraction=0.85,
        )

        self.assertAlmostEqual(summary["flow_fraction"], 0.99)
        self.assertTrue(summary["held_the_flow"])

    def test_a_window_with_no_rows_reports_nothing_rather_than_zero(self):
        summary = summarise_target(
            {"target_ul_min": 50.0, "settled": False, "settle_outcome": "timed_out",
             "from_elapsed_s": 100.0, "to_elapsed_s": 200.0},
            self.ROWS,
            minimum_flow_fraction=0.85,
        )

        self.assertEqual(summary["samples"], 0)
        self.assertIsNone(summary["flow_ul_min"]["mean"])
        self.assertIsNone(summary["flow_fraction"])

    def test_a_step_that_never_ran_has_no_window(self):
        summary = summarise_target(
            {"target_ul_min": 300.0, "settled": False, "settle_outcome": "never reached",
             "from_elapsed_s": None, "to_elapsed_s": None},
            self.ROWS,
            minimum_flow_fraction=0.85,
        )

        self.assertEqual(summary["samples"], 0)

    def test_a_missing_log_is_no_rows_rather_than_a_crash(self):
        self.assertEqual(read_rows("/nonexistent/fluidics.csv"), [])


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.admet = Admet()

    def test_it_says_what_it_needs_first(self):
        self.assertEqual(
            set(operation("validate_oil_capacity").requires),
            {"project", "fluidics", "corrections", "idle", "safe"},
        )

    def test_it_returns_while_the_run_goes_on(self):
        self.assertEqual(operation("validate_oil_capacity").kind, "start")
        self.assertTrue(operation("validate_oil_capacity").starts_protocol)

    def test_a_trip_limit_above_the_cap_is_refused(self):
        # The controller tops out at 2000 and the point is to stop short of it.
        # Declared as the parameter's maximum, so it is refused by the schema
        # and a model reading the schema is told before it asks. Guards are
        # checked before settings, so the session has to be real to get here.
        with tempfile.TemporaryDirectory() as tmp:
            self.admet.create_project(Path(tmp) / "rig.admetp")
            self.admet.do("connect_fluidics", {"simulated": True})
            self.admet.do("apply_corrections")
            self.addCleanup(self.admet.do, "disconnect_fluidics")

            with self.assertRaises(ValueError) as caught:
                self.admet.do("validate_oil_capacity", {
                    "configuration": "bypass_chip",
                    "flow_targets_ul_min": [50.0],
                    "oil_pressure_trip_mbar": MAX_TRIP_MBAR + 1,
                })

            self.assertIn("1900", str(caught.exception))

    def test_the_cap_is_in_the_schema_a_client_reads(self):
        declared = {p["name"]: p for p in Admet().describe("validate_oil_capacity")["params"]}

        self.assertEqual(declared["oil_pressure_trip_mbar"]["maximum"], MAX_TRIP_MBAR)

    def test_an_unknown_configuration_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.admet.create_project(Path(tmp) / "rig.admetp")
            self.admet.do("connect_fluidics", {"simulated": True})
            self.admet.do("apply_corrections")
            self.addCleanup(self.admet.do, "disconnect_fluidics")

            with self.assertRaises(Exception) as caught:
                self.admet.do("validate_oil_capacity", {
                    "configuration": "through_the_cat",
                    "flow_targets_ul_min": [50.0],
                })

            self.assertIn("bypass_chip", str(caught.exception))

    def test_it_is_refused_without_a_project_to_write_into(self):
        self.admet.do("connect_fluidics", {"simulated": True})
        self.admet.do("apply_corrections")
        self.addCleanup(self.admet.do, "disconnect_fluidics")

        with self.assertRaises(Refused) as caught:
            self.admet.do("validate_oil_capacity", {
                "configuration": "bypass_chip", "flow_targets_ul_min": [50.0],
            })

        self.assertIn("no project is open", str(caught.exception))


class RunTests(unittest.TestCase):
    """The whole loop against the simulator."""

    FAST = {
        "settle_tolerance_ul_min": 50.0,
        "settle_window_s": 1.0,
        "settle_timeout_s": 3.0,
        "sample_window_s": 1.0,
        "tick_s": 0.1,
    }

    def _run(self, **overrides):
        tmp = Path(tempfile.mkdtemp())
        admet = Admet()
        admet.create_project(tmp / "oil.admetp")
        admet.do("connect_fluidics", {"simulated": True})
        admet.do("apply_corrections")
        self.addCleanup(admet.do, "disconnect_fluidics")
        admet.do("validate_oil_capacity", {
            "configuration": "bypass_chip",
            "flow_targets_ul_min": [5.0, 10.0],
            **self.FAST,
            **overrides,
        })
        return admet

    def _finish(self, admet, *, confirm=True):
        if confirm:
            deadline = time.monotonic() + 5
            while not admet.do("observe")["protocol"]["confirmation_message"]:
                if time.monotonic() > deadline:
                    self.fail("never asked for confirmation")
                time.sleep(0.05)
            admet.do("confirm_protocol")
        deadline = time.monotonic() + 60
        while admet.do("observe")["validation"]["active"]:
            if time.monotonic() > deadline:
                self.fail("the validation never finished")
            time.sleep(0.2)
        return admet.do("observe")["validation"]

    def test_nothing_flows_until_the_mapping_is_confirmed(self):
        admet = self._run()
        time.sleep(0.5)

        observed = admet.do("observe")
        self.assertIn("physically the oil line", observed["protocol"]["confirmation_message"])
        oil = observed["channels"][OIL_CHANNEL]
        self.assertEqual(oil["mode"], "off")
        self.assertIsNone(oil["requested_flow_ul_min"])
        admet.do("stop_protocol")

    def test_the_run_is_visible_while_it_runs(self):
        # The whole reason it does not block.
        admet = self._run()
        time.sleep(0.4)

        validation = admet.do("observe")["validation"]
        self.assertTrue(validation["active"])
        self.assertTrue(validation["id"])
        self.assertEqual(validation["configuration"], "bypass_chip")
        self.assertIn("fluidics_csv", validation["artifacts"])
        admet.do("stop_protocol")

    def test_a_good_run_passes_and_writes_its_summary(self):
        admet = self._run()

        validation = self._finish(admet)

        self.assertEqual(validation["classification"], PASS)
        summary = json.loads(Path(validation["artifacts"]["summary"]).read_text())
        self.assertTrue(summary["mapping_confirmed"])
        self.assertEqual([t["requested_ul_min"] for t in summary["targets"]], [5.0, 10.0])
        for target in summary["targets"]:
            self.assertTrue(target["settled"])
            self.assertGreater(target["samples"], 0)
            self.assertIsNotNone(target["flow_ul_min"]["mean"])
            self.assertIsNotNone(target["pressure_mbar"]["max"])

    def test_the_summary_keeps_what_the_run_was_measured_under(self):
        admet = self._run()

        validation = self._finish(admet)

        summary = json.loads(Path(validation["artifacts"]["summary"]).read_text())
        self.assertEqual(summary["context"]["channels"][0]["label"], "Oil L")
        self.assertTrue(summary["context"]["software_version"])
        self.assertEqual(summary["settings"]["configuration"], "bypass_chip")

    def test_the_raw_log_is_kept_and_the_windows_point_into_it(self):
        admet = self._run()

        validation = self._finish(admet)

        rows = read_rows(validation["artifacts"]["fluidics_csv"])
        summary = json.loads(Path(validation["artifacts"]["summary"]).read_text())
        self.assertGreater(len(rows), 0)
        for target in summary["targets"]:
            inside = [
                row for row in rows
                if target["from_elapsed_s"] <= row["elapsed_s"] <= target["to_elapsed_s"]
            ]
            self.assertEqual(len(inside), target["samples"])

    def test_reaching_the_limit_is_capacity_limited_and_leaves_the_rig_safe(self):
        admet = self._run(
            flow_targets_ul_min=[50.0, 300.0], oil_pressure_trip_mbar=60.0
        )

        validation = self._finish(admet)

        self.assertEqual(validation["classification"], CAPACITY_LIMITED)
        observed = admet.do("observe")
        self.assertTrue(observed["safety"]["tripped"])
        self.assertEqual({c["mode"] for c in observed["channels"]}, {"off"})
        self.assertFalse(admet.state()["running"])
        admet.do("reset_safety")

    def test_the_summary_survives_the_project_being_reopened(self):
        admet = self._run()
        self._finish(admet)
        path = admet.project.path

        reopened = Admet()
        reopened.open_project(path)

        checks = list((path / "records" / "checks").glob("*.json"))
        self.assertTrue(checks)
        self.assertEqual(
            json.loads(checks[0].read_text())["kind"], "oil_capacity"
        )


if __name__ == "__main__":
    unittest.main()
