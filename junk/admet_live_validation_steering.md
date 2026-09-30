# Steering Instructions: Deliver the Live Validation Loop First

Repository: `/Users/alexeystroganov/.github_projects/admet2`

## Objective

Stop expanding or redesigning the action/protocol architecture. Deliver the smallest safe system that lets:

1. One long-lived ADMET process own the Fluigent connection.
2. An MCP client command that process and inspect current measurements.
3. A human watch the same run in a separate terminal window.
4. Every validation run save raw measurements, events, safety trips, parameters, and a summary.
5. The first real validation measure oil-path hydraulic capacity without intentionally reaching the 2 bar controller ceiling.

The immediate product is a closed experimental feedback loop, not a general workflow language or polished GUI.

## Freeze These Decisions

- Keep the existing `Admet` service, operations, Python protocols, pipeline runtime, and stdio MCP server.
- Do not add YAML, another protocol abstraction, another action registry, a web server, or a Qt/NiceGUI interface in this milestone.
- Do not rename or reorganize existing public concepts again.
- Do not expose a second hardware-owning process.
- Implement a read-only terminal monitor before considering a graphical interface.
- Keep low-level engine tools for diagnostics, but all documented experiment runs must use guarded operations.

## Required Runtime Shape

There must be exactly one controller process:

```text
Codex/MCP client <--stdio--> admet serve <---> Fluigent/camera
                                  |
                                  +--> atomic runtime/state.json
                                  +--> append-only runtime/events.jsonl
                                  +--> project recordings and validation artifacts

Human terminal --> admet control --runtime <same-directory>
```

The current terminal attaches to the owner through MCP for review and run controls.
It must never open the SDK or create an acquisition engine. `control --once`
remains a read-only telemetry snapshot. See README.md for the current workflow;
the milestone requirements below describe the original monitor implementation.

Add a `--runtime PATH` option shared by `serve` and `control`. The serving process must take an exclusive owner lock in that directory. A second serving process must fail clearly rather than opening the same instrument.

## Milestone 1: Observable State

The current acquisition queue is not a valid latest-value API: once its bounded queue fills, new snapshots can be discarded. Fix this without making the MCP layer consume UI queues.

Add thread-safe observation state:

- `AcquisitionThread.latest_snapshot()` returns the newest `DataSnapshot` without removing it.
- Keep a bounded recent-snapshot ring sufficient for short sampling windows.
- `PipelineEngine.latest_event()` returns the newest event without removing it.
- Give pipeline events a monotonically increasing sequence number and wall-clock timestamp.
- Keep a bounded recent-event ring addressable as `events after sequence N`.
- Queue compatibility may remain for existing callers, but observation must not depend on draining a queue.

Add a guarded read operation and MCP tool named `observe` returning one JSON object:

```json
{
  "observed_at": "ISO-8601 timestamp",
  "project": {"open": true, "path": "..."},
  "connection": {"fluidics": true, "simulated": true},
  "channels": [
    {
      "index": 0,
      "label": "Oil L",
      "mode": "flow",
      "requested_flow_ul_min": 100.0,
      "requested_pressure_mbar": 0.0,
      "pressure_mbar": 742.3,
      "flow_ul_min": 98.4,
      "volume_ul": 21.6,
      "stable": true,
      "pressure_mean_mbar": 741.8,
      "pressure_std_mbar": 2.1,
      "flow_mean_ul_min": 98.2,
      "flow_std_ul_min": 1.4
    }
  ],
  "protocol": {
    "state": "running",
    "event_sequence": 42,
    "step_index": 2,
    "total_steps": 8,
    "step_name": "Oil 100 uL/min sample",
    "progress": 0.6,
    "confirmation_message": "",
    "error": ""
  },
  "recording": {"active": true, "id": "..."},
  "safety": {"armed": true, "tripped": false, "reason": ""}
}
```

Disconnected fields should be empty or null, not fabricated zero measurements.

Add `protocol_events` with an optional `after_sequence` and bounded `limit`. It must return typed events, including operator confirmation text, completion, timeout, skip, cancellation, and error outcomes.

## Milestone 2: Runtime Publication and TUI

While `admet serve` is running:

