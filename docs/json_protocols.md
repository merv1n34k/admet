# Saved protocols

Bundled experiment definitions live in the repository-root `templates/` directory.
They are included as package data when building the Python wheel. Loading templates
does not depend on the terminal's current directory.

Execution archives JSON protocols in the project. A definition
contains a name, optional parameters/pressure trips, and ordered steps. Saving and planning do
not actuate hardware. Execution always takes the reviewed plan ID.

The Qt Plan button creates one replaceable in-memory preview per experiment tab.
No preview file is written. Editing invalidates the preview; Execute rechecks the
rig and guards, assigns a fresh `run_id`, and only then archives the execution
snapshot and starts recording. New recordings live under
`records/protocols/<run_id>/`. Only the current project format is read; older
projects will be converted by a separate migrate command.
Internal preview IDs/digests are not experiment history or reusable execution permission.

## Editable parameters in Qt

### Manual run measurements

Declare measurements separately from parameters:

```json
"measurements": {
  "before_mg": {"label": "Vessel before", "unit": "mg", "step": 2, "required": true},
  "after_mg": {"label": "Vessel after", "unit": "mg", "step": 2, "required": true},
  "density": {"label": "Fluid density", "unit": "g/mL", "min": 0}
}
```

`step` optionally identifies an expanded, one-based step (each repeat can have its
own fields). There are no defaults: missing values are `null`. The separate table
becomes editable on Execute. Entries save to that run's `measurements.json`; they
never change execution or trigger calculations. The run selector restores earlier
entries for review/correction. Repeating a protocol starts a fresh empty table.
Calculate processes measurements only on request, after the recording finishes.
Optional `unit`, `min`, `max` and `required` fields validate measurements.
Supported units: `mg`, `g`, `s`, `uL`, `g/mL`, `mPa.s`, `mm`, `cm`, `1` (dimensionless).
Numbers must be finite; missing entries remain null, including required entries
while the run is in progress. Required inputs block calculation, not execution.

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

Parameters support text, number, boolean and choice declarations:

```json
"parameters": {
  "oil_name": {"type":"text","label":"Oil name","default":"dSurf"},
  "flow": {"type":"number","label":"Flow, µL/min","default":15,"min":0,"max":100},
  "filtered": {"type":"boolean","label":"Filtered","default":true},
  "finish": {"type":"choice","label":"Completion","default":"zero","options":["zero","hold"]}
}
```

Qt renders text/numeric cells, checkboxes and dropdowns in the Parameters table.
Number bounds are optional; values must be finite. Choices must contain distinct
scalar values (text, number or boolean), and retain their native JSON types.
The shorthand `[label, numeric default]` still means a nonnegative number.
Type a complete value and press Enter or leave the cell, as in Priming.
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
violations are refused. Only numeric parameters can enter arithmetic; booleans are
not converted to 0/1. `{parameter_name}` substitutes text in protocol/step names,
confirmation messages, groups, trigger type, completion action and density fluid name.
Resolved fields still pass their ordinary validation (including filename-safe
protocol names and valid trigger/completion choices). Substitution is single-pass;
inserted text is never evaluated. Booleans can label a run but do not conditionally
skip steps. Raw JSON editing remains available.

Channel keys can use a declared numeric choice: `"sensor_setpoints": {"{channel}": "working_flow"}`.
The resolved key must be a valid channel index; duplicate resolved keys are refused.
Use the same parameter in calculation `channel` so the selected sensor and analysis
stay aligned. Changing the channel does not automatically change the flow value:
set both explicitly in the Parameters table.

A step may carry `"units": "{units}"`, a mask read right to left: `001` L, `010` M1,
`100` M2, `110` M1 and M2, `111` all. The step keeps only the setpoints of the units
the mask names, so each unit can still have its own flow or pressure. Its trigger
and timeout may use `step_min_flow`, the lowest flow the step sets after masking.
Measurements marked `"for_each_unit": "units"` are repeated for every unit in the
mask: `{unit}` becomes `ch0`, `ch1`… in keys, and `{unit_name}` the unit's name in
labels. A gravimetry declaration takes `"units": "{units}"` and uses `{unit}` in its
mass bindings.

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

