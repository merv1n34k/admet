# admet

## Standalone Windows Qt branch

`feat/windows-qt-protocols` restores the previous Qt device/camera window and
adds the validated JSON protocol workflow. No agent, MCP connection, terminal
interface, or server setup is needed.

```powershell
uv sync --locked --extra control
.\start-admet.cmd
```

With the project environment activated, open the app with `admet qt`
(optionally `--project PATH`). Without activation, use `uv run --extra control admet qt`.

The desktop defaults to real devices. Step 2 has a **Simulated Hardware** selector
for Fluigent; choose before connecting. This does not simulate the camera.
Devices remain disconnected until you click Connect. Create/open an
`.admetp` project, connect devices, apply corrections, then use the original
TOC: **Priming → Checkup / chip layout → Experiment steps → Wash**. Add experiments with **+ Protocol
step**. Select/import JSON and review the plan directly between the existing
action bar and graphs/camera; use **Plan** and **Execute** in that action bar.
Confirmation gates, pause/skip/abort, emergency stop, and run artifacts use the
existing backend. Closing the window stops and disconnects; it does not detach.
Execute uses the protocol's own confirmation gates, without an extra approval prompt.

Checkup is a non-actuating design/calculation page; **Save Project** preserves
the layout and entered readings. Gravimetry and pressure/flow checks are editable
JSON templates in the protocol selector, not fixed experiment stages.

**Calculations** is a separate TOC section: select **Oil density** or **Recording
summary**, select a finished recorded run, then **Calculate**. It reads archived
files, never operates devices, and saves each result separately under
`records/protocols/<plan_id>/calculations/`. Saved results are available after
reopening the project; Checkup and protocol execution do not run calculations.
Density uses the confirmed density templates' height/pass labels and recorded
step times (10 seconds settling, then sampling). Keep those labels unchanged;
arbitrary CSV files without geometry and step timing cannot establish density.
Older recordings without the saved polling clock origin return an inconclusive
density result rather than guessing measurement windows. The calculation
selector is backed by `workflows/calculations.py`; additional calculators can
register there without adding experiment-specific TOC pages.

See [Windows desktop setup and workflow](docs/windows-qt.md). Desktop operation
is covered by offscreen Qt, simulated fluidics, and mocked camera tests on macOS;
Windows drivers and physical devices still need acceptance testing on the target PC.

## Inherited headless interface

The existing headless implementation below is retained for compatibility and
regression testing. It is not part of the standalone desktop launch path.
Never run the desktop and a headless owner against the same instrument.

Microfluidics acquisition and analysis, arranged around one rule: **one process
owns the instrument.**

```
  MCP client  ──stdio──▶  admet serve  ──▶  Fluigent / camera
                              │
                              ├──▶  runtime/state.json     replaced ~4×/s
                              ├──▶  runtime/events.jsonl   appended on change
                              └──▶  project: raw data, summaries, manifest

  human       ──────────▶  admet control --runtime <same dir> ──MCP──▶ owner
```

The agent configures the rig and prepares experiments. The human attaches from
another terminal to review plans, execute, and control the run. Both clients use
the same guarded MCP operations; only the owner opens hardware.

With `--runtime`, `serve` is split internally into a durable hardware owner and
a per-chat stdio MCP relay. Closing a chat detaches only its relay; the owner,
hardware connection, corrections, plans, protocol, safety latch, telemetry and
event cursors remain alive. Running the same `serve` command later reattaches to
that owner after verifying the live/simulated mode, exact project and software
digest. A code update therefore requires an orderly owner restart rather than
silently attaching new relay code to an old in-memory service. Relays pump MCP
traffic in both directions and the owner serves each client independently, so
notifications, large tool lists and a blocked client do not stall other clients.

```sh
make setup      # uv sync --all-extras
make test       # all simulated, no hardware touched
```

## Three commands

```sh
admet describe [TARGET]
admet serve (--simulated | --live) [--project PATH] [--runtime PATH]
admet control --runtime PATH [--once]
```

