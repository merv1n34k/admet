# Oil density: M1 recordings and offline calculation

Open `density_dsurf`, `density_evagreen`, or `density_custom_mix` in the Experiment
selector. These ordinary JSON templates control **M1/channel 1 only**; normal
execution leaves channels 0 and 2 untouched. Emergency stop remains global.
The recipe does not configure corrections or ask for temperature/extra metadata.

## Confirmed recipe

| Segment | Outlet heights above current reservoir surface | Flow points (µL/min) |
|---|---|---|
| Scout | 5 cm | 5, 15, 20 |
| Pass 1 | 5, 15, 25 cm | 5, 15, 20 at each height |
| Pass 2 | 25, 15, 5 cm | 20, 15, 5 at each height |

Each point lasts 20 seconds with a 25-second timeout. Active duration is seven
minutes plus operator waits. Nominal consumption is **93.3 µL per oil** (280 µL
for all three), excluding priming and transitions. Without oil-specific flow
calibration this is not a reliable actual-volume budget.

M1 pressure is set to zero before each height gate and at completion. There is an
extra review gate after the first scout point. Review the full scout at the first
main-pass gate. Confirm channel mapping and measured height before proceeding.
Height is relative to the **current oil surface**, not the reservoir bottom or
pickup; account for the falling surface. Keep the height constant within a sweep.

Flow control adjusts pressure to reach its target. Abort for unexpected pressure,
unreachable flow, drift, air entry or changing outlet conditions. Pressure trips
are off by default as requested; step timeouts are not overpressure limits.
Zero pressure does not isolate the path or prevent gravity flow. Keep tubing,
outlet geometry and correction settings unchanged throughout each oil run.

## Calculation

Open **Calculations → Oil density**, choose the finished run and **Calculate**.
No hardware calls are made. Imports and execution do not require calculation
metadata. Height/pass labels in the confirmed templates are checked against
confirmation text; keep measurement step labels intact. Bare CSV recordings
without geometry and event timing cannot determine density.

The saved polling-clock origin aligns CSV elapsed time with protocol events.
Discard the first 10 seconds of each point and average the remaining window,
with a 0.1-second end margin to exclude zero-output transitions. Exclude operator
waits. Scout data are checked but do not enter the regression. For each pass:

1. At each height, fit measured pressure against measured flow: `P = R Q + P0`.
2. Fit the three intercepts against height: `P0 = s h + c`.
3. Calculate `density [g/mL] = s [mbar/cm] / 0.980665`.

A constant multiplicative flow correction changes R, not P0. Zero offset,
nonlinearity, drift, pressure gain error and changing meniscus conditions can
still bias density. Constant pressure offset is absorbed by c. Two passes check
repeatability, not absolute accuracy or a robust 95% confidence interval.

## Quality checks and persistence

These are screening heuristics, not instrument specifications:

- At least 10 finite paired readings spanning 80% of the sampling window,
  with no gap above one second. Missing values are not converted to zero.
- Pressure SD and half-window drift ≤ 1 mbar; flow SD and drift ≤ the larger
  of 0.5 µL/min or 10% of the absolute mean.
- Mean flow within the larger of 1 µL/min or 20% of its target.
- Positive pressure/flow slope and R² ≥ 0.95; positive pressure/height slope
  and R² ≥ 0.98; both passes available and disagreement ≤ 10% of their mean.
- Paused, skipped, failed, incomplete or unalignable runs are inconclusive.

Failed checks leave accepted density null while retaining diagnostics and
available pass estimates. There is no automatic escalation or repeated dispensing.

Every calculation saves a new result under the run's `calculations/` directory,
with source hashes, calculator version, point mapping, averages/SDs and fit
diagnostics. Reopen saved results through the GUI after closing the session.
Copy the entire `.admetp` directory to move an experiment to Windows.
Temperature and other metadata stay in the operator's separate records.
