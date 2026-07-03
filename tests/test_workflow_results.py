import math
import unittest

import numpy as np

from admet.workflows.results import (
    calculate_poisson,
    chi_squared,
    compute_sample_stats,
    observed_counts,
    size_distribution,
)


class WorkflowResultStatsTests(unittest.TestCase):
    def test_compute_sample_stats_matches_ported_formulas(self):
        rows = [
            {"diameter_um": 10.0, "inclusions": 0},
            {"diameter_um": 12.0, "inclusions": 1},
            {"diameter_um": 14.0, "inclusions": 2},
        ]

        stats = compute_sample_stats(
            "sample",
            rows,
            bead_count=6.5e5,
            dilution=1000,
        )

        self.assertEqual(stats["label"], "sample")
        self.assertEqual(stats["total_droplets"], 3)
        self.assertEqual(stats["total_inclusions"], 3)
        self.assertEqual(stats["with_inclusions"], 2)
        self.assertAlmostEqual(stats["mean_d"], 12.0)
        self.assertAlmostEqual(stats["median_d"], 12.0)
        self.assertAlmostEqual(stats["std_d"], 2.0)
        self.assertAlmostEqual(stats["cv"], 100.0 / 6.0)

    def test_calculate_poisson_uses_median_droplet_volume(self):
        x_range, pmf, lambda_val = calculate_poisson(
            [0, 1, 2],
            12.0,
            bead_count=6.5e5,
            dilution=1000,
        )

        expected_volume_ml = (4.0 / 3.0) * math.pi * (6.0**3) * 1e-9
        expected_lambda = (6.5e5 / (1000 * 2.0)) * expected_volume_ml
        self.assertTrue(np.array_equal(x_range, np.arange(0, 6)))
        self.assertAlmostEqual(lambda_val, expected_lambda)
        self.assertAlmostEqual(pmf[0], math.exp(-expected_lambda))

    def test_chi_squared_rescales_expected_counts(self):
        chi2, p_value = chi_squared({0: 50, 1: 30, 2: 20}, [0.5, 0.3, 0.2], 100)

        self.assertAlmostEqual(chi2, 0.0)
        if p_value is not None:
            self.assertAlmostEqual(p_value, 1.0)

    def test_observed_counts_and_size_distribution(self):
        self.assertEqual(observed_counts([2, 1, 2, 0]), {0: 1, 1: 1, 2: 2})

        counts, edges = size_distribution([10.0, 11.0, 12.0], bins=[10.0, 11.0, 12.0])
        self.assertTrue(np.array_equal(counts, np.array([1, 2])))
        self.assertTrue(np.array_equal(edges, np.array([10.0, 11.0, 12.0])))


if __name__ == "__main__":
    unittest.main()
