# Oil density: M1 recordings and offline calculation

## First: separate single-height scout

Open **Template · flow stability scout** in an Experiment stage. Its Priming-style
settings table exposes oil base flow (default 5 µL/min), point duration (60 s),
and fixed outlet height (5 cm above the current reservoir surface). Keep the
existing correction configuration; this template does not apply corrections.

Default targets are **5, 15, 20, 30, 45 → 45, 30, 20, 15, 5 µL/min** on M1 only.
Keep the same height throughout. The initial native gate confirms mapping,
height and oil at the outlet. The receiving container must be open to atmosphere,
with the outlet above collected liquid. A closed receiving tube adds accumulating
backpressure and invalidates this experiment. The run starts/ends at M1 zero flow and records all
transients. Each point ends at zero flow before the next target. It lasts 10 minutes
plus confirmation time; nominal consumption is 230 µL excluding prime/transitions.
Actual volume is unverified with a copied correction. With linear scale 2.25,
displayed targets correspond to unscaled IPA readings divided by 2.25, not known
physical oil flow. Zero-flow holds do not guarantee meniscus position or isolation.

After completion, use **Calculations → Flow stability scout → Calculate**.
It uses archived parameters and explicit step associations, never step names.
It compares 5/10/20/30-second windows, seeks a stable tail lasting at least the
window plus 5 seconds, and adds 5 seconds to the worst accepted settling time.
It checks the proposed settling/measurement window itself, not just the quieter
end of the trace. All usable flows must qualify in both directions; at least three
are needed. Two successive **non-overlapping** measurement windows must both be
stable, agree in their mean pressure/flow, and give repeatable fitted intercepts
and resistance in both sweep directions. The shortest qualifying averaging
window is suggested, with a **20-second minimum**. Five and ten seconds remain
diagnostic only, never recommendations for the physical experiment.

The default acquisition stays at 60 seconds per point. Verifying two 30-second
windows after settling needs a longer recording; the calculator reports this
instead of accepting overlapping or incomplete windows. Failure messages distinguish
insufficient duration, unsettled data, and poor fit/repeatability. A curved pressure–flow
relationship is not automatically a request for longer acquisition. Nothing retries automatically.

These versioned screening thresholds are **analysis criteria, not pressure trips
or manufacturer accuracy specifications**:

| Check | Acceptance threshold |
|---|---|
| Pressure sample SD / half-window drift | ≤1.0 / ≤1.0 mbar |
| Flow sample SD | ≤max(0.5 µL/min, 10% of target) |
| Flow half-window drift | ≤max(0.5 µL/min, 5% of target) |
| Mean flow error | ≤max(1 µL/min, 20% of target) |
| Sampling | ≥10 finite pairs; ≥80% coverage; no gap >1 s |
| Pressure–flow fit | Positive slope; R² ≥0.95 |
| Fitted zero-flow pressure P0 bootstrap SD | ≤0.5 mbar in each pass |
| Forward/reverse P0 difference | ≤1.0 mbar |
| Forward/reverse resistance difference | ≤10% |
| Minimum recommended averaging | 20 s |
| Successive windows | Two full non-overlapping windows after settling |
| Successive mean pressure / flow difference | ≤1.0 mbar / ≤max(0.5 µL/min, 5% of target) |
| Successive fit intercept / resistance difference | ≤1.0 mbar / ≤10% |

Scout calculator version 3 accepts mild curvature (0.95 ≤ R² < 0.995) and
forward/reverse intercept offsets between 0.5 and 1 mbar with explicit warnings.
These are practical screening tolerances, not a claim that R² measures density
accuracy. Missing data and gross drift are still rejected. Previously saved
calculations are unchanged; Calculate creates a new versioned result.

Pressure and flow are resampled together in 2-second blocks (200 bootstrap fits,
at least five blocks). This assesses random precision conditional on that block
length, not absolute accuracy or slower autocorrelation. Missing/incomplete data,
pauses, skips and failed steps remain invalid. Results retain every tested window,
rejected flow and reason, along with thresholds. Compare longer windows in the
result table; more averaging need not improve precision when drift dominates.

The scout cannot estimate density at one height. Its accepted density remains
`null`. Review its results and explicitly approve targets/timing before a separate
density protocol; the application does not start one or rewrite production recipes.

### Simulation verification

