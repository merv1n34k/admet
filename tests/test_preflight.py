import json
import unittest

from admet.workflows.preflight import (
    CHECK_DISPENSE,
    CHECK_FLOW,
    CheckConditions,
    CheckSnapshot,
    DispenseCheck,
    FlowCheck,
    FlowSetup,
    GravimetricRun,
    LiquidVolumes,
    dispense_time_s,
    estimate_consumption,
    gravimetric_factors,
    ChannelPath,
    Segment,
    assess_feasibility,
    chip_resistance,
    emulsion_viscosity,
    fit_system_resistance,
    layout_back_pressure,
    solve_flows,
    tubing_back_pressure,
)


class FlowSetupTests(unittest.TestCase):
    def test_reference_setup_matches_the_planning_sheet(self):
        setup = FlowSetup(250.0, 67.0, 67.0)

        self.assertEqual(setup.total_ul_min, 384.0)
        self.assertEqual(setup.aqueous_ul_min, 134.0)
        self.assertAlmostEqual(setup.phase_ratio, 1.866, places=3)

    def test_phase_ratio_at_the_protocol_limits(self):
        self.assertAlmostEqual(FlowSetup(250.0, 58.0, 58.0).phase_ratio, 2.155, places=3)
        self.assertAlmostEqual(FlowSetup(250.0, 70.0, 70.0).phase_ratio, 1.786, places=3)

    def test_solving_a_total_flow_round_trips(self):
        solved = solve_flows(384.0, 1.866)

        self.assertAlmostEqual(solved.total_ul_min, 384.0, places=6)
        self.assertAlmostEqual(solved.phase_ratio, 1.866, places=6)
        self.assertAlmostEqual(solved.beads_ul_min, solved.cells_ul_min)
        self.assertAlmostEqual(solved.oil_ul_min, 250.0, places=1)

    def test_degenerate_inputs_do_not_divide_by_zero(self):
        self.assertEqual(solve_flows(0.0, 1.8).total_ul_min, 0.0)
        self.assertEqual(solve_flows(384.0, 0.0).total_ul_min, 0.0)
        self.assertEqual(FlowSetup(250.0, 0.0, 0.0).phase_ratio, 0.0)


class ConsumptionTests(unittest.TestCase):
    def test_matches_the_planning_sheet(self):
        # two setups at the reference flows, n=3, 60s runs, 30% overage
        setups = [FlowSetup(250.0, 67.0, 67.0), FlowSetup(250.0, 67.0, 67.0)]

        report = estimate_consumption(setups, replicates=3, run_time_s=60.0)

        self.assertEqual(report.tests.oil, 1500.0)
        self.assertEqual(report.tests.water, 804.0)
        self.assertEqual(report.tests.ipa, 0.0)
        # (1500 + 200 priming + 2 setups * 50 dead * 3 runs) * 1.3
        self.assertEqual(report.overall.oil, 2600.0)
        # IPA is dominated by the wash and is not scaled by overage
        self.assertEqual(report.overall.ipa, 5400.0)

    def test_run_time_and_replicates_scale_the_test_volume(self):
        setups = [FlowSetup(250.0, 67.0, 67.0)]

        single = estimate_consumption(setups, replicates=1, run_time_s=60.0)
        doubled = estimate_consumption(setups, replicates=2, run_time_s=120.0)

        self.assertEqual(doubled.tests.oil, single.tests.oil * 4)

    def test_zero_runs_still_reports_the_fixed_volumes(self):
        report = estimate_consumption([], replicates=0, run_time_s=0.0)

        self.assertEqual(report.tests.oil, 0.0)
        self.assertEqual(report.priming.oil, 200.0)
        self.assertEqual(report.washing.ipa, 5000.0)

    def test_overall_rounds_half_away_from_zero_like_the_sheet(self):
        setups = [FlowSetup(60.0, 0.0, 0.0)]
        report = estimate_consumption(
            setups,
            replicates=1,
            run_time_s=60.0,
            overage_percent=0.0,
            dead_volume=LiquidVolumes(0.0, 0.0, 0.0),
            priming=LiquidVolumes(90.0, 0.0, 0.0),
        )

        # 60 + 90 = 150 -> 200, not the 100 that banker's rounding would give
        self.assertEqual(report.overall.oil, 200.0)

    def test_report_lists_every_stage(self):
        report = estimate_consumption([FlowSetup(250.0, 67.0, 67.0)], replicates=1, run_time_s=60.0)

        self.assertEqual(
            [name for name, _volumes in report.stages],
            ["Dead volume", "Priming", "Tests", "Washing", "Overall"],
        )


