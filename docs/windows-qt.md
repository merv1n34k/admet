# Standalone Qt on Windows

This branch reuses the earlier Qt camera/fluidics/correction/plot window and the
current Python service. The desktop owns the devices directly; it does not
require an agent or a background server.

## Install and launch

Install Git and uv in PowerShell if needed:

```powershell
winget install --id Git.Git -e --source winget
winget install --id astral-sh.uv -e --source winget
```

Close and reopen PowerShell so both commands are on PATH. For a fresh install:

```powershell
git clone --branch feat/windows-qt-protocols https://github.com/merv1n34k/admet.git admet2
cd admet2
uv python install 3.12
uv sync --locked --extra control
uv run --locked --extra control admet qt
```

For an existing clone, finish any run and close ADMET before updating. Preserve
local edits; do not reset or overwrite them to force an update. From the repository:

```powershell
git fetch origin
git switch feat/windows-qt-protocols
git pull --ff-only origin feat/windows-qt-protocols
uv sync --locked --extra control
uv run --locked --extra control admet qt
```

The project uses Python 3.12 and the normal `.venv`. The control extra includes
PySide6, pyqtgraph, OpenCV, and pypylon; analysis/ML extras are not needed.
The first sync needs network access. Once installed, running the desktop needs
no chat session or agent.

Launch from PowerShell:

```powershell
uv run --locked --extra control admet qt
uv run --locked --extra control admet qt --project "D:\Experiments\today.admetp"
```

The launcher has no mode flags. Step 2 restores the **Simulated Hardware** selector
for Fluigent (False = real hardware, True = simulation). Choose before connecting;
the selector is locked while connected. It does not change the camera backend.
Startup does not connect devices. Click Connect when the physical setup is ready.
Simulation remains an internal test fixture, not a desktop operating mode.

For live use, install the Fluigent and Basler device drivers appropriate to the
instrument. The repository includes Fluigent SDK Windows x64/x86 libraries and
selects by platform. If using an external SDK, `ADMET_FLUIGENT_SDK_PATH` points
to its Python directory containing `Fluigent/SDK`. Never copy macOS `.venv` to
Windows: sync a fresh environment there.

Only one desktop instance per user is allowed. Do not also run Python hardware
control or vendor software against the same devices. The desktop
lock does not coordinate with those other programs.

## Operate without an agent

In the existing **Setup parameters table**, choose **Acquisition: Fluidics only**
(default) or **Camera + fluidics**.
The choice is saved in the project. Fluidics-only needs no camera and records CSV;
it hides the camera preview so only the fluidics graphs occupy the display.
Combined acquisition requires a live camera and uses the existing paired video/CSV
recorder. The mode is frozen into each reviewed plan and saved run. Changing it
discards unexecuted previews; replan before execution. Switching is disabled while
a protocol or recording is active.

1. Use the existing project controls to create/open an `.admetp` project.
   The project menu shows saved protocol/analysis run counts
   and the last time you opened each project on this computer. Projects opened
   outside the default folder remain in the menu after restarting. At startup,
   missing project paths (or paths without a manifest) are removed from this local
   history only; no project data is deleted. Unknown last-opened times show **—**.
2. Select and connect the camera if needed; enable Live for preview.
3. Select Fluigent, connect, then on **Calibration** set each channel's liquid,
   correction factors and dead volume, and apply them. The device panels and live plots are restored from the Qt UI.
4. The TOC keeps **Priming** and **Wash** as fixed stages. Use **+ Protocol step**
   to insert custom experiments between them. Each experiment has a **Protocol**
   section between the existing action bar and graphs/camera: select a saved
   definition or template and **Open**, **Import JSON**, or **Edit JSON**.
   **Save JSON** validates and stores it in the project. Saved custom TOC order
   and protocol references are restored when you reopen the project.
5. **Plan**, in the existing action bar, freezes the definition without setting any channel,
   starting a protocol, or recording.
6. Review the table: **STEP / UNIT ID / TYPE / TARGET / TRIGGER & ETA / END /
   CONFIRM**. Each row is one step; aligned unit/type/target lines inside that row
   execute simultaneously. Human-only gates say **confirm**, not off/time-zero.
   **Before** means confirmation is required before that step applies targets.
   Select a row for the full confirmation, exact trigger and timeout; **Hide details**
   folds the instructions again. This changes only the preview, not the executable steps.
   The existing top action panel summarizes step count, nominal ETA and Oil/Cells/Beads
   consumption from the expanded steps. No extra caption sits above the table.
   ETA excludes operator waiting. Unknown duration or consumption is shown as **—**,
   including pressure-controlled flow and flow held during an operator wait.
