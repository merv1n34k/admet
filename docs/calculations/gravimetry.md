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

A collection is **flagged** (it counts against the status) when its flow did not
settle after the stop, or its mean flow was further off target than the limit
allows. It still counts in the values. A collection whose masses or recording are
invalid has no values and is not used.

## Limits

Fixed. A result outside them is inconclusive, which only informs.

| Limit | Value |
|---|---|
| Repeat CV at each flow level | ≤ 5 % |
| Fit R² of the flow curve | ≥ 0.95 |
| Recorded mean flow vs target | within 20 % |
| Flow settles after the stop | within 10 s |

## Per flow level

The three collections at each flow give the true flow ± SD, the factor, the
repeat CV (checked against its limit), the difference between up and down passes, and a
repeatability-only 95 % interval when all three are present.

## The flow curve

True flow is fitted against recorded flow over all collections:
**true = slope × recorded + intercept**. The fit needs a positive slope and an R²
at or above its limit.

## Correction for a profile

Each unit suggests the terms to enter in a profile on the [Calibration](../control/calibration)
page, so the unit itself reports the weighed flow.

1. The run's own terms are undone to recover each collection's raw table reading.
2. Two corrections are fitted on those raw readings: **scale only** (a·x) and
   **scale + square** (a·x + b·x²).
3. The terms the run used are scored too, as **keep current**.

The fit uses the collections whose weighed flow lies in the **fit range**, set in
the parameter table on the Calculations page (*Fit from* / *Fit to*, 0 leaves that
end open). Set it to the flows you work at, so the terms are best there.

Each option shows its worst error over that range. The suggestion is the fit with
the smaller worst error: **scale + square** when it beats scale only by at least
0.1 percentage point, otherwise **scale only**. *Keep current* is shown for
comparison.

The bottom of the result shows the fit worked through for each unit: every
collection's recorded flow, raw reading *x* and weighed flow *y*, whether it is in
the range, the sums, and each formula with its numbers.

The suggestion is only a suggestion: it is calculated whatever the status, and
never applied by itself. An inconclusive unit still gets one; its notes say which
checks it failed.

## Status

**usable** when every flow level has its collections, the repeat CV and the fit
are within their limits, and no collection was flagged; otherwise **inconclusive**. With several
units, the run is usable only when every unit is.

::: warning Prime the outlet
A first collection that comes up short usually means the line after the sensor
was not full. Run the unit to waste until drops come steadily before collection 1.
:::