class GravimetricTests(unittest.TestCase):
    def test_a_perfect_dispense_gives_a_factor_of_one(self):
        # 100 uL of a 1.614 g/mL oil weighs 0.1614 g
        runs = [GravimetricRun("Oil L", 10.0, 10.1614)]

        result = gravimetric_factors(runs, densities={"Oil L": 1.614}, target_ul=100.0)[0]

        self.assertAlmostEqual(result.mean_volume_ul, 100.0, places=3)
        self.assertAlmostEqual(result.mean_relative, 1.0, places=3)

    def test_under_dispensing_reports_a_factor_below_one(self):
        runs = [GravimetricRun("Oil L", 10.0, 10.0807)]  # half the expected mass

        result = gravimetric_factors(runs, densities={"Oil L": 1.614}, target_ul=100.0)[0]

        self.assertAlmostEqual(result.mean_relative, 0.5, places=3)

    def test_replicates_are_averaged_per_channel(self):
        runs = [
            GravimetricRun("Oil L", 10.0, 10.1614),
            GravimetricRun("Oil L", 10.0, 10.0807),
            GravimetricRun("Cells M", 10.0, 10.1),
        ]

        results = {r.channel: r for r in gravimetric_factors(runs, densities={"Oil L": 1.614, "Cells M": 1.0}, target_ul=100.0)}

        self.assertEqual(results["Oil L"].runs, 2)
        self.assertAlmostEqual(results["Oil L"].mean_relative, 0.75, places=3)
        self.assertEqual(results["Cells M"].runs, 1)

    def test_missing_density_does_not_raise(self):
        results = gravimetric_factors(
            [GravimetricRun("Oil L", 10.0, 10.5)], densities={}, target_ul=100.0
        )

        self.assertEqual(results[0].mean_volume_ul, 0.0)

    def test_dispense_time(self):
        self.assertEqual(dispense_time_s(100.0, 250.0), 24.0)
        self.assertEqual(dispense_time_s(100.0, 0.0), 0.0)



