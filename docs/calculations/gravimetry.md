# Gravimetry

Calculated from a [gravimetry run](../protocols/templates#gravimetry). Each flow
unit in the run is calculated on its own, from its own recording and its own
vessel.

## Per collection

| Quantity | How |
|---|---|
| Weighed volume | (mass after − mass before) / density |
| Recorded volume | The unit's recorded flow integrated over the collection, plus the flow that keeps arriving after the stop until it settles |
| Factor | weighed / recorded |

Density comes from a saved [fluid density](./density) result for the same liquid,
chosen on the Calculations page, or from the density measurement field.

A collection is **left out** — and listed with the reason — when it was skipped
during the run, or when no mass was entered after it. Clearing a collection's
"after" mass is how to leave out a bad one. A flow level still needs at least two
collections.

A collection is **rejected** (it counts against the result) when its flow did not
settle within 10 s after the stop, its mean flow was more than 20 % off target, or
its masses or recording are invalid.

## Per flow level

The three collections at each flow give the true flow ± SD, the factor, the
repeat CV (must be ≤ 5 %), the difference between up and down passes, and a
repeatability-only 95 % interval when all three are present.

## The flow curve

True flow is fitted against recorded flow over all collections:
**true = slope × recorded + intercept**. The fit needs a positive slope and
R² ≥ 0.95.

## Correction for the Rig table

Each unit suggests the terms to enter on the [Calibration](../control/calibration)
page, so the unit itself reports the weighed flow.

1. The run's own terms are undone to recover each collection's raw table reading.
2. Two corrections are fitted on those raw readings: **scale only** (a·x) and
   **scale + square** (a·x + b·x²).
3. The terms the run used are scored too, as **keep current**.

Each option shows its worst error over the measured flow range. The suggestion is:

- **Keep current** if it is within 1 percentage point of the best option —
  over a narrow range many terms draw nearly the same curve, so a change would
  gain nothing;
- otherwise **scale + square** if a single scale misses by over 2 % somewhere and
  the curve is clearly better;
- otherwise **scale only**.

A unit whose suggestion is *keep current* reads right with the terms it ran with.

## Status

**usable** when every flow level has its collections, the repeat CV and the fit
pass, and no collection was rejected; otherwise **inconclusive**. With several
units, the run is usable only when every unit is.

::: warning Prime the outlet
A first collection that comes up short usually means the line after the sensor
was not full. Run the unit to waste until drops come steadily before collection 1.
:::