7. **Execute** executes that reviewed plan ID only, without an extra approval prompt.
   A changed connection, correction set, project, or safety context requires a
   new plan. Editing JSON or built-in parameters disables execution until you re-plan.
8. When a gate appears, **Confirm** applies that step's targets. **Skip**,
   **Pause**, and **Abort** zero protocol-owned channels. **Resume** resumes a paused run.
   Read the event log and monitor the rig throughout the run.

The protocol's own confirmation gates are the only run confirmations. A step
with `confirm_message` waits inline before applying targets; a step without it
runs immediately. Definition replacement, safety reset and closing connected
devices use a focused popup with **Confirm** and **Cancel**. Cancel is the default;
Escape or closing the popup cancels without performing the action. Protocol gates
remain inline and never open a confirmation popup.

Manual flow/pressure inputs apply a typed target only on **Enter**; decimal values
use a dot. **Stop** zeros that channel. User-owned channels remain controllable
while a protocol runs on other channels. Protocol-owned channels are locked while
running; pause the protocol or wait for release before editing them. On resume,
protocol targets take over again. Flow requires applied corrections; detected
ranges, a tripped safety latch and any armed pressure limit remain enforced.
Manual targets have no duration/volume endpoint: supervise them and stop explicitly.
Use bounded JSON steps for reproducible dispensing. Emergency stop and desktop
shutdown zero all channels, including manually controlled ones.
Priming and Wash keep their original step sequences and familiar parameter
forms, using the same guarded JSON plan/recording backend. They have explicit
timeouts. Software pressure trips default to **Off**: leave the optional pressure
trip field blank, or enter a positive threshold in mbar to enable it. There is no
fixed 1900 mbar cap. The editable wash target defaults to 1800 mbar; that target is
not a safety limit. Review these values for the physical rig before execution.

Numeric parameter fields accept free typing and apply a valid value on Enter or
leaving the field, not on each keystroke. Invalid text stays visible with an error;
the previous value is unchanged and cannot be silently used to build a new plan.
Editing a built-in protocol parameter invalidates its preview. Build and review a
new plan before execution. Detected controller and sensor ranges still apply,
including when trips are off. A 2000 mbar pressure target is allowed on a detected
2000 mbar controller when no lower software trip is configured. A pressure target
must remain strictly below an explicitly configured trip.

With trips off, ADMET provides **no software overpressure shutdown**. The plan
shows Off and records which channels have no trip. Step timeouts, emergency stop,
zero-on-completion and protocol confirmation gates remain active. Existing saved
protocols retain any explicit trips; changing a default does not rewrite them.

### Preflight and reusable protocols

**Preflight** contains flow/phase ratios, the tubing map and consumption estimates.
It does not operate devices or change a protocol. **Save Project** preserves the
setup; recorded-experiment processing belongs in **Calculations**.

The Experiment selector includes **density**, **dropseq** and **flow stability scout**.
Select a protocol, edit its parameters or JSON, then Plan and Execute. Edits stay
in memory until execution; the run archives the exact definition and settings.
These protocols use the same recording, safety and confirmation path. Gravimetry
and dead-volume protocols are being redesigned alongside their calculations;
the previous gravimetry and pressure-flow examples are no longer offered.

### Drop-Seq starting protocol

In **Experiment 1**, select **dropseq**, then **Plan** to review it. Its source is
[`dropseq.json`](../templates/dropseq.json). Execute saves the definition with the run.
Priming and Wash remain separate existing stages; this template does not repeat
them or change the calibration or camera settings.

The template follows the existing Drop-Seq defaults for one set and one replicate:

| Unit ID | Control | Target |
|---|---|---|
| 0 — Oil L | Flow | 300 µL/min |
| 1 — Cells M1 | Flow | 40 µL/min |
| 2 — Beads M2 | Flow | 40 µL/min |

All three run simultaneously after the inline start confirmation. The collection
ends at **150 µL measured on Oil L**, then commands all three outputs to zero.
Nominal active duration is **30 seconds**; the hard timeout is **120 seconds**.
The final inline confirmation closes the run and its fluidics recording. Operator
waiting time is additional. At target flows, each aqueous channel contributes
about 20 µL; 150 µL is **not** the total emulsion volume or an independent aqueous
volume target. Actual amounts depend on measured flow.

