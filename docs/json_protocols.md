# Saved protocols

The Qt desktop or agent saves JSON protocols in the project. A definition
contains a name, optional parameters/pressure trips, and ordered steps. Saving and planning do
not actuate hardware. Execution always takes the reviewed plan ID.

The Qt Plan button creates one replaceable in-memory preview per experiment tab.
No preview file is written. Editing invalidates the preview; Execute rechecks the
rig and guards, assigns a fresh `run_id`, and only then archives the execution
snapshot and starts recording. New recordings live under
`records/protocols/<run_id>/`; old plan-ID-based archives remain readable.
Internal preview IDs/digests are not experiment history or reusable execution permission.

## Editable parameters in Qt

### Manual run measurements

Declare measurements separately from parameters:

```json
"measurements": {
  "before_mg": {"label": "Vessel before (mg)", "step": 2},
  "after_mg": {"label": "Vessel after (mg)", "step": 2},
  "density": {"label": "Oil density (g/mL)"}
}
```

`step` optionally identifies an expanded, one-based step (each repeat can have its
own fields). There are no defaults: missing values are `null`. The separate table
becomes editable on Execute. Entries save to that run's `measurements.json`; they
never change execution or trigger calculations. The run selector restores earlier
entries for review/correction. Repeating a protocol starts a fresh empty table.
Calculate processes measurements only on request, after the recording finishes.

Declare only the values the operator should edit. Qt renders them in the same
**Parameter / Value** settings table as Priming, separate from the read-only plan.
No new dialog or step-table editing is needed.

```json
{
  "name": "parameter_example",
  "steps": [
    {
      "sensor_setpoints": {"1": "oil_base_flow * 1.5"},
      "trigger_type": "time",
      "trigger_params": {"duration_s": "measurement_s"},
      "timeout_s": "measurement_s + 10",
      "on_complete": "zero",
      "confirm_message": "Confirm M1 mapping before starting."
    },
    {
      "sensor_setpoints": {"1": "oil_base_flow * 2.5"},
      "trigger_type": "time",
      "trigger_params": {"duration_s": "measurement_s"},
      "timeout_s": "measurement_s + 10",
      "on_complete": "zero"
    }
  ],
  "parameters": {
    "oil_base_flow": ["Oil flow rate, µL/min", 5],
    "measurement_s": ["Measurement duration, s", 20]
  }
}
```

Each declaration is `[label, numeric default]`. Values are finite nonnegative
numbers. Type a complete value and press Enter or leave the cell, as in Priming.
Several parameters can be edited before **Plan**. Changing a parameter invalidates
the current preview; it never changes an already executing plan or actuates a device.
Edits and plan previews are memory-only. Execute archives declarations, expressions
and selected `parameter_values` for that run. Closing the app or switching/reopening
projects discards unexecuted edits; Save Project does not save protocol drafts.

Expressions accept declared names, numbers, `+ - * /`, unary signs and parentheses.
They work in flow/pressure setpoints, numeric trigger parameters, timeout, repeat,
and optional pressure limits. Integer fields must resolve to integers. There are
no functions, Python evaluation, attributes, imports or dependency chains between
parameters. Unknown names, division by zero, invalid results and hardware-range
violations are refused. `{parameter_name}` in step names/confirmation text displays
the resolved value. Raw JSON editing remains available.

The archived `protocol.json` preserves the template and chosen values;
`plan.json` preserves exact resolved, expanded steps and the executable digest.
Calculations resolve archived inputs, not current GUI values. Existing plain JSON
files continue to work without parameters.

## Plain definitions

```json
{
  "name": "short_run",
  "pressure_limits_mbar": {"0": 500},
  "steps": [
    {
      "sensor_setpoints": {"0": 10},
      "trigger_type": "volume",
      "trigger_params": {"sensor_index": 0, "target_volume_ul": 5},
      "timeout_s": 60,
      "on_complete": "zero",
      "confirm_message": "Dispense 5 uL at 10 uL/min?"
    }
  ]
}
```

Channel keys are configured channel indices. Flow targets use `sensor_setpoints`
in µL/min; open-loop pressure targets use `pressure_setpoints` in mbar. Several
channels in one step run concurrently. A channel cannot have both modes in one
step. `pressure_limits_mbar` is optional; omitted or `{}` means software pressure
trips are off. Entries enable trips only for the specified channels, must be
positive, and must not exceed the detected controller maximum. Pressure targets
must stay below any explicitly configured trip. Flow and pressure targets must
remain within detected hardware ranges even when no trips are configured.
There is no fixed 1900 mbar cap and no automatic software overpressure shutdown
on channels without a configured trip. Existing saved limits are preserved.
The supported targets are non-negative; vacuum/reverse-flow protocols are not
part of this version.

Triggers use the existing `time`, `volume`, `stability`, `threshold`, and
`condition` vocabulary shown by `describe run_steps`. `time` takes `duration_s`;
other triggers require an explicit step `timeout_s`. A timeout fails the run and
zeros the channels. Optional `name`, `repeat`, and `group` retain their existing
step meanings. Completion defaults to `zero`; `hold` and `revert` are explicit
choices. Use `confirm_message` to gate a step before its targets are applied.

MCP operations (separate from the template-based Qt workflow):

- `save_protocol`: `{"protocol": <definition>, "replace": false}` saves in the open project.
- `list_protocols`: `{}` lists files; `{"name": "short_run"}` reads one.
- `plan_protocol_file`: `{"path": "/path/to/project.admetp/protocols/short_run.json"}`
  reads and validates a JSON file, then returns an immutable plan.
- `plan_protocol`: `{"operation_id": "run_json_protocol", "settings": {"protocol": <definition>}}`
  plans an inline definition using exactly the same path.
- `control_protocol`: `{"action": "execute", "plan_id": "plan_…"}` executes after review
  and explicit human approval. Existing confirm/skip/pause/resume/abort actions apply.

Unknown fields, duplicate file keys, invalid numbers, unknown channels, and
out-of-range targets are refused. A saved file can be edited or replaced without
changing an already-created plan. Reload it to create a new plan.

```text
experiment.admetp/
  manifest.json             # project index and run references
  records/protocols/run_…/
    protocol.json
    plan.json
    events.jsonl
    summary.json
    measurements.json       # when declared by the protocol
    calculations/           # results calculated on request
  records/fluidics/…csv
```

Qt does not create or browse a top-level `protocols/` directory. Its selector
uses bundled templates, and edits stay in memory until execution. The older MCP
`save_protocol`/`list_protocols` file library remains separate; existing files
are not deleted or modified by this Qt change.

JSON protocol execution automatically records fluidics. The run summary references
that CSV and contains the plan, settings, digest, rig mapping, corrections, limits,
and outcome. The project manifest registers definitions and run summaries. Plans
on disk are audit records; a new owner never silently restores their permission
to execute. Reopen the definition and create a fresh plan to repeat an experiment.
