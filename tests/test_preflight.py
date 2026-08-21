import unittest

from admet.workflows.preflight import (
    FlowSetup,
    GravimetricRun,
    LiquidVolumes,
    dispense_time_s,
    estimate_consumption,
    gravimetric_factors,
    solve_flows,
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


if __name__ == "__main__":
    unittest.main()
