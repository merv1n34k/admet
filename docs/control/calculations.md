# Calculations

The **Calculations** stage turns finished runs into results. It reads the saved
files only; it needs no device and changes no setting on the rig.

## Calculating

1. Choose the **Recorded run** (newest first) and one of the **calculations** its
   protocol declares.
2. If the calculation can use a reference — a density result for gravimetry, a
   reference run for viscosity — pick it explicitly. ADMET never picks the
   latest result for you.
3. Click **Calculate**. The result is saved as a new file under the run's
   `calculations/` folder; earlier results stay and are listed under
   *Saved results for this run*.

If inputs are missing — a measurement not entered, a recording still open — the
page says what is missing instead of calculating.

## Reading a result

Results are shown as sections: a heading with its status, a few key values, then
tables and any issues.

| Status | Meaning |
|---|---|
| usable / consistent / complete | The result passed its checks |
| inconclusive | Something failed a check; the issues say what, step by step |

A result is marked **outdated** when one of its inputs changed after it was
calculated — a corrected mass, for example. Calculate again to refresh it.

## Available calculations

| Calculation | Declared by | Page |
|---|---|---|
| Fluid density | density | [Fluid density](../calculations/density) |
| Gravimetry | gravimetry | [Gravimetry](../calculations/gravimetry) |
| Dead volume | dead_volume | [Dead volume](../calculations/dead-volume) |
| Viscosity | viscosity | [Viscosity](../calculations/viscosity) |
| Flow stability scout | flow_stability_scout | [Flow scout](../calculations/other#flow-stability-scout) |
| Recording summary | any run | [Recording summary](../calculations/other#recording-summary) |