class BackPressureTests(unittest.TestCase):
    def test_drop_scales_with_length_and_inverse_fourth_power_of_bore(self):
        base = tubing_back_pressure(
            length_cm=100, inner_diameter_mm=0.25, flow_ul_min=250, viscosity_mpa_s=1.24
        )
        longer = tubing_back_pressure(
            length_cm=200, inner_diameter_mm=0.25, flow_ul_min=250, viscosity_mpa_s=1.24
        )
        wider = tubing_back_pressure(
            length_cm=100, inner_diameter_mm=0.50, flow_ul_min=250, viscosity_mpa_s=1.24
        )

        self.assertAlmostEqual(longer.drop_mbar / base.drop_mbar, 2.0, places=6)
        self.assertAlmostEqual(base.drop_mbar / wider.drop_mbar, 16.0, places=6)

    def test_matches_hagen_poiseuille_by_hand(self):
        # dP = 128 * mu * L * Q / (pi * d^4)
        from math import pi

        mu, length_m, flow_m3s, diameter_m = 1.24e-3, 1.0, 250e-9 / 60, 0.25e-3
        expected_mbar = 128 * mu * length_m * flow_m3s / (pi * diameter_m**4) / 100

        result = tubing_back_pressure(
            length_cm=100, inner_diameter_mm=0.25, flow_ul_min=250, viscosity_mpa_s=1.24
        )

        self.assertAlmostEqual(result.drop_mbar, expected_mbar, places=6)

    def test_flags_a_line_the_controller_cannot_drive(self):
        result = tubing_back_pressure(
            length_cm=500,
            inner_diameter_mm=0.25,
            flow_ul_min=250,
            viscosity_mpa_s=1.24,
            limit_mbar=2000,
        )

        self.assertFalse(result.within_limit)
        self.assertLess(result.headroom_mbar, 0)
        # the longest line that would have worked
        self.assertAlmostEqual(result.max_length_cm, 371.0, delta=1.0)

    def test_max_length_is_the_break_even_point(self):
        limit = 2000.0
        result = tubing_back_pressure(
            length_cm=100, inner_diameter_mm=0.25, flow_ul_min=250,
            viscosity_mpa_s=1.24, limit_mbar=limit,
        )
        at_max = tubing_back_pressure(
            length_cm=result.max_length_cm, inner_diameter_mm=0.25, flow_ul_min=250,
            viscosity_mpa_s=1.24, limit_mbar=limit,
        )

        self.assertAlmostEqual(at_max.drop_mbar, limit, places=6)

    def test_reynolds_stays_laminar_at_working_flows(self):
        result = tubing_back_pressure(
            length_cm=100, inner_diameter_mm=0.25, flow_ul_min=250,
            viscosity_mpa_s=1.24, density_g_ml=1.614,
        )

        self.assertTrue(result.laminar)
        self.assertGreater(result.reynolds, 0)

    def test_degenerate_inputs_do_not_raise(self):
        base = {"length_cm": 100, "inner_diameter_mm": 0.25, "flow_ul_min": 250, "viscosity_mpa_s": 1.24}
        for field, value in (("inner_diameter_mm", 0.0), ("viscosity_mpa_s", 0.0)):
            result = tubing_back_pressure(**{**base, field: value})

            self.assertEqual(result.drop_mbar, 0.0, field)


class SystemResistanceTests(unittest.TestCase):
    def test_fit_recovers_a_known_resistance_and_threshold(self):
        # P = 33 * Q + 25, sampled exactly
        samples = [(33.0 * q + 25.0, q) for q in (5, 20, 40, 60)]

        system = fit_system_resistance(samples)

        self.assertAlmostEqual(system.resistance, 33.0, places=6)
        self.assertAlmostEqual(system.threshold_mbar, 25.0, places=6)
        self.assertAlmostEqual(system.r_squared, 1.0, places=9)
        self.assertTrue(system.trustworthy)

    def test_a_scattered_sweep_is_not_trustworthy(self):
        samples = [(200, 5), (600, 40), (1000, 12), (1400, 80)]

        self.assertFalse(fit_system_resistance(samples).trustworthy)

    def test_too_few_points_is_inert(self):
        system = fit_system_resistance([(200, 5)])

        self.assertEqual(system.resistance, 0.0)
        self.assertFalse(system.trustworthy)

    def test_predictions_invert_each_other(self):
        system = fit_system_resistance([(33.0 * q + 25.0, q) for q in (5, 20, 40, 60)])

        self.assertAlmostEqual(system.flow_at(system.pressure_for(42.0)), 42.0, places=6)


