# Fluid density

Calculated from a [density run](../protocols/templates#density).

## Method

At each outlet height, the channel runs at three flows. Pressure rises in a
straight line with flow; where that line meets zero flow is the **static
pressure** needed to lift the liquid to that height:

- per height: fit **P = P₀ + R·Q** → P₀ is the static pressure;
- per pass: fit **P₀ against height** → the slope is ρ·g, so
  **density = slope / 0.980665** (mbar per cm per g/mL).

Each pass needs all three heights, and each height all three of its flows. The
reported density is the mean of the two passes, and the passes must agree within
10 %.

With equally spaced heights, the density depends only on the lowest and highest
height; the middle one checks that the line is straight.

## Which samples count

For each flow step, the first `settling_s` seconds are skipped and the rest of
the step is averaged. A step is rejected as **unsettled** when, over that window:

| Check | Pressure | Flow |
|---|---|---|
| Noise (standard deviation) | > 1 mbar | > max(0.5 µL/min, 10 % of the flow) |
| Drift (second half − first half) | > 1 mbar | same as noise |

A step is also rejected if its mean flow is more than max(1 µL/min, 20 %) off the
target, or if samples are missing or have gaps over a second. A run with a pause,
skip or failed step cannot pass.

## Result

| Value | Meaning |
|---|---|
| Density | Mean of both passes, g/mL; empty unless both passes are usable |
| Passes | Each pass's density and its pressure-against-height R² |
| Points | Each step's mean flow and pressure, flow noise, pressure drift and any issue |

Status is **consistent** when both passes are usable, agree within 10 % and no
issue remains; otherwise **inconclusive**, with the reasons per step.

::: tip Reading a failed pass
One rejected step removes its height, and one missing height removes the pass.
Look at the step's noise and drift in the Points table: values just over the
limit point to a short disturbance; values far over it point to an unstable path
(air, a leftover plug of another liquid, an outlet drop).
:::

## Good practice

- Wash well between liquids. Oil and water interfaces left in the line add a
  pressure that changes over time.
- Put the outlet at exactly the same heights in both passes; for water, 1 cm of
  height is about 1 mbar.
- Use the result as the density reference for [gravimetry](./gravimetry) of the
  same liquid.
