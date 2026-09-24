# admet

Microfluidics acquisition and analysis, arranged around one rule: **one process
owns the instrument.**

```
  MCP client  ──stdio──▶  admet serve  ──▶  Fluigent / camera
                              │
                              ├──▶  runtime/state.json     replaced ~4×/s
                              ├──▶  runtime/events.jsonl   appended on change
                              └──▶  project: raw data, summaries, manifest

  human       ──────────▶  admet watch --runtime <same dir>     read-only
```

The client commands and reads. The human watches the same run from another
terminal, through files that carry no way to command anything. Nothing else
opens the hardware — a second server pointed at the same runtime directory is
refused before it can try.

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
admet watch --runtime PATH [--once]
```

That is the whole command line. Running an experiment is deliberately not here:
it happens over MCP or from Python, both through the same guarded operations. A
terminal command per operation would be a third way to do the same thing, and
the one that drifts.

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

## Watching a run

```sh
admet watch --runtime /tmp/admet-live          # live, q to quit
admet watch --runtime /tmp/admet-live --once   # one frame, for pipes
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

The monitor reads two files and nothing else. Two tests hold that: one parses
its import graph, and one runs `admet watch` as a real process and asserts
neither the service nor any engine module was loaded.

### Manual emergency control

`watch` remains strictly read-only. It displays the persistent owner's PID,
which the operator can use for two fixed OS-level recovery actions when MCP is
unavailable:

```sh
kill -USR1 PID   # emergency-stop activity and keep the owner alive
kill -TERM PID   # safely stop activity, disconnect, publish stopped, and exit
```

Use the exact PID currently shown by `watch`. `SIGKILL` is a last resort because
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
2. The complete immutable plan appears in `observe` and `admet watch` before execution.
3. The AI explains every ordered step, safety limit, confirmation, and abort condition.
4. The AI stops and waits for explicit human approval.
5. The human reviews the read-only TUI and confirms the physical setup.
6. Only after approval does the AI call `execute_protocol_plan` with the `plan_id` alone.
7. `execute_protocol_plan` returns the protocol-start yield. The AI then calls
   `wait_protocol_event`, passing each returned `next_sequence` back as
   `after_sequence`, until the protocol completes or fails. `observe` remains
   available for complete telemetry between milestones.
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
{"plan_id": "plan_…"}
```

Execution returns a `yield` with `reason: "protocol_started"`. Continue with a
bounded long-poll:

```json
{"after_sequence": 17, "timeout_s": 10}
```

Call those arguments with `wait_protocol_event`. It returns for a protocol
start, confirmation gate, step completion/timeout/skip/cancellation/failure,
protocol completion/cancellation/failure, or the requested timeout. Progress
samples do not masquerade as milestones. Always reuse `next_sequence`; cursors
remain monotonic across protocols and reading does not remove events.

The live `watch` TUI adapts its rules and wrapped blocks to the terminal width.
Use the arrow keys or `j`/`k` to scroll one line, Page Up/Page Down to scroll a
page, and Home/End to jump to either edge; the control footer remains pinned.
Its lifecycle keys are `q` to quit only the TUI, uppercase `E` to send the owner
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

The pressure limit is capped at 1900 mbar — the controller tops out at 2000, and
the point is to stop short of it rather than go looking for it.

Nothing flows until the operator answers a question quoting what the
*instrument* reports about channel 0, not what the configuration claims. Answer
with `confirm_protocol`.

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

However the server exits — stdin closing, an exception, `KeyboardInterrupt` —
the rig is stopped before publishing stops, so a failed write can never leave
liquid moving.

## Writing a protocol

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