Python API (separate from the template-based Qt workflow):

- `admet.do("save_protocol", {"protocol": definition, "replace": False})` saves in the open project.
- `admet.do("list_protocols")` lists files; pass `{"name": "short_run"}` to read one.
- `admet.plan_protocol_file(path)` validates a JSON file and returns an immutable plan.
- `admet.plan_protocol("run_json_protocol", {"protocol": definition})` plans an inline definition.
- `admet.control_protocol(action="execute", plan_id=plan_id)` executes after review
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
uses bundled templates, and edits stay in memory until execution. The Python
`save_protocol`/`list_protocols` file library remains separate; existing files
are not deleted or modified by this Qt change.

JSON protocol execution automatically records fluidics. The run summary references
that CSV and contains the plan, settings, digest, rig mapping, corrections, limits,
and outcome. The project manifest registers definitions and run summaries. Plans
on disk are audit records; a new session never silently restores their permission
to execute. Reopen the definition and create a fresh plan to repeat an experiment.

## Run calculations

The workflow is **Setup / Preflight → Plan → Execute → enter measurements →
Calculate → repeat experiments → Wash → Cleanup**. Parameters define execution;
measurements are observations only. Neither measurement entry nor calculation
changes instrument setpoints or calibrations.

JSON declares a list of known calculators, with one entry per calculation type:

```json
"calculations": [
  {
    "type": "gravimetry", "units": "010", "liquid": "{liquid_name}",
    "density": "density",
    "samples": [
      {"step": 2, "before": "before_{unit}_mg", "after": "after_{unit}_mg"}
    ]
  },
  {"type": "recording_summary"}
]
```

This illustrates bindings; gravimetry needs the three-pass series (up/down/up).
Use the bundled template as a complete example. References to steps
use expanded, one-based indices; measurement `step` associations must match.
Unknown types/fields, incompatible units, missing step references and invalid
sampling recipes are rejected before execution. `liquid` and viscosity `path_id`
accept text parameters; viscosity `settle_s` accepts numeric parameter expressions.

### Gravimetry

The template runs the units in its `units` mask: `001` L, `010` M1, `100` M2,
`110` M1 and M2 together, or `111` all three. Each unit has its own working flow:
`working_flow_l` (250 µL/min) and `working_flow_m` (67 µL/min). There are three
targets per unit: `low_flow`, the midpoint, and the working flow. The lower target
defaults to 15 µL/min. Run ascending, descending, then ascending: three independent
collections per target, nine total per unit. Each collection runs on time,
`collection_ul × 60 / step_min_flow`, so the slowest unit delivers the nominal
100 µL and faster units more. The budget is 900 µL nominal per M unit, excluding
priming and any separate setup runs. Actual collected volume depends on the
calibration; the calculation uses what each unit recorded.

Each unit has its own vessel and mass fields per collection. One calculation
reads every unit alone, from its own recorded flow and masses, and reports each
unit's result; a missing or bad mass makes only that unit inconclusive.

Enter only each vessel's before/after mass in the measurement table; there is no
mass time-series requirement or new balance UI. Weigh the complete collection.
Supply liquid density manually, or explicitly
select a saved usable density result identifying the same fluid in Calculations.

- True volume (µL) = mass gain (mg) / density (g/mL).
- Recorded volume = trapezoidal integral of recorded flow over the collection step.
- Per-collection multiplier = true volume / recorded volume.
- True and recorded mean flow = respective volume / actual collection time.
- Fit true flow against recorded flow across collections; require R² ≥0.95 and
  positive slope. This is **not** a mass-versus-time R².