`control` replaces `watch`; there is no compatibility alias. It attaches to an
already-running owner. Ask the agent to start/configure that owner. Quitting the
terminal detaches without stopping the session. `--once` prints a read-only,
pipe-safe telemetry snapshot without connecting to MCP.

`serve` refuses to start without `--simulated` or `--live`. Real hardware is
never what you get by saying nothing.

## Finding out what exists

```sh
admet describe                      # every operation, and the engines beneath
admet describe validate_oil_capacity   # its settings, and what must be true first
admet describe acquisition          # one engine's actions, and what drives them
```

Every id is a verb then its subject, and the same call is called the same thing
at every level: the operation `connect_fluidics` drives the action
`connect_fluidics`. Each also declares what calling it does:

| kind | meaning |
| --- | --- |
| `read` | answers a question and changes nothing |
| `write` | has finished having its effect when it returns |
| `start` | leaves something running after it returns — poll `observe`, or wait |

## Controlling a run

```sh
admet control --runtime /tmp/admet-live          # attach; q to detach
admet control --runtime /tmp/admet-live --once   # one frame, for pipes
```

```
ADMET  SIMULATED   pid 25966 · up 00:04:12
──────────────────────────────────────────────────────────────────────────────
  process    running   heartbeat 0.1s ago
  project    /runs/oil.admetp
  rig        connected   polling   recording oilcap_20260921
  safety     armed (0 1900.0)
──────────────────────────────────────────────────────────────────────────────
  CH  LABEL     MODE    REQUESTED       PRESSURE mbar         FLOW uL/min   VOLUME  STABLE
  0   Oil L     flow        100.0        742.3 ±2.1         98.40 ±1.40    21.60  yes
  1   Cells M   off           0.0         -0.6 ±0.3          0.00 ±0.00     0.00  no
──────────────────────────────────────────────────────────────────────────────
  protocol   running   step 4/8   sample 100.0 uL/min
             ██████████████░░░░░░░░░░ 60%   outcome running
──────────────────────────────────────────────────────────────────────────────
  validation oilcap_bypass_chip_…   running   target 100.0 uL/min   unclassified
```

A measurement that has not been taken shows as `—`, never `0.0`: on this screen
the two would be indistinguishable. A heartbeat older than three seconds says
`STALE` and that the screen is not current.

The terminal reads telemetry files and sends actions over the owner's MCP
socket. Requests run separately from keyboard input. The terminal imports
neither the service nor engines, and cannot create another hardware owner.

| Key | Action |
| --- | --- |
| `O` | Open the project's saved protocol library; arrows select, Enter creates a plan |
| `V` | Review a current plan; Tab cycles through plans |
| `R` | Execute the displayed plan (available in review only) |
| `Y` / `S` | Proceed through a confirmation / skip the current step |
| `P` / `A` | Pause or resume / abort the run |
| `C` | Close the view; the run continues |
| `L` | Open the event log |
| `E` / `X` | Emergency stop / safely shut down the owner |
| `q` / Escape | Back from a detail view; from the dashboard, detach and quit |

Open and review are non-actuating. Execution and confirmation are separate
deliberate actions. The agent continues to manage device connections, corrections,
protocol editing, and server startup. `X` requests orderly zeroing and shutdown;
it does not send `SIGKILL`.

### Manual emergency control

`control` offers `E` for emergency stop and `X` for graceful server shutdown.
These use independent OS signals, so a pending MCP request does not block them.
The displayed PID also permits recovery from a separate shell:

```sh
kill -USR1 PID   # emergency-stop activity and keep the owner alive
kill -TERM PID   # safely stop activity, disconnect, publish stopped, and exit
```

Use the exact PID currently shown by `control`. `SIGKILL` is a last resort because
it cannot run hardware cleanup. If pressure or flow may be unsafe, use the
physical emergency stop first; it remains authoritative.

Manual `set_channel_flow` calls over MCP carry a bounded `control_lease_s`
(10 seconds by default, 60 seconds maximum). The owner gives each channel to
only one MCP client at a time and automatically zeros it when the lease expires
or that client disconnects. Protocol steps retain their separate planned,
guarded lifecycle.

