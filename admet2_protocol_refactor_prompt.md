# Prompt: Refactor ADMET2 into a Safe Markup-Driven Experiment Platform

You are a senior Python architect and laboratory-automation engineer working in this repository:

`/Users/alexeystroganov/.github_projects/admet2`

Your task is to add a safe, versioned, markup-driven experiment protocol system without destabilizing the working Fluigent and camera control code.

## Context

ADMET2 is a Python 3.12 application with two modes:

- `admet control`: PySide6 desktop hardware-control UI.
- `admet analyze`: NiceGUI web analysis UI.

The repository currently has a useful acquisition engine, simulation backends, project persistence, synchronized CSV/video capture, preflight checks, and a substantial passing test suite. Do not rewrite these working components merely for stylistic consistency.

Protocols are currently defined in Python in `src/admet/engines/acquisition/protocol.py`. Control stages are defined in `src/admet/workflows/control.py`, while `src/admet/ui/control.py` contains protocol-name and confirmation-text special cases. An earlier plan explicitly removed pipeline JSON loading and retained code-defined protocols in `junk/master_plan.md`; the product requirement has now changed.

The target user experience is:

1. An operator creates or selects a human-readable protocol file.
2. ADMET2 loads and validates it without modifying Python.
3. The UI renders parameters and a deterministic expanded run preview.
4. Safety and hardware compatibility are checked before any command is sent.
5. The protocol executes using semantic channel names.
6. The run stores the exact protocol, structured events, raw samples, summaries, and recording references.
7. Registered trusted analyzers can consume those artifacts.

## First actions

Before editing:

1. Read all applicable `AGENTS.md` files.
2. Inspect `pyproject.toml`, the acquisition engine, workflow definitions, project storage, both UIs, and relevant tests.
3. Run the current focused and full simulated test suites to establish a baseline.
4. Write a short design note and phased migration plan. Identify invariants that must remain true.
5. Do not operate real hardware. All automated tests must force simulation/fake backends.

Do not make a giant one-shot rewrite. Work in independently testable phases. Preserve current behavior until each built-in protocol is migrated and covered by regression tests.

## Required architecture

Create a protocol domain outside the hardware engine, for example under:

`src/admet/workflows/protocols/`

Use modules with clear responsibilities, such as:

- `model.py`: typed protocol, parameter, step, action, result, and event models.
- `loader.py`: safe YAML loading and protocol discovery.
- `validator.py`: schema, semantic, capability, and safety validation.
- `compiler.py`: parameter binding and deterministic loop/matrix expansion.
- `runtime.py`: execution orchestration or an adapter to the existing pipeline engine.
- `artifacts.py`: protocol snapshot, events, samples, and summaries.

Names may differ if the codebase suggests a better decomposition, but protocol ownership must not remain in the low-level acquisition engine.

Use a versioned YAML format with `protocol_version: 1`. Use safe parsing only. No `eval`, `exec`, arbitrary Python imports, arbitrary Jinja expressions, or user-supplied code execution. If substitutions are supported, implement a deliberately small typed expression/substitution language and reject unknown constructs.

Support:

- Packaged built-in protocols.
- User protocol directories.
- Project-local protocol imports.
- An immutable copy of the exact resolved protocol used for every run.
- Stable protocol IDs distinct from display names.
- Protocol content hashing.
- Clear errors for unknown fields and unsupported protocol versions.

## Hardware identity and safety

Fix hardware identity before allowing user protocols to actuate channels.

Protocol files must refer to semantic names such as `oil`, `cells`, and `beads`, never SDK list indices. Resolve semantic names through an explicit persisted hardware map.

The map must include all stable identifiers exposed by the SDK, such as controller/device identity, serial, position, channel type, and measurement/control ranges. On connection:

- Verify the saved mapping against detected hardware.
- Refuse automatic actuation on ambiguous or changed mappings.
- Present a clear operator confirmation/remapping flow.
- Store the verified map with the run artifacts.

Before execution, validate every expanded setpoint against:

- The connected hardware's actual pressure and flow ranges.
- Protocol-declared limits.
- Project or site safety limits.
- Optional configurable operating headroom below absolute limits.
- Available sensors and actuators.
- Required camera or recording capabilities.

Unknown, non-finite, out-of-range, or type-invalid values must fail before hardware calls. Preserve emergency stop independently of protocol state. Define safe `on_error`, cancellation, and finalization behavior so interrupted protocols leave channels in an explicit safe state.

## Protocol primitives

Implement a minimal composable v1 set rather than encoding each experiment as a new action. It must cover:

- `set_flow`
- `set_pressure`
- `stop_channel` or `zero_channel`
- `wait_time`
- `wait_stable` for one or more selected channels
- `wait_condition` using a bounded, safe condition grammar
- `prompt` or `confirm`
- `operator_input` with typed values, units, bounds, and required/optional status
- `capture_window` with duration, sample source, and summaries such as mean, standard deviation, minimum, maximum, count, and timestamps
- `start_recording`
- `stop_recording`
- `repeat`
- `foreach`
- deterministic matrix/sweep expansion
- labels and parameter substitution
- explicit `on_complete`, `on_error`, and final safety actions

Every wait with a timeout must report whether it completed normally or timed out. Timeout must not silently look like successful settling.

If adaptive branching is not safe for v1, reject it clearly and support deterministic sweeps first. Do not pretend unsupported behavior works.

## Structured execution events

Replace name- and text-based behavior with typed events. Each step event must include at least:

- Run ID and protocol ID/version/hash.
- Expanded step ID and source step ID.
- Step type.
- Start and end timestamps.
- Outcome: `completed`, `skipped`, `timed_out`, `cancelled`, or `error`.
- Resolved setpoints and semantic-to-physical channel mapping.
- Operator inputs.
- Capture-window summaries and raw-data references.
- Recording IDs and file references.
- Error details where applicable.

Recording must be controlled by explicit actions/events. Remove reliance on `_run_start_label` and `_run_complete_label` in `src/admet/ui/control.py`.

Replace the Characterise-specific `_capture_sweep_point` behavior with generic `capture_window` artifacts. Downstream characterization code should consume structured protocol results rather than inspect a pipeline name.

## Persistence

For each run, persist a structure equivalent to:

```text
records/protocols/<run_id>/
  protocol.yaml
  resolved_protocol.yaml
  events.jsonl
  samples.csv
  summary.json
```

Integrate it with the existing project manifest rather than creating an unrelated storage island. Writes should be atomic where practical. Existing projects must remain readable. If a manifest migration is needed, make it explicit, versioned, tested, and backward compatible.

Store enough provenance to reproduce interpretation later:

- Application version and source revision when available.
- Protocol version and content hash.
- Bound parameters.
- Verified hardware map and capability ranges.
- Liquid/correction profile identity and content hash.
- Camera settings when used.
- Timestamps and units.

## UI requirements

Do not replace PySide6 or NiceGUI as part of this task. Decouple protocol behavior from widgets first.

Add to the control UI:

- Protocol discovery, import, and selection.
- Reload/refresh after adding a protocol file.
- Validation report with errors and warnings separated.
- Parameter form generated from typed protocol parameter definitions.
- Expanded step/sweep preview before execution.
- Estimated duration and liquid consumption where calculable.
- Required capabilities and mapped channels.
- Prominent safety limits and headroom warnings.
- Explicit recording state and artifact location.
- Run, pause, resume, skip, and stop controls driven by runtime state.
- Optional camera area shown only when the protocol requires or enables camera use.

Do not branch on protocol display names, stage IDs, or human-facing prompt strings to implement behavior. UI text must be freely editable without changing semantics.

Keep UI changes focused. Also fix obvious protocol-run usability issues encountered in the touched views, such as clipped instruction text and misleading empty plot ranges, but do not launch a general redesign.

## Built-in migration

Preserve and migrate current behavior for:

- Priming
- Drop-Seq
- Wash
- Characterise
- Gravimetry

Start with the simplest protocol. An adapter to the current `PipelineEngine` is acceptable during migration. Remove old Python definitions only after all migrated protocols have equivalent simulated end-to-end coverage.

If some behavior cannot be represented generically, define a narrow, trusted, registered extension point with a typed interface. Do not permit protocol files to import or name arbitrary Python callables.

## Hydrostatic oil-density acceptance protocol

Add a protocol file demonstrating that a scientifically useful new experiment can be introduced without changing Python protocol code.

Purpose: determine oil density from the hydrostatic pressure offset measured through the Fluigent setup, and compare a candidate generation oil to EvaGreen generation oil.

Required protocol behavior:

1. Select the verified semantic `oil` channel.
2. Collect metadata: oil identity, lot, temperature, tubing ID/length, outlet configuration, correction profile, and reference/candidate status.
3. For each configured height condition, prompt the operator to set the reservoir/outlet geometry and enter the actual signed height difference in centimeters.
4. Execute a configurable pressure sweep around the expected balance point, with both ascending and descending order represented across repetitions.
5. At every pressure point, wait for settling with an explicit timeout result.
6. Capture a fixed-duration pressure and flow window, including raw references and summary statistics.
7. Return the channel to a safe state between height conditions and on every error/cancellation path.
8. Persist all run inputs and outputs through the generic protocol artifact system.

Implement density analysis as a trusted registered analyzer, not embedded arbitrary YAML code:

1. Fit flow against applied pressure for each height and estimate the zero-flow pressure with uncertainty and fit diagnostics.
2. Reject or flag poorly settled, timed-out, undersampled, nonlinear, or low-quality fits.
3. Fit zero-flow pressure against signed height.
4. Calculate density using:

   `density_g_per_mL = slope_mbar_per_cm / 0.980665`

5. Report candidate/reference relative density with propagated uncertainty.
6. Preserve the data needed to independently repeat the calculation.

Do not hardcode the expected oil density as the answer. Do not rely on the current correction factor being valid near zero flow without recording that assumption and validating direction/hysteresis behavior.

An illustrative YAML shape is below. Improve it as needed while preserving the requirements:

```yaml
protocol_version: 1
id: hydrostatic_oil_density
name: Hydrostatic oil-density measurement
capabilities:
  channels: [oil]
  camera: false
parameters:
  oil_id:
    type: string
    required: true
  heights_cm:
    type: list[number]
    default: [-20, -10, 0, 10, 20]
  pressure_offsets_mbar:
    type: list[number]
    default: [-40, -20, -10, 0, 10, 20, 40]
  settle_seconds:
    type: number
    unit: s
    default: 5
    minimum: 1
  sample_seconds:
    type: number
    unit: s
    default: 30
    minimum: 5
safety:
  on_error:
    - stop_channel: oil
  finally:
    - stop_channel: oil
steps:
  - foreach:
      variable: nominal_height_cm
      values: ${heights_cm}
      steps:
        - operator_input:
            id: actual_height_cm
            type: number
            unit: cm
            prompt: Enter the measured signed vertical height difference
        - foreach:
            variable: pressure_offset_mbar
            values: ${pressure_offsets_mbar}
            steps:
              - set_pressure:
                  channel: oil
                  value: ${pressure_offset_mbar}
              - wait_stable:
                  channels: [oil]
                  minimum_duration: ${settle_seconds}
                  timeout: 60
              - capture_window:
                  id: hydrostatic_point
                  channels: [oil]
                  duration: ${sample_seconds}
                  fields: [pressure, flow]
                  summaries: [mean, stddev, min, max, count]
        - stop_channel: oil
analysis:
  analyzer: hydrostatic_density_v1
```

The final schema may represent actions differently. The acceptance criterion is behavior and safety, not exact syntax.

## Tests

Add focused tests for:

- YAML parsing and protocol-version handling.
- Rejection of unknown fields, unknown actions, malformed types, and non-finite values.
- Safe substitution without code execution.
- Parameter bounds and units.
- Deterministic repeat/foreach/matrix expansion and stable step IDs.
- Semantic hardware mapping, changed-device detection, and ambiguous mappings.
- Static range and headroom validation before backend calls.
- Explicit recording actions and artifact references.
- Completed versus timed-out stability events.
- Cancellation, error finalization, and emergency-stop independence.
- Atomic artifact persistence and existing-project compatibility.
- Simulated end-to-end execution of every migrated built-in.
- Simulated end-to-end hydrostatic density protocol execution.
- UI presenter/view-model behavior without requiring manual clicks where practical.

Run at minimum:

```bash
uv run -m unittest discover -s tests
uv run ruff check .
```

Use the repository's established commands if they differ. Do not weaken existing tests to make the refactor pass.

## Documentation

The repository currently lacks an adequate top-level guide. Add concise documentation covering:

- Installation and simulated startup.
- Protocol discovery locations and precedence.
- The version-1 YAML schema with examples.
- Parameter types, units, substitutions, loops, and captures.
- Safety validation and channel mapping.
- Run artifacts and analysis integration.
- How to add a new protocol without changing Python.
- How to add a trusted analyzer or extension when generic primitives are insufficient.
- Migration notes for existing projects and built-ins.

## Acceptance criteria

The work is complete only when all of the following are true:

1. A user can add a valid YAML protocol and run it in simulation without modifying Python.
2. The UI discovers it after refresh and generates its parameter form and preview.
3. Invalid or unsafe protocols fail before any hardware command.
4. Semantic channels resolve through a verified, persisted physical map.
5. Recording behavior is explicit and independent of human-readable messages.
6. Every step emits structured status and provenance.
7. The exact source and resolved protocol are stored with the run.
8. Existing built-in protocols retain their behavior under simulation.
9. The hydrostatic oil-density protocol executes end to end and yields analyzable structured data.
10. The full tests and lint pass.

## Delivery expectations

Deliver changes in reviewable phases. For each phase, report:

- Files changed and architectural purpose.
- Compatibility impact.
- Safety implications.
- Tests added and commands run.
- Remaining migration debt.

Prefer clear typed boundaries and deletion of superseded special cases over parallel permanent implementations. Do not claim hardware validation from simulation; explicitly separate software verification from later bench validation.