Pressure trips default **Off**. These are repository defaults, not proof that the
current oil path can sustain 300 µL/min. Review/edit the JSON before executing;
camera video is not automatically recorded by JSON protocols.

Results remain in the normal protocol recordings. The Checkup calculator accepts
entered measurements or historical check records; it does not automatically infer
which arbitrary JSON steps belong to a calibration or fit. The layout is saved;
liquid properties are calculated from the currently selected liquid profiles.

## Storage and recording

### Calculations

The separate **Calculations** TOC page reads finished runs from the current
project. Choose **Oil density** or **Recording summary**, select the recorded run,
then click **Calculate**. It requires no device connection and makes no hardware
calls. **Refresh** finds newly finished runs. The saved-result selector restores
previous calculations after reopening the project; recalculation adds a new result.

Density templates are **density dsurf**, **density evagreen** and **density custom
mix**. All use channel 1 only: 5/15/20 µL/min for 20 seconds per point, one scout
and two opposite height passes.
The files contain ordinary protocol steps, not mandatory analysis metadata.
Density analysis recognizes their explicit height/pass labels and validates them
against the confirmation text; keep these labels intact. Unknown geometry is
refused rather than guessed. Older recordings lacking a polling clock origin
produce an inconclusive density result. A whole-recording summary can still be used.

Results contain source-file hashes, calculator version, per-point statistics,
fit diagnostics and warnings. Missing measurements remain null. Calculations do
not alter raw data, run state or device settings. No automatic calculation runs
on protocol completion, and no calculation is added to Checkup.

### Project files

The schema is unchanged; see [JSON protocols](json_protocols.md). No YAML or
alternate runner is used.

- `manifest.json`: project index, saved setup and run references.
- `records/protocols/<run_id>/`: executed definition, plan, event log and summary.
- `records/protocols/<run_id>/measurements.json`: manual measurements, when declared.
- `records/protocols/<run_id>/calculations/`: separately saved calculation results.
- `records/fluidics/`: CSV recordings referenced by the run summary.

The selector contains bundled templates. JSON edits, parameter edits, custom
experiment TOC entries and plan previews stay in memory. Closing the app or
switching/reopening projects discards them, without a draft-save prompt.
Only execution creates a run archive. Manual run measurements and calculation
results are still saved. Older draft metadata is ignored, not restored.

When a protocol declares measurements, its table sits directly below the steps,
without a separate caption or run selector. Cells are enabled once that run starts
and remain editable after it finishes. Planning another run clears and disables
the table until Execute; it never carries values forward. Previously entered values
remain stored with their run, including empty values as null. Historical runs are
selected in **Calculations**, not in the experiment's measurement table.

JSON runs automatically record fluidics. **Camera + fluidics** also records video
using the existing synchronized recorder. **Fluidics only** records CSV alone;
the mode is a project setting, not a change to the JSON protocol schema.

Copy the entire `.admetp` directory to preserve an experiment. Reopening loads
definitions and past artifacts, not executable old plan IDs: build a fresh plan
for the new session.

## Stop and close

**EMERGENCY STOP** runs independently of the ordinary GUI command queue and
requests zeroing before cleanup. It latches safety. After resolving the cause,
**Reset safety** on the Cleanup stage requires explicit confirmation and the backend's reset checks.
A pressure trip terminates the run and closes recording, rather than pausing it.
Resetting the latch does not resume the run. Inspect the cause and measured
pressures first, then review the partial recording and build a new plan: repeating
a partially completed dispense may deliver additional volume.
Closing the window asks about connected devices, zeros channels, stops the run
and recording, disconnects, and exits only after cleanup. Errors stay visible.

The physical emergency stop remains authoritative. A software stop cannot
guarantee recovery from a blocked vendor SDK, dead USB link, OS crash, or power
loss. Closing the window is not a persistent detach operation.

## Validation

`make test-desktop` exercises the Qt window offscreen, simulated fluidics,
mocked camera compatibility, library/plan/run artifacts, replay/stale refusal,
zero-on-pause/skip/abort, shutdown errors, and independent emergency dispatch.
`make test` runs the complete unittest suite; `make lint` checks the repository.

Development validation was performed on macOS, not on the Windows instrument
PC. Verify driver loading, actual camera preview, channel mapping, correction
application and physical emergency controls on that PC before any experiment.
