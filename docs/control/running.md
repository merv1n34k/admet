# Running protocols

Every experiment is a [JSON protocol](../protocols/format). In `admet control` you
add one with **+ Protocol step**, pick a template, set its parameters, review the
plan and execute it.

## Plan, review, execute

1. **Select** a protocol from the selector. Its parameters appear in the
   *Parameter / Value* table; the JSON can be edited too.
2. **Plan** resolves the parameters and freezes the steps. Nothing moves and
   nothing is recorded. The plan table shows, per step:
   **STEP · UNIT ID · TYPE · TARGET · TRIGGER & ETA · END · CONFIRM**.
   Lines inside one row run at the same time. The action bar sums the step count,
   nominal duration and liquid use.
3. **Execute** runs exactly that reviewed plan. A changed connection, correction,
   project or parameter needs a new plan first.

Edits and previews stay in memory. Only Execute archives the protocol, its
parameter values and the run.

## During a run

| Control | Effect |
|---|---|
| **Confirm** | Passes the current gate; the step then applies its targets. |
| **Pause** | Sets the protocol's channels to zero and pauses recording. |
| **Resume** | Puts the paused step's targets back; the step continues where it was. |
| **Skip** | Ends the current step and moves on. |
| **Abort** | Stops the run, zeroes its channels and closes the recording. |

A step with a `confirm_message` waits at an inline **gate** before it starts —
for example *"Collection 4: weigh the vessel and place it"*. The gate keeps its
prompt through pause and resume.

Time spent paused does not count against a step's duration. A paused or skipped
step is marked in the run's events, and calculations treat it accordingly.

## Measurements

Protocols can declare values you type in during the run: vessel masses, dead
volumes, a reference viscosity. The **Measurements** table sits under the plan,
becomes editable on Execute and stays editable after the run. Values save to the
run as you type; empty cells stay empty, never zero.

A new run starts with an empty table. To review or correct an earlier run, use
**Calculations**.

## Recording

Every run records fluidics (pressure and flow of each channel) to
`records/fluidics/`. In *Camera + fluidics* mode it also records video. The run's
`summary.json` points at both and holds the rig mapping and corrections in force.