## Over MCP

```sh
admet serve --simulated --runtime /tmp/admet-live \
            --project /runs/oil.admetp --create-project
```

`--project`, `--create-project` and `--runtime` are accepted on either side of
`serve`.

Protocol starts over MCP use an immutable plan boundary. Direct protocol tools
return a refusal telling the client to call `plan_protocol`; engine actions have
no guards and remain available only through the expert Python binding.

The human/AI workflow is deliberately two-phase:

1. The AI calls `plan_protocol` with an existing protocol operation and settings.
2. The complete immutable plan appears in `observe` and `admet control` before execution.
3. The AI explains every ordered step, safety limit, confirmation, and abort condition.
4. The AI stops and waits for explicit human approval.
5. The human reviews the TUI and confirms the physical setup.
6. Only after approval does the AI call `control_protocol` with action `execute`
   and the immutable `plan_id`.
7. Every subsequent interaction uses that same tool. An action such as `confirm`,
   `skip`, `pause`, `resume`, or `abort` is applied and the call waits for the next
   meaningful protocol milestone or its bounded timeout. `wait` advances without
   applying an action. `observe` remains available for complete telemetry between
   milestones.
8. The physical emergency stop remains authoritative.

Planning performs no hardware action: it starts no acquisition, recording,
pressure, or flow. Execution rejects cancelled, replaced, previously executed,
or stale plans when the project, connection, channel mapping, correction,
safety, or hardware identity has changed.

```json
{"operation_id": "validate_oil_capacity",
 "settings": {"configuration": "bypass_chip",
              "flow_targets_ul_min": [50, 100, 150],
              "oil_pressure_trip_mbar": 1900}}
```

Call that object as the arguments to `plan_protocol`, review the returned
`steps`, `required_confirmations`, `armed_safety_limits`, `abort_conditions`,
`unmet_guards`, and `digest`, then execute with:

```json
{"action": "execute", "plan_id": "plan_…", "timeout_s": 10}
```

Execution consumes its internal start event and remains blocked until the next
external milestone, normally a confirmation gate, step outcome, protocol
outcome, or bounded timeout. When a timeout is returned, continue with:

```json
{"action": "wait", "after_sequence": 17, "timeout_s": 10}
```

Call those arguments with `control_protocol`. It returns for a protocol
confirmation gate, step completion/timeout/skip/cancellation/failure,
protocol completion/cancellation/failure, or the requested timeout. Progress
samples do not masquerade as milestones. Always reuse `next_sequence`; cursors
remain monotonic across protocols and reading does not remove events.

The live `control` TUI adapts its rules and wrapped blocks to the terminal width.
Use the arrow keys or `j`/`k` to scroll one line, Page Up/Page Down to scroll a
page, and Home/End to jump to either edge; the control footer remains pinned.
Press `L` to open the complete event log; `q` or Escape returns to the dashboard.
On the dashboard, `q` or Escape quits only the TUI. Uppercase `E` sends the owner
an immediate software emergency-stop signal, and uppercase `X` to ask the owner
to zero the rig, release hardware, and shut down gracefully. Signals are
refused when the published heartbeat is stale. The physical emergency stop
remains authoritative.

**`observe`** is the one read. Unguarded, no arguments, and it takes nothing
from anyone:

```json
{
  "observed_at": "…", "project": {…}, "connection": {"fluidics": true, "simulated": true},
  "polling": true, "recording": {"active": true, "fluidics_csv": "…"},
  "channels": [{"index": 0, "label": "Oil L", "mode": "flow",
                "requested_flow_ul_min": 100.0, "pressure_mbar": 742.3,
                "flow_ul_min": 98.4, "flow_std_ul_min": 1.4, "stable": true,
                "detected": {"sensor_type": "Flow_L_dual", "pressure_max_mbar": 2000.0}}],
  "protocol": {"state": "running", "step_index": 3, "progress": 0.6,
               "confirmation_message": "", "event_sequence": 412},
  "guards": {"corrections": {"met": true, "why_not": ""}},
  "runtime": {…}, "validation": {…}, "safety": {"armed": true, "tripped": false}
}
```