class FeasibilityTests(unittest.TestCase):
    def _chip_dominated(self):
        return fit_system_resistance([(33.0 * q + 25.0, q) for q in (5, 20, 40, 60)])

    def test_reachable_target_is_feasible(self):
        result = assess_feasibility(self._chip_dominated(), target_flow_ul_min=50, limit_mbar=2000)

        self.assertTrue(result.feasible)
        self.assertEqual(result.shortfall_mbar, 0.0)
        self.assertEqual(result.remedies, ())

    def test_unreachable_target_reports_the_ceiling(self):
        result = assess_feasibility(self._chip_dominated(), target_flow_ul_min=250, limit_mbar=2000)

        self.assertFalse(result.feasible)
        self.assertGreater(result.shortfall_mbar, 0)
        self.assertAlmostEqual(result.max_flow_ul_min, (2000 - 25) / 33.0, places=6)

    def test_says_plainly_when_no_tubing_change_can_help(self):
        result = assess_feasibility(
            self._chip_dominated(), target_flow_ul_min=250, limit_mbar=2000,
            tubing_resistance=2.16, tubing_length_cm=100, tubing_id_mm=0.25,
        )

        joined = " ".join(result.remedies)
        self.assertIn("no tubing change helps", joined)
        self.assertNotIn("Shorten the tubing", joined)

    def test_suggests_re_plumbing_when_the_tubing_is_the_problem(self):
        # tubing 10.78, chip 1.0
        system = fit_system_resistance([(11.78 * q + 20.0, q) for q in (20, 60, 100, 150)])

        result = assess_feasibility(
            system, target_flow_ul_min=250, limit_mbar=2000,
            tubing_resistance=10.78, tubing_length_cm=500, tubing_id_mm=0.25,
        )

        joined = " ".join(result.remedies)
        self.assertIn("Shorten the tubing", joined)
        self.assertIn("Widen the tubing bore", joined)

    def test_the_suggested_bore_actually_clears_the_limit(self):
        import re

        length, bore, viscosity, target, limit = 500.0, 0.25, 1.24, 250.0, 2000.0
        tubing_r = tubing_back_pressure(
            length_cm=length, inner_diameter_mm=bore, flow_ul_min=target, viscosity_mpa_s=viscosity
        ).drop_mbar / target
        chip_r = 1.0
        system = fit_system_resistance([((tubing_r + chip_r) * q + 20.0, q) for q in (20, 60, 100)])

        result = assess_feasibility(
            system, target_flow_ul_min=target, limit_mbar=limit,
            tubing_resistance=tubing_r, tubing_length_cm=length, tubing_id_mm=bore,
        )
        suggested = float(re.search(r"bore to ([0-9.]+) mm", " ".join(result.remedies)).group(1))

        new_tubing_r = tubing_back_pressure(
            length_cm=length, inner_diameter_mm=suggested, flow_ul_min=target,
            viscosity_mpa_s=viscosity,
        ).drop_mbar / target
        required = (new_tubing_r + chip_r) * target + system.threshold_mbar

        self.assertLessEqual(required, limit)


class LayoutTests(unittest.TestCase):
    def _channels(self, oil_leg_cm=15.0):
        return [
            ChannelPath("Oil", (Segment(20, 0.75), Segment(oil_leg_cm, 0.25)), 250, 1.24),
            ChannelPath("Cells", (Segment(20, 0.75), Segment(15, 0.25)), 67, 0.89),
            ChannelPath("Beads", (Segment(20, 0.75), Segment(15, 0.25)), 67, 0.89),
        ]

    def test_segments_in_a_channel_add_up(self):
        one = ChannelPath("x", (Segment(30, 0.25),), 250, 1.24)
        split = ChannelPath("x", (Segment(10, 0.25), Segment(20, 0.25)), 250, 1.24)

        self.assertAlmostEqual(one.drop_mbar(), split.drop_mbar(), places=9)

    def test_inlets_are_parallel_so_one_line_only_affects_its_own_channel(self):
        base = {load.label: load for load in layout_back_pressure(self._channels())}
        longer = {load.label: load for load in layout_back_pressure(self._channels(oil_leg_cm=200))}

        self.assertGreater(longer["Oil"].path_mbar, base["Oil"].path_mbar)
        self.assertEqual(longer["Cells"].path_mbar, base["Cells"].path_mbar)
        self.assertEqual(longer["Beads"].path_mbar, base["Beads"].path_mbar)

    def test_the_outlet_is_shared_by_every_channel(self):
        loads = layout_back_pressure(self._channels(), outlet=Segment(20, 0.25))

        outlet_drops = {round(load.outlet_mbar, 9) for load in loads}
        self.assertEqual(len(outlet_drops), 1)
        self.assertGreater(outlet_drops.pop(), 0)

    def test_the_outlet_is_charged_at_the_total_flow(self):
        channels = self._channels()
        total = sum(channel.flow_ul_min for channel in channels)
        outlet = Segment(20, 0.25)

        load = layout_back_pressure(channels, outlet=outlet, outlet_viscosity_mpa_s=1.24)[0]

        self.assertAlmostEqual(load.outlet_mbar, outlet.resistance(1.24) * total, places=9)

    def test_without_an_outlet_only_the_channel_paths_count(self):
        loads = layout_back_pressure(self._channels())

        for load in loads:
            self.assertEqual(load.outlet_mbar, 0.0)
            self.assertEqual(load.total_mbar, load.path_mbar)


