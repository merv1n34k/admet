# Viscosity

Calculated from a [viscosity run](../protocols/templates#viscosity): the pressure
needed at each flow on one channel's path.

## Method

For each of the two passes, fit **P = P₀ + R·Q**, where Q is the recorded flow
(already corrected by the flow unit). R is the path's **hydraulic resistance**,
in mbar·min/µL.

- With a usable **reference** run on the same path (same `path_id` and channel),
  the **relative viscosity** is R / R<sub>reference</sub>.
- If that reference run has a known viscosity entered, the **viscosity** is the
  relative viscosity × that value, in mPa·s. Otherwise it stays empty.

The known value is reference input, not something ADMET measured.

## Checks

- Averaging of at least 5 s and 10 samples per point (default: 30 s after 20 s
  settling); only samples inside the window count.
- Positive R and R² ≥ 0.95 in each pass.
- Flow CV and early/late drift of pressure and flow ≤ 5 %.
- The two passes' slopes agree within 10 %; a larger difference is reported as
  hysteresis.

## Assumptions

Newtonian liquid, laminar flow, the same filled path and outlet height for sample
and reference, and comparable temperatures. Matching a path ID does not verify
the physical setup, and the fit errors do not include calibration, geometry or
temperature uncertainty.