Recorded flow already includes the SDK correction. A proposed multiplier of 1.2
means another 20% relative to that recorded flow, **not** replacing SDK scale 2.25
with 1.2. There is no automatic application, and this number is not predefined by
the experiment. Require three valid collections per target and true-flow repeat
CV ≤5%. Mean recorded flow more than 20% from its target is flagged for capacity
or settling investigation. Before/after measurements include startup; they measure
complete-dispense calibration, not a separately isolated steady-state mass slope.

Results show per-target corrections, repeat SD and a repeatability-only 95%
Student-t interval (three independent, approximately normal collections). Direction
differences are reported rather than averaged away; with two ascending passes and
one descending pass they are exploratory, not a precise hysteresis estimate. A
single multiplier is offered only when all factors span ≤5% of their mean;
otherwise use the flow-dependent calibration curve. R² and three repeats alone
do not establish absolute accuracy or guarantee statistical significance. Balance
resolution, density, evaporation and retained droplets add systematic uncertainty.

### Dead volume

Dead volume is measured by hand. The protocol has three confirmation-only steps:
they set no flow and leave every channel as it is (`on_complete: hold`), so you run
the selected channel yourself — manual channel control stays available while the
protocol waits. For each measurement, find the channel's dead volume your own way,
enter it in the **Measurements** table (µL) and confirm.

The calculation reports the mean of the three values, their SD and CV, and a
repeatability-only 95% Student-t interval. Nothing is copied anywhere: enter the
result in the Calibration table's dead volume column yourself.

### Viscosity

Select the channel and the same low/working flow parameters as gravimetry.
The template uses flow control: low → middle → working → working → middle → low,
measuring the pressure needed at each level. Confirm each pass. Each point ends at
zero flow; the final one-second zero-flow step closes out recording coverage.
Keep the filled geometry, outlet height and
identified `path_id` unchanged between sample and reference runs. Temperature must
be comparable; record it separately, not in a mandatory ADMET field.

Fit measured `P = P0 + RQ` separately for the two passes. Q is the recorded flow,
already corrected inside the flow unit by the Calibration stage; no multiplier is
applied on top. Minimum averaging is 5 seconds/10 samples; default is
30 seconds after 20 seconds settling. Only interior measured samples enter the
point statistics, not interpolated boundary values. Required checks are positive R, R² ≥0.95,
flow CV and early/late pressure/flow drift ≤5%, and slope disagreement ≤10%.
These thresholds flag problems; they do not establish absolute accuracy.

Hydraulic resistance R is reported in mbar·min/µL. With a selected usable viscosity
reference from the same path/channel, relative viscosity is `R / R_reference`.
If that reference run has a manually entered known viscosity, absolute viscosity
is the ratio multiplied by that known value. Otherwise absolute viscosity stays
null. The known value is reference input, never presented as an independent ADMET
measurement. This assumes Newtonian laminar flow, unchanged geometry and comparable
temperature; matching a path label does not verify the physical setup. Fit standard
errors do not include calibration/geometry/temperature uncertainty.

### Calibration and saved results

Corrections belong to the rig: set them on the Calibration stage, where they are
applied inside the flow unit. Calculations never apply a second correction to
recorded flow. Gravimetry reports the factor between weighed and recorded volume;
enter any change to the scale by hand on the Calibration stage.

Calculations consume closed recordings, actual step events and run measurements.
Missing/nonfinite samples, gaps over one second, pauses, skipped/incomplete steps
or insufficient trace coverage do not become zeros. New metrology calculations
also verify saved definitions against the archived executed steps. Missing inputs
are explained before calculation; quality failures save an inconclusive result
with per-step reasons rather than a fabricated estimate.

Each result records calculator version, measurement revision, selected references
and hashes of its input files, including reference dependencies. Editing input
measurements marks existing results **outdated** without deleting them. Outdated
or inconclusive results cannot be selected as references. References are explicit,
never inferred from the latest run. Result history is separate from protocols and
recordings, which are never rewritten by a calculation.
