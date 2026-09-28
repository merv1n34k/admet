# Saved protocols

The agent configures the rig and saves JSON protocols through MCP. A definition
contains only a name, optional pressure trips, and ordered steps. Saving and planning do
not actuate hardware. Execution always takes the reviewed plan ID.

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

MCP operations:

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
  protocols/short_run.json
  plans/plan_….json
  records/protocols/plan_…/
    protocol.json
    plan.json
    events.jsonl
    summary.json
  records/fluidics/…csv
```

JSON protocol execution automatically records fluidics. The run summary references
that CSV and contains the plan, settings, digest, rig mapping, corrections, limits,
and outcome. The project manifest registers definitions and run summaries. Plans
on disk are audit records; a new owner never silently restores their permission
to execute. Reopen the definition and create a fresh plan to repeat an experiment.