Disconnected it still answers — measurements come back `null` rather than as a
plausible zero. The `guards` section is read from the same place a refusal is
decided, so what it shows and why something is refused cannot disagree.

**`protocol_events`** gives what you have not seen, by sequence:

```json
{"after_sequence": 412, "limit": 50}
```

Poll `observe` for what is true now and `protocol_events` for what happened
between polls. Reading never removes anything, so a client cannot starve the
monitor or the recorder.

## The oil capacity validation

Submit the oil settings through `plan_protocol` as shown above; do not call
`validate_oil_capacity` directly over MCP.

Targets are run exactly as given, each settled then sampled, with a lead-in at
half the first so nothing jumps from zero straight to a target. They are not
sorted or deduplicated for you: a list that does not climb, repeats itself, or
opens above 50 uL/min is refused, because silently rewriting it gives a run that
does not match its own request.

Software pressure trips are optional and off by default. Set
`oil_pressure_trip_mbar` explicitly to enable a trip; there is no fixed 1900 mbar
cap, but a configured threshold must not exceed the detected controller range.
Without a trip this run has no software overpressure shutdown. The 1900 mbar
values in the examples are explicit choices, not a global restriction. JSON
protocols similarly use optional per-channel `pressure_limits_mbar` entries.

Nothing flows until the operator answers a question quoting what the
*instrument* reports about channel 0, not what the configuration claims. Answer
with `control_protocol(action="confirm", after_sequence=<gate sequence>)`. The call
does not return immediately after applying the confirmation: it waits for the next
step or protocol milestone, up to its bounded `timeout_s`.

It returns as soon as it starts; poll `observe` while it runs. The summary lands
in the project under `records/checks/` with per-target mean/std/min/max flow and
pressure, sample counts, whether each settled, flow fraction, and:

| classification | |
| --- | --- |
| `pass` | every target settled and held its flow, under the limit |
| `capacity_limited` | pressure reached the boundary before the flow did |
| `unstable` | pressure had room, but the flow never settled |
| `invalid` | no data, no mapping confirmation, a disconnect, a failed recording |

A run is never called a pass because the protocol thread ended.

## Safety

`emergency_stop` needs nothing to be true first and can be called twice. It
zeroes every channel, then stops the protocol, then closes the recording, each
attempted independently, and latches why.

A pressure watchdog reads *measured* pressure inside the acquisition poll and
trips **at** the limit, not above it — a ceiling you may sit on is not a
ceiling. A setpoint is what was asked for; the failure that matters, a line that
will not flow so the controller pushes harder, shows only in the measurement. It
is independent of the protocol, so a protocol that has hung is still stopped.
The poll itself only zeroes the channels and latches; stopping the protocol and
closing the recording happen off that thread, because blocking there would stop
the very polling the watchdog reads from.

A trip latches. While set, everything that makes liquid move is refused and
`observe` says so; reading, stopping and disconnecting stay available, because a
latch that blocked those would leave you holding a tripped rig with no way to
deal with it. `reset_safety` is refused while anything still reads over its
limit.

Closing a relay's stdin detaches that client; the durable owner continues its
planned run. Orderly owner shutdown attempts to stop the rig before publishing
stops. The physical emergency stop remains authoritative if software or an SDK
cannot complete cleanup.

## Writing a protocol

Prefer reusable JSON files saved in the project's `protocols/` directory.
The agent uses `save_protocol`, `list_protocols`, and `plan_protocol_file` to
save, reopen, and plan them. Each execution records the exact definition, plan,
events, fluidics CSV, and outcome in the `.admetp` project. See the
[JSON protocol guide](docs/json_protocols.md) for the small format and MCP examples.

A protocol is a list of steps; a step holds channels at setpoints until its
trigger fires. Run one you wrote with `run_steps`, without adding it to the
build:

```json
{"steps": [
  {"name": "wet the oil line", "sensor_setpoints": {"0": 5.0},
   "trigger_type": "volume",
   "trigger_params": {"sensor_index": 0, "target_volume_ul": 2.0},
   "on_complete": "hold"},
  {"name": "settle", "sensor_setpoints": {"0": 2.0},
   "trigger_type": "time", "trigger_params": {"duration_s": 30.0},
   "on_complete": "zero"}
]}
```

Triggers: `time`, `volume`, `stability`, `threshold`, `condition`,
`confirmation`. What each needs is read from the trigger itself and reported by
`describe run_steps`, so it cannot drift. On completion: `hold`, `zero`,
`revert`. A step with a `confirm_message` holds before applying any setpoint.

To ship one with the build, write a builder in `workflows/protocols.py` and add
an `Operation` naming it in `operations.py`. It appears in MCP and `describe`
with no other change.

A stability step that gives up is recorded `timed_out`, not `completed` — a
settle that never happened must not read like one that did.

## From Python

`Admet.do("run_steps", ...)` and the other direct protocol operations remain an
intentional expert escape hatch for trusted in-process integrations. The MCP
server alone enforces plan-before-execute; this distinction is deliberate and
covered by tests.

```python
from admet.core.service import Admet

admet = Admet()
admet.create_project("runs/today.admetp")
admet.do("connect_fluidics", {"simulated": True})
admet.do("apply_corrections")
admet.do("run_priming", {"prime_oil_volume_ul": 40.0})
admet.wait_for_protocol()
```

`Admet` owns the session and decides where every file goes — an engine is handed
paths and never chooses its own, so nothing is written outside the open project.
Engine actions stay reachable here as the expert escape hatch:

```python
admet.engine_action("acquisition", "set_camera_settings", {"camera_exposure_us": 3000})
```

## Projects

A `.admetp` directory holding a manifest, recordings and results. A recording
keeps the context it was made under — software version, correction factors, what
the instrument says each channel is, the armed limits — because flows measured
under different corrections are not comparable.

Recording does not require a camera, and does not record video unless
`include_video` asks for it -- a camera that happens to be live is not a request
to record it. A video that was never written is never registered: the manifest
lists only files that exist.

## Layout

```
  src/admet/core/        the Admet service: sessions, paths, routing,
                         runtime telemetry, the monitor
  src/admet/workflows/   protocols, operations, guards, the validation recipe
  src/admet/engines/     the workhorses: acquisition, opencv, cellpose
  src/admet/mcp/         the stdio server, generated from the declarations
  src/admet/app.py       the command line
```

A Qt interface used to live here; it is on the `admet2` branch and can be merged
back. Nothing outside `app.py` imported it.

## Tests

```sh
make test        # unit tests
make test-all    # everything
make lint
```

Every test runs against the Fluigent simulator and the Pylon camera emulator,
and that is enforced rather than assumed. `tests/test_acceptance.py` drives the
whole arrangement over MCP: connect, observe, render the monitor's frame from
the published files, run a validation, answer the operator gate, poll while it
runs, then close stdin and confirm every channel is off.

**Simulation shows the software is consistent, not that a measurement is right.**
Nothing here has been validated on a bench.

## First bench run

Not automated, and not to be run casually:

1. Oil reservoir, Flow Unit L, tubing straight to a safe waste container —
   bypass the chip.
2. Confirm channel 0 is physically Oil L, against what `observe` reports.
3. Confirm the physical emergency stop is reachable. It remains authoritative;
   nothing in this software replaces it.
4. A new project, and a live runtime directory.
5. `[50, 100, 150]` uL/min only, 1900 mbar trip.
6. Watch the TUI while the client polls `observe`.
7. Review the summary before authorising `[175, 200, 225, 250]`.
8. Do not test 300 uL/min or deliberately reproduce the 2 bar ceiling until the
   bypass result has been reviewed.
9. Repeat the approved targets with the chip connected. The difference isolates
   chip and inlet resistance from the reservoir, Flow Unit and tubing.
