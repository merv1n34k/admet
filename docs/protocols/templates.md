# Bundled templates

These protocols ship with ADMET (the `templates/` folder) and appear in the
protocol selector. Edits made in the app stay in memory; the run archives the
version it executed.

## Density

Measures a liquid's density from the hydrostatic pressure at three outlet heights.
At each height the channel runs at 1×, 2× and 3× the base flow; the pressure at
zero flow is found from the line through them. Two passes: low → high, then
high → low.

| Parameter | Default |
|---|---|
| Channel | 1 (M1) |
| Fluid name | dSurf |
| Base flow (1× / 2× / 3×) | 15 µL/min |
| Settling time | 10 s |
| Measurement time | 20 s |
| Outlet heights | 5 / 15 / 25 cm |

The heights run in blocks, and the template itself has no gates: decide how the
outlet gets to each height between blocks — for example, add a `confirm_message`
to the first step of each block. One run takes about 9 minutes of flow and about
270 µL. Calculation: [Fluid density](../calculations/density).

## Gravimetry

Weighs what each flow unit delivers to calibrate its reading. Three flow levels,
run up, down, up — nine collections per unit. Runs one unit or several at once.

| Parameter | Default |
|---|---|
| Flow units | `110` (M1 and M2) — `001` L, `010` M1, `100` M2, `111` all |
| Liquid name | water |
| Lowest flow (all units) | 15 µL/min |
| Working flow on L | 250 µL/min |
| Working flow on M1/M2 | 67 µL/min |
| Nominal volume per collection | 100 µL on the slowest unit |

The levels are the lowest flow, the midpoint, and the working flow. Each
collection runs on time, long enough for the slowest unit to deliver the nominal
volume. One vessel per unit can collect all nine; weigh it before and after each
collection. Calculation: [Gravimetry](../calculations/gravimetry).

::: tip Speed it up
Most of the time is the lowest level: 100 µL at 15 µL/min takes 6.7 minutes.
Set the lowest flow to the bottom of the range you actually use.
:::

## Dead volume

Three gates, nothing else: no flow is set and every channel is left as it is, so
you drive the channel by hand. Measure the dead volume your way, enter it, and
confirm — three times. Calculation: [Dead volume](../calculations/dead-volume).

## Viscosity

Pressure against flow on one channel's unchanged path: low → middle → working →
working → middle → low, settling then averaging at each level.

| Parameter | Default |
|---|---|
| Channel | 1 (M1) |
| Liquid name | oil |
| Lowest flow | 15 µL/min |
| Working flow | 67 µL/min (use 250 on L) |
| Path ID | path-1 |
| Settling / averaging | 20 s / 30 s |

Run a reference liquid on the same path first. Calculation:
[Viscosity](../calculations/viscosity).

## Flow stability scout

A single-height sweep of flows on M1 to find settling and averaging times before
a density run.

| Parameter | Default |
|---|---|
| Oil base flow | 5 µL/min |
| Time per flow point | 60 s |
| Fixed outlet height | 5 cm |

Calculation: [Flow scout](../calculations/other#flow-stability-scout).

## Drop-Seq

One Drop-Seq collection: oil on L at 300 µL/min, cells on M1 and beads on M2 at
40 µL/min each, all at once after a start gate. It stops when L has delivered
150 µL. Edit the step's flows and target to run other conditions.

| Channel | Flow |
|---|---|
| 0 — Oil L | 300 µL/min |
| 1 — Cells M1 | 40 µL/min |
| 2 — Beads M2 | 40 µL/min |