class EmulsionViscosityTests(unittest.TestCase):
    def test_no_droplets_leaves_the_carrier_alone(self):
        self.assertEqual(emulsion_viscosity(1.24, 1.0, 0.0), 1.24)

    def test_the_emulsion_is_thicker_than_either_liquid(self):
        # This is the point: what leaves the chip flows less easily than the oil
        # that went in, and the outlet carries every channel's flow.
        thickened = emulsion_viscosity(1.24, 1.0, 0.35)

        self.assertGreater(thickened, 1.24)
        self.assertGreater(thickened, 1.0)

    def test_it_rises_with_the_aqueous_fraction(self):
        low = emulsion_viscosity(1.24, 1.0, 0.1)
        high = emulsion_viscosity(1.24, 1.0, 0.4)

        self.assertGreater(high, low)

    def test_rigid_droplets_reduce_to_the_einstein_limit(self):
        # As the dispersed phase stiffens, Taylor's shape factor tends to 2.5.
        self.assertAlmostEqual(emulsion_viscosity(1.0, 1e9, 0.2), 1.0 + 2.5 * 0.2, places=6)

    def test_a_nonsense_fraction_cannot_produce_a_nonsense_viscosity(self):
        self.assertEqual(emulsion_viscosity(1.24, 1.0, -5.0), 1.24)
        self.assertGreater(emulsion_viscosity(1.24, 1.0, 50.0), 1.24)
        self.assertEqual(emulsion_viscosity(0.0, 1.0, 0.3), 0.0)


class ChipResistanceTests(unittest.TestCase):
    def test_the_chip_is_what_the_fit_leaves_after_the_plumbing(self):
        self.assertAlmostEqual(chip_resistance(13.33, 4.52), 8.81, places=6)

    def test_plumbing_larger_than_the_fit_reads_as_no_chip_rather_than_negative(self):
        # Happens when the entered layout overstates the tubing; a negative chip
        # would be worse than useless.
        self.assertEqual(chip_resistance(2.0, 5.0), 0.0)

    def test_an_unmeasured_channel_leaves_the_chip_unknown_not_negative(self):
        self.assertEqual(chip_resistance(0.0, 0.0), 0.0)


