# Calibration

A flow unit measures flow by heat transfer, which depends on the liquid. Each
unit has factory **tables** for water and isopropanol; any other liquid needs a
correction on top. ADMET sends these corrections to the flow unit itself, so the
flow it reports — and regulates to — is the corrected one.

## The Calibration page

The page has three tables.

**Rig** — one row per channel: the **Profile** it uses (only profiles for its flow
unit type are offered), that profile's sensor table and terms (read-only), and
the channel's **dead volume** (tubing from source to chip).

**Profiles** — how a flow unit reads a liquid, saved under a name you choose:

| Column | Meaning |
|---|---|
| Name | e.g. "M1 · Water" or "L · dSurf" |
| Liquid | The liquid the profile is for |
| Flow unit | L or M |
| Sensor table | The flow unit's factory table: `H2O` or `IPA` |
| Scale, Square (x²), Cube (x³) | Terms *a*, *b*, *c* |

The flow unit reports **a·x + b·x² + c·x³**, where *x* is its reading on the
chosen table. A plain scale (b = c = 0) suits liquids whose error is the same at
every flow; the square term bends the curve when the error grows with flow.

**Liquids** — what a liquid is: name, density and viscosity.

Default profiles and liquids are shown first and are locked: editing one adds a
project copy below the defaults with your change. Project profiles and liquids
can be renamed, edited and removed — except a profile a channel uses, or a
liquid a profile uses.

Everything is saved in the project, so a reopened project starts on the same
profiles. **Apply All Corrections** sends each channel's profile to its flow
unit; flow regulation is only available once they are applied.

### What applying checks

- **The table really switched.** A flow unit takes a moment to change tables and
  silently keeps its old table if it does not have the one asked for. ADMET waits
  until each unit reports the requested table and stops with an error naming
  the table it kept otherwise — for example, an L unit has no `Oil` table.
- **The range stays sensible.** A square or cube term would push the range the
  unit reports far beyond its table's own, and flow regulation is tuned to that
  range. With a curve, ADMET caps the range at the table's own.

### Factory tables

| Flow unit | H2O range | IPA range |
|---|---|---|
| M | ±80 µL/min | ±500 µL/min |
| L | ±1 mL/min | ±10 mL/min |

A unit reports a slightly wider range than these (e.g. an M on H2O reports
0–120 µL/min).

## Finding the correction: gravimetry

The terms come from **gravimetry**: weigh what each unit delivers and compare it
with what it recorded. The loop is:

1. Run the [gravimetry template](../protocols/templates#gravimetry) with the
   terms you have (e.g. IPA × 2.25 for an oil on L).
2. **Calculate** gravimetry. Each unit gets a *Correction for a profile* with
   three options — keep current, scale only, scale + square — and their worst
   error over the measured flows. The suggested one is in bold.
3. Enter the suggested terms in a profile (or add one), select it for the channel on the Rig, and **Apply All Corrections**.
4. Run gravimetry again. When the suggestion is **Keep current**, the unit reads
   right with the terms it ran with.

See [Gravimetry](../calculations/gravimetry) for how the terms are fitted.

::: tip Calibrate where you run
Choose gravimetry flows that cover the flows your experiments use. A curve fitted
between 150 and 300 µL/min says nothing about 15 µL/min.
:::

## Dead volume

Measure each channel's dead volume with the [dead volume template](../protocols/templates#dead-volume)
and enter the result in the Rig table's dead volume column.
