from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np


def compute_sample_stats(
    label: str,
    records: Iterable[Mapping[str, Any]],
    *,
    use_inclusions: bool = True,
    use_poisson: bool = True,
    bead_count: float = 6.5e5,
    dilution: int = 1000,
) -> dict[str, Any]:
    rows = list(records)
    diameters = _float_column(rows, "diameter_um")
    inclusions = _int_column(rows, "inclusions") if use_inclusions else np.zeros(len(rows), dtype=int)

    mean_d = float(np.mean(diameters)) if len(diameters) else 0.0
    median_d = float(np.median(diameters)) if len(diameters) else 0.0
    std_d = float(np.std(diameters, ddof=1)) if len(diameters) > 1 else 0.0
    cv = (std_d / mean_d * 100.0) if mean_d > 0 else 0.0
    total_droplets = len(rows)
    total_inclusions = int(np.sum(inclusions)) if use_inclusions else 0
    with_inclusions = int(np.sum(inclusions > 0)) if use_inclusions else 0

    stats = {
        "label": label,
        "mean_d": mean_d,
        "median_d": median_d,
        "std_d": std_d,
        "cv": cv,
        "total_droplets": total_droplets,
        "total_inclusions": total_inclusions,
        "with_inclusions": with_inclusions,
        "lambda_val": None,
        "chi2": None,
        "p_value": None,
    }

    if use_inclusions and use_poisson and total_droplets > 0:
        x_range, theoretical, lambda_val = calculate_poisson(
            inclusions,
            median_d,
            bead_count=bead_count,
            dilution=dilution,
        )
        chi2, p_value = chi_squared(observed_counts(inclusions), theoretical, total_droplets)
        stats.update(
            {
                "lambda_val": lambda_val,
                "chi2": chi2,
                "p_value": p_value,
                "poisson_x": x_range,
                "poisson_pmf": theoretical,
            }
        )

    return stats


def calculate_poisson(
    inclusion_counts: Iterable[int],
    median_diameter_um: float,
    *,
    bead_count: float,
    dilution: int,
) -> tuple[np.ndarray, np.ndarray, float]:
    radius_um = median_diameter_um / 2.0
    volume_ml = (4.0 / 3.0) * np.pi * (radius_um**3) * 1e-9
    lambda_val = (bead_count / (dilution * 2.0)) * volume_ml
    counts = np.asarray(list(inclusion_counts), dtype=int)
    max_inc = int(np.max(counts)) + 3 if len(counts) else 3
    x_range = np.arange(0, max_inc + 1)
    theoretical = np.asarray([_poisson_pmf(int(x), lambda_val) for x in x_range])
    return x_range, theoretical, float(lambda_val)


def chi_squared(
    counts: Mapping[int, int],
    theoretical_probs: Iterable[float],
    n_total: int,
) -> tuple[float | None, float | None]:
    probs = np.asarray(list(theoretical_probs), dtype=float)
    observed = []
    expected = []
    for index in sorted(counts):
        if index < len(probs):
            observed.append(counts[index])
            expected.append(probs[index] * n_total)

    obs = np.asarray(observed, dtype=float)
    exp = np.asarray(expected, dtype=float)
    mask = exp >= 5
    if int(np.sum(mask)) < 2:
        return None, None

    obs_f = obs[mask]
    exp_f = exp[mask]
    exp_f = exp_f * (np.sum(obs_f) / np.sum(exp_f))
    chi2 = float(np.sum(((obs_f - exp_f) ** 2) / exp_f))

    try:
        from scipy import stats as scipy_stats
    except ModuleNotFoundError:
        return chi2, None

    return chi2, float(scipy_stats.chisquare(obs_f, exp_f).pvalue)


def observed_counts(inclusion_counts: Iterable[int]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for value in inclusion_counts:
        key = int(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def size_distribution(
    diameters_um: Iterable[float],
    *,
    bins: int | Iterable[float] = 25,
) -> tuple[np.ndarray, np.ndarray]:
    values = np.asarray(list(diameters_um), dtype=float)
    return np.histogram(values, bins=bins)


def _float_column(rows: list[Mapping[str, Any]], name: str) -> np.ndarray:
    return np.asarray([float(row[name]) for row in rows], dtype=float)


def _int_column(rows: list[Mapping[str, Any]], name: str) -> np.ndarray:
    return np.asarray([int(row.get(name, 0)) for row in rows], dtype=int)


def _poisson_pmf(k: int, lambda_val: float) -> float:
    if lambda_val <= 0:
        return 1.0 if k == 0 else 0.0
    log_p = -lambda_val + k * math.log(lambda_val) - math.lgamma(k + 1)
    return math.exp(log_p)