class CheckSnapshotTests(unittest.TestCase):
    def _conditions(self):
        return CheckConditions(
            setup=FlowSetup(250.0, 67.0, 67.0),
            liquids={"Oil L": (0.99, 1.24)},
            paths=(ChannelPath("Oil L", (Segment(20.0, 0.75), Segment(5.0, 0.25)), 250.0, 1.24),),
            outlet=Segment(20.0, 0.25),
            pressure_limit_mbar=2000.0,
        )

    def _flow_snapshot(self, samples):
        fit = fit_system_resistance(samples)
        return CheckSnapshot(
            kind=CHECK_FLOW,
            recorded_at="2026-08-20T10:00:00+00:00",
            conditions=self._conditions(),
            flow_checks=(
                FlowCheck(
                    "Oil L",
                    tuple(samples),
                    fit,
                    assess_feasibility(fit, target_flow_ul_min=250.0, limit_mbar=2000.0),
                ),
            ),
        )

    def test_a_snapshot_survives_a_json_round_trip(self):
        snapshot = self._flow_snapshot([(500.0, 50.0), (1000.0, 100.0), (1500.0, 150.0)])

        restored = json.loads(json.dumps(snapshot.to_dict()))

        self.assertEqual(restored["kind"], CHECK_FLOW)
        self.assertEqual(restored["recorded_at"], "2026-08-20T10:00:00+00:00")
        self.assertEqual(len(restored["flow_checks"][0]["samples"]), 3)

    def test_the_snapshot_carries_the_setup_the_check_was_run_on(self):
        # Without the plumbing and the liquid, a resistance figure cannot be
        # compared against a later check.
        conditions = self._flow_snapshot([(500.0, 50.0), (1000.0, 100.0)]).to_dict()["conditions"]

        self.assertEqual(conditions["liquids"]["Oil L"]["viscosity_mpa_s"], 1.24)
        self.assertEqual(conditions["layout"]["outlet"], {"length_cm": 20.0, "bore_mm": 0.25})
        self.assertEqual(
            conditions["layout"]["channels"][0]["runs"],
            [{"length_cm": 20.0, "bore_mm": 0.75}, {"length_cm": 5.0, "bore_mm": 0.25}],
        )
        self.assertAlmostEqual(conditions["flows_ul_min"]["phase_ratio"], 1.866, places=3)

    def test_the_stored_fit_splits_the_path_into_plumbing_and_chip(self):
        fit = fit_system_resistance([(500.0, 50.0), (1000.0, 100.0), (1500.0, 150.0)])
        check = FlowCheck(
            "Oil L",
            ((500.0, 50.0), (1000.0, 100.0), (1500.0, 150.0)),
            fit,
            assess_feasibility(fit, target_flow_ul_min=250.0, limit_mbar=2000.0),
            tubing_resistance=2.0,
        )

        written = check.to_dict()["fit"]

        self.assertAlmostEqual(written["resistance_mbar_per_ul_min"], 10.0, places=6)
        self.assertEqual(written["tubing_mbar_per_ul_min"], 2.0)
        self.assertAlmostEqual(written["chip_mbar_per_ul_min"], 8.0, places=6)

    def test_the_summary_names_a_channel_that_cannot_reach_its_flow(self):
        # 10 mbar per uL/min needs 2,500 mbar at 250 uL/min, over the 2,000 limit.
        snapshot = self._flow_snapshot([(500.0, 50.0), (1000.0, 100.0), (1500.0, 150.0)])

        self.assertIn("not feasible", snapshot.summary())
        self.assertIn("Oil L", snapshot.summary())

    def test_a_feasible_sweep_reads_as_feasible(self):
        snapshot = self._flow_snapshot([(100.0, 50.0), (200.0, 100.0), (300.0, 150.0)])

        self.assertTrue(snapshot.flow_checks[0].feasibility.feasible)
        self.assertIn("feasible", snapshot.summary())
        self.assertNotIn("not feasible", snapshot.summary())

    def test_a_dispense_snapshot_keeps_the_weights_behind_the_factor(self):
        runs = (GravimetricRun("Oil L", 1.0, 1.099), GravimetricRun("Oil L", 1.0, 1.1))
        result = gravimetric_factors(runs, densities={"Oil L": 0.99}, target_ul=100.0)[0]
        snapshot = CheckSnapshot(
            kind=CHECK_DISPENSE,
            recorded_at="2026-08-20T10:00:00+00:00",
            conditions=self._conditions(),
            dispense_checks=(DispenseCheck("Oil L", 100.0, 250.0, runs, result),),
        )

        written = snapshot.to_dict()["dispense_checks"][0]

        self.assertEqual(len(written["weights_g"]), 2)
        self.assertAlmostEqual(written["weights_g"][0]["net"], 0.099, places=6)
        self.assertAlmostEqual(written["result"]["mean_factor"], 1.005, places=3)
        self.assertIn("Oil L 1.005", snapshot.summary())


if __name__ == "__main__":
    unittest.main()
