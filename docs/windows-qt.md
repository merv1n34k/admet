# Standalone Qt on Windows

This branch reuses the earlier Qt camera/fluidics/correction/plot window and the
current Python service. The desktop owns the devices directly; it does not
start or attach to MCP, a terminal interface, or a background server.

## Install and launch

Have Git and uv available on the Windows PC. Obtain this branch, then run from
the repository directory:

```powershell
git switch feat/windows-qt-protocols
uv sync --locked --extra control
.\start-admet.cmd
```

The project uses Python 3.12 and the normal `.venv`. The control extra includes
PySide6, pyqtgraph, OpenCV, and pypylon; analysis/ML extras are not needed.
The first sync needs network access. Once installed, running the desktop needs
no chat session or agent.

Double-click `start-admet.cmd`, or run:

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

Only one desktop instance per user is allowed. Do not also launch a headless
ADMET owner or vendor control software against the same devices. The desktop
lock does not coordinate with those other programs.

## Operate without an agent

1. Use the existing project controls to create/open an `.admetp` project.
2. Select and connect the camera if needed; enable Live for preview.
3. Select Fluigent, connect, then set liquid profiles/correction factors and
   apply them. The device panels and live plots are restored from the Qt UI.
4. The TOC keeps **Priming** and **Wash** as fixed stages. Use **+ Protocol step**
   to insert custom experiments between them. Each experiment has a **Protocol**
   section between the existing action bar and graphs/camera: select a saved
   definition or template and **Open**, **Import JSON**, or **Edit JSON**.
   **Save JSON** validates and stores it in the project. Saved custom TOC order
   and protocol references are restored when you reopen the project.
5. **Plan**, in the existing action bar, freezes the definition without setting any channel,
   starting a protocol, or recording.
6. Review the table: **STEP / UNIT ID / TYPE / TARGET / TRIGGER & ETA / END /
   CONFIRM**. Multiple channel rows with the same STEP execute simultaneously.
   Pressure limits and unmet guards appear above the table; hover over the summary
   for abort conditions, warnings and digest. ETA excludes operator waiting.
7. **Execute** executes that reviewed plan ID only, without an extra approval prompt.
   A changed connection, correction set, project, or safety context requires a
   new plan. Editing JSON or built-in parameters disables execution until you re-plan.
8. When a gate appears, **Confirm** applies that step's targets. **Skip**,
   **Pause**, and **Abort** zero all channels. **Resume** resumes a paused run.
   Read the event log and monitor the rig throughout the run.

The protocol's own confirmation gates are the only run confirmations. A step
with `confirm_message` waits inline before applying targets; a step without it
runs immediately. Definition replacement, safety reset and closing connected
devices use a focused popup with **Confirm** and **Cancel**. Cancel is the default;
Escape or closing the popup cancels without performing the action. Protocol gates
remain inline and never open a confirmation popup.

The old unbounded manual flow/pressure inputs are disabled. Put dispensing
targets in bounded JSON steps with pressure trips and time/volume conditions.
Priming and Wash keep their original step sequences and familiar parameter
forms, using the same guarded JSON plan/recording backend. They have explicit
timeouts and a default 1900 mbar pressure trip. The old 2000 mbar wash hold is
replaced with an editable 1800 mbar default, below that trip and the controller
ceiling. Review these values for the physical rig before execution.

### Checkup and reusable measurement protocols

**Checkup / chip layout** restores the original tubing map, flow split,
resistance fit, gravimetric calculator and consumption estimates. It does not
operate devices or change a protocol. **Save Project** preserves the map and
entered readings, including incomplete rows. Previous check records can be loaded
from the history table; a historical result is not a fresh validation of the setup.
The pressure budget here is a calculation input, not an armed hardware limit.

The Experiment selector includes **Template · gravimetry** and
**Template · pressure flow check**. These are ordinary JSON documents: open,
edit targets/steps, save under your own name, then Plan and Execute. They add no
fixed workflow stages and use the same recording, safety and confirmation path.
Gravimetry starts with one 50 µL dispense per channel at 50 µL/min. The flow-check
example increases Oil L from 50 to 250 µL/min with proportional aqueous targets;
its stability trigger observes Oil L only, with a 30-second timeout per step.
Both examples use 1900 mbar trips and zero outputs at step completion.
They are starting examples, not approval for a physical setup.

Results remain in the normal protocol recordings. The Checkup calculator accepts
entered measurements or historical check records; it does not automatically infer
which arbitrary JSON steps belong to a calibration or fit. The layout is saved;
liquid properties are calculated from the currently selected liquid profiles.

## Storage and recording

The schema is unchanged; see [JSON protocols](json_protocols.md). No YAML or
alternate runner is used.

- `protocols/<name>.json`: reusable definitions.
- `plans/<plan_id>.json`: plan audit records.
- `records/protocols/<plan_id>/`: exact definition, plan, event log and summary.
- `records/fluidics/`: CSV recordings referenced by the run summary.
- `manifest.json`: project definitions and run references.

JSON runs automatically record fluidics. Camera live preview is available;
JSON runs currently do **not** record video. The GUI does not silently turn on
video or change the existing JSON schema.

Copy the entire `.admetp` directory to preserve an experiment. Reopening loads
definitions and past artifacts, not executable old plan IDs: build a fresh plan
for the new session.

## Stop and close

**EMERGENCY STOP** runs independently of the ordinary GUI command queue and
requests zeroing before cleanup. It latches safety. After resolving the cause,
**Reset safety** on the Cleanup stage requires explicit confirmation and the backend's reset checks.
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