- Atomically replace `<runtime>/state.json` at 2-5 Hz.
- Append state changes, pipeline events, safety events, recording changes, and errors to `<runtime>/events.jsonl`.
- Include process ID, mode (`simulated` or `live`), server start time, project path, and a heartbeat timestamp.
- Mark the runtime state stopped during orderly shutdown.
- A stale heartbeat must be visually obvious to the monitor.

Implement:

```bash
admet control --runtime /tmp/admet-live
```

Use only the standard library and ANSI terminal rendering for this milestone. Do not add a UI framework. Support `--once` for tests and noninteractive inspection.

Display:

- LIVE or SIMULATED mode in a prominent header.
- Connection, polling, recording, project, and safety state.
- One row per channel: label/index, control mode, requested value, pressure, flow, rolling mean and standard deviation, volume, and stability.
- Protocol step, progress, latest event, confirmation request, and error.
- Runtime heartbeat age.
- The current recording and validation artifact paths.
- A permanent message that this monitor is read-only and the physical emergency stop remains authoritative.

Do not attempt keyboard control from the read-only monitor in this milestone.

## Milestone 3: Safety Boundary

Real hardware must become opt-in rather than the default.

- `admet serve --simulated` starts simulation.
- `admet serve --live` starts a server allowed to connect real hardware.
- Starting `serve` without either flag must fail with a clear message.
- The two flags must be mutually exclusive.

Add an always-available, idempotent `emergency_stop` operation/tool that:

1. Calls `ChannelManager.emergency_stop_all()` immediately.
2. Stops the protocol.
3. Stops recording cleanly where possible.
4. Latches a safety event and reason.
5. Does not require project, corrections, camera, or a running protocol.

On MCP stdin EOF, server exception, `KeyboardInterrupt`, or normal server shutdown, execute emergency stop and hardware cleanup in `finally`. Test this with fakes.

Add a pressure watchdog independent of protocol logic:

- It observes current measured pressures, not requested setpoints.
- Limits are explicitly armed for a validation run.
- A limit violation immediately calls the same emergency-stop path.
- A trip is latched and visible through `observe`, the runtime files, and saved results.
- A tripped system cannot restart flow until explicitly reset while all channels read safe.

Do not solve general hardware mapping in this milestone, but show the detected physical channel metadata and require the operator to confirm that channel 0 is Oil L before the oil test starts. Record that confirmation.

## Milestone 4: Fluidics-Only Recording

System validation must not require a camera.

- Permit synchronized fluidics recording when no camera is connected.
- Do not create or register a video file that was not written.
- Return and display the CSV path immediately after recording begins.
- Continue using the project to allocate and register artifacts.
- Record correction parameters, detected channel metadata, software revision, and safety limits with the run.

## Milestone 5: First Validation Recipe

Add one named guarded operation: `validate_oil_capacity`.

This is deliberately a specific recipe, not a generic protocol redesign.

Parameters:

- `configuration`: required text, initially `bypass_chip` or `with_chip`.
- `flow_targets_ul_min`: numeric list, default `[50, 100, 150]` for the first bench run.
- `settle_tolerance_ul_min`: default 5.
- `settle_window_s`: default 5.
- `settle_timeout_s`: default 30.
- `sample_window_s`: default 10.
- `oil_pressure_trip_mbar`: default 1900 and maximum 1900 for the initial milestone.
- `minimum_flow_fraction`: default 0.85.
- `tick_s`: default 0.2.

Requirements:

- Project open.
- Fluidics connected and polling.
- Correction factors applied.
- No other protocol running.
- Operator confirmation that channel 0 is Oil L, the outlet goes safely to waste, and the stated configuration is correct.

Execution:

1. Arm the pressure watchdog at 1900 mbar for the oil channel.
2. Start fluidics-only recording.
3. Set aqueous channels to zero.
4. For every target, create a settle step followed by a fixed sampling step.
5. Never jump directly from zero to the highest target.
6. Stop the protocol and zero all channels on watchdog trip, timeout with high pressure, cancellation, or error.
7. Stop recording in `finally`.
8. Save raw event timestamps so CSV rows can be assigned to target windows.
9. Produce a JSON summary under the project checks/validation records.

Per target, summarize:

- Requested oil flow.
- Mean, standard deviation, minimum, and maximum measured flow.
- Mean, standard deviation, minimum, and maximum pressure.
- Number and duration of samples.
- Settled or timed out.
- Flow fraction: measured mean / requested.
- Safety trip status.

Overall classification:

- `pass`: all targets settle, flow fraction is at least the configured minimum, and pressure remains below the trip limit.
- `capacity_limited`: pressure reaches the safety boundary before flow is achieved.
- `unstable`: pressure remains below the boundary but flow does not settle.
- `invalid`: missing data, wrong mapping confirmation, disconnect, or recording failure.

Do not call a run successful merely because the protocol thread ended.

## Example Acceptance Run: Simulation

The implementation is not complete until this works end to end.

Terminal 1, MCP server owned by the client:

```bash
uv run --directory /Users/alexeystroganov/.github_projects/admet2 \
  admet --project /tmp/admet_oil_loop.admetp --create-project \
  --runtime /tmp/admet_oil_runtime serve --simulated
```

Terminal 2, human monitor:

```bash
uv run --directory /Users/alexeystroganov/.github_projects/admet2 \
  admet control --runtime /tmp/admet_oil_runtime
```

Through MCP:

1. Call `connect_fluidics`.
2. Call `apply_corrections` with explicit simulated test values.
3. Call `observe`; verify three channels and fresh timestamps.
4. Call `validate_oil_capacity` with targets `[5, 10, 20]`, 1-second settle/sample windows, and a simulator-safe pressure limit.
5. Poll `observe` and `protocol_events` while it runs.
6. Verify the TUI changes step and measurements in real time.
7. Verify the protocol completes, channels return to zero, recording closes, CSV exists, summary exists, and the project manifest references produced artifacts only.
8. Close MCP stdin and verify cleanup leaves every simulated pressure at zero and runtime state says stopped.

Add one automated integration test covering this exact loop with short durations.

## First Real Bench Run

Do not run this automatically in tests. After simulation acceptance, document this operator sequence:

1. Use the oil reservoir, Flow Unit L, and tubing routed directly to a safe waste container; bypass the chip.
2. Confirm channel 0 is physically Oil L.
3. Confirm the physical emergency stop is reachable.
4. Use a new project and a live runtime directory.
5. Start with `[50, 100, 150]` uL/min only and a 1900 mbar trip.
6. Watch the TUI while the MCP client polls `observe`.
7. Review the summary before authorizing a second run at `[175, 200, 225, 250]` uL/min.
8. Do not test 300 uL/min or deliberately reproduce the 2 bar ceiling until the bypass result is reviewed.
9. Repeat the same approved targets with the chip connected. The difference isolates chip/inlet resistance from reservoir, Flow Unit, and tubing resistance.

The prior demonstrated failure was approximately 1999.7 mbar with only 236.4 uL/min against a 300 uL/min target on 30 July, and approximately 128 uL/min against 250 uL/min in August. The new recipe must preserve pressure headroom and stop before repeating that saturation condition.

## Tests and Acceptance Criteria

Add tests for:

- Latest snapshots continue updating beyond the old queue capacity.
- Observation never consumes pipeline events needed elsewhere.
- Event sequence and `after_sequence` behavior.
- Atomic runtime state publication.
- `watch --once` rendering from a fixture.
- Single-controller runtime lock.
- Explicit simulated/live startup requirement.
- Emergency stop on EOF, exception, interrupt, and pressure trip.
- Fluidics-only recording with no phantom video artifact.
- Oil-capacity classification for pass, capacity-limited, unstable, and invalid cases.
- Full short simulated acceptance run.

Run:

```bash
uv run -m unittest discover -s tests
uv run ruff check .
```

## Definition of Done

Stop when these are true:

- I can connect the MCP server to one simulated controller process.
- The operator can open `admet control` and see the same process in real time.
- I can call `observe` repeatedly and receive fresh pressure/flow measurements and protocol progress.
- I can retrieve confirmation messages and events without draining another consumer's data.
- A disconnect or overpressure zeros the simulated rig automatically.
- One oil-capacity validation produces raw CSV, event history, and a classified summary.
- Tests and lint pass.

Do not continue into YAML protocols, UI restoration, density measurement, or general experiment builders in this milestone. Report the changed files, exact commands, simulation transcript, and unresolved bench-only risks.