`tests/test_flow_scout.py` executes the real plan/confirmation/recording pipeline
against explicitly selected Fluigent SDK simulated instruments. Physical discovery
is blocked. Test-only sensor readings add exponential settling, correlated flow
noise, pressure noise/quantization and a deliberately unstable low-flow point.
Only acquisition/trigger/event clocks are accelerated; it retains a full 600-second
virtual recording. Corrections are IPA ×2.25 on M1, density in the plant is 1.6000
g/mL. The test is included in `make test`.

A separate synthetic multi-height test exercises the existing density calculator
with the same imperfect plant at 1.6000 g/mL. Ten random seeds must recover density
within **±1% (1.5840–1.6160 g/mL)**; a strong-drift case must remain inconclusive.
This threshold verifies the software against the stated model, not real-instrument
accuracy. Live recordings must still qualify, and copied oil calibration remains
unverified. No live hardware is accessed by these tests.

## Density recipes after scout review

Open `density` and set the **Oil name** parameter (dSurf, EvaGreen or your mix) in the Experiment
selector. These ordinary JSON templates control **M1/channel 1 only**; normal
execution leaves channels 0 and 2 untouched. Emergency stop remains global.
The recipe does not configure corrections or ask for temperature/extra metadata.

The September 29 open-outlet scout qualifies under version 3 at 7 seconds
settling and 20 seconds averaging. The new templates round settling up to 10 s
and use three well-separated, stable flows: 15/30/45 µL/min. They do not repeat
the separate single-height scout. This selection is based on that oil/path;
another oil still needs its own scout review.

The Qt parameter table exposes base flow (15, with 1x/2x/3x multipliers), settling
(10 s), averaging (20 s), and three heights (5/15/25 cm). Planning resolves those
values into both execution steps and calculation geometry. Each sweep uses the
same three flows. A changed height updates its native confirmation automatically.

## Default recipe

| Segment | Outlet heights above current reservoir surface | Flow points (µL/min) |
|---|---|---|
| Pass 1 | 5, 15, 25 cm | 15, 30, 45 at each height |
| Pass 2 | 25, 15, 5 cm | 45, 30, 15 at each height |

Each point lasts 30 seconds (10 settling + 20 averaging), with a 40-second timeout.
There are 18 measurement points and six native height-confirmation gates.
Active duration is nine minutes plus operator waits. Nominal consumption is
**270 µL per oil** (810 µL for all three), excluding priming and transitions. Without oil-specific flow
calibration this is not a reliable actual-volume budget.

M1 flow is set to zero before each height gate and at completion. The controller
may retain nonzero pressure to balance the oil column; this is not depressurization
or physical isolation. Verify the meniscus stays at the outlet. There is an
initial scout review at the first main-pass gate. Confirm channel mapping and
measured height before every sweep.
Height is relative to the **current oil surface**, not the reservoir bottom or
pickup; account for the falling surface. Keep the height constant within a sweep.

Flow control adjusts pressure to reach its target. Abort for unexpected pressure,
unreachable flow, drift, air entry or changing outlet conditions. Pressure trips
are off by default as requested; step timeouts are not overpressure limits.
Zero pressure does not isolate the path or prevent gravity flow. Keep tubing,
outlet geometry and correction settings unchanged throughout each oil run.

## Calculation

Open **Calculations → Oil density**, choose the finished run and **Calculate**.
No hardware calls are made. New templates carry explicit calculation step/height
associations; calculations validate their resolved settings against the archived
execution steps and native confirmations. Older 21-point recordings, including
protocol-only recipes whose labels carry geometry, remain readable. Bare CSV
recordings without geometry and event timing cannot determine density.

The saved polling-clock origin aligns CSV elapsed time with protocol events.
Discard the declared settling time (default 10 seconds) and average the remaining window,
with a 0.1-second end margin to exclude zero-output transitions. Exclude operator
waits. Legacy embedded scout data are checked but do not enter the regression. For each pass:

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
  and R² ≥ 0.95; both passes available and disagreement ≤ 10% of their mean.
- Paused, skipped, failed, incomplete or unalignable runs are inconclusive.

Failed checks leave accepted density null while retaining diagnostics and
available pass estimates. There is no automatic escalation or repeated dispensing.
Density calculator version 2 retains curvature warnings even when the result passes.
A shared curved flow response may cancel between heights when identical flows
are used, but height-dependent resistance or changing outlet conditions need not;
the scout's acceptance alone does not validate density accuracy.

Every calculation saves a new result under the run's `calculations/` directory,
with source hashes, calculator version, point mapping, averages/SDs and fit
diagnostics. Reopen saved results through the GUI after closing the session.
Copy the entire `.admetp` directory to move an experiment to Windows.
Temperature and other metadata stay in the operator's separate records.
