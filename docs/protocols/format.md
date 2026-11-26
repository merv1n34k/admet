# Protocol format

A protocol is a JSON document: a name, ordered steps, and optionally a run block,
parameters, measurements and calculations. Saving and planning never move anything;
only executing a reviewed plan does.

```json
{
  "name": "short_run",
  "pressure_limits_mbar": {"0": 500},
  "steps": [
    {
      "name": "Dispense",
      "sensor_setpoints": {"0": 10},
      "trigger_type": "volume",
      "trigger_params": {"sensor_index": 0, "target_volume_ul": 5},
      "timeout_s": 60,
      "on_complete": "zero",
      "confirm_message": "Dispense 5 µL at 10 µL/min?"
    }
  ]
}
```

## Steps

| Field | Meaning |
|---|---|
| `name` | Shown in the plan and the event log |
| `sensor_setpoints` | Flow targets in µL/min, by channel |
| `pressure_setpoints` | Pressure targets in mbar, by channel |
| `trigger_type`, `trigger_params` | What ends the step (below) |
| `timeout_s` | Fails the run if the step has not ended by then; required except for `time` |
| `on_complete` | `zero` (default), `hold` or `revert` |
| `confirm_message` | A gate: the step waits for **Confirm** before applying its targets |
| `repeat`, `group` | Repeat the step; group steps in the plan |
| `record` | `true` records camera video while this step runs (off by default); a boolean parameter, `"{record_video}"`, can switch it |
| `record_max_s` | Stops this step's video after this many seconds; 100 at most |

Channel keys are channel indices (`"0"`, `"1"`, `"2"`). Channels in one step run
at the same time. A channel cannot be flow- and pressure-controlled in the same
step. Targets are non-negative and must fit the ranges the hardware reports.

### Video

Only steps with `"record": true` record video, each into its own file, from when
the step starts until it ends. Nothing is recorded while a step waits at its gate.
A pause closes the file; resuming the step opens a new part (`…_part2_…`). Stop,
skip, an error, a timeout and E-STOP all close it. No step records more than 100 s
in total over its parts, or less if `record_max_s` says so; at that point the video
stops and the step carries on. If the camera cannot start, the protocol pauses with
all channels at zero, so a step that asked for video never runs on without it unless
you resume it.

### Triggers

| Trigger | Ends when | Parameters |
|---|---|---|
| `time` | The duration has passed | `duration_s` |
| `volume` | A channel has delivered the volume | `sensor_index`, `target_volume_ul`, `mode` (`adaptive` or `integral`) |
| `stability` | A reading has settled | see `admet describe run_steps` |
| `threshold` | A reading crosses a value | see `admet describe run_steps` |
| `condition` | A condition on readings holds | see `admet describe run_steps` |

A volume trigger watches the one channel named by `sensor_index`; every other
channel in the step stops at the same moment, whatever it has delivered.

### Pressure trips

`pressure_limits_mbar` sets software trips per channel. They are off unless set.
See [Safety](../control/safety#pressure-trips).

## Run

What the run will be called and which set it joins, apart from the protocol's name:

```json
"run": {"name": "Oil 3", "condition": "Oil 300"}
```

Both are optional text of up to 80 characters; a missing or empty one is none. The
**name** tags the run: its folder is named after it, its record, the record of each
recording and the lists in Review and Calculations carry it, and in `admet control`
the step takes it at Plan; without one the run is named after the protocol. The
**condition** is the generation condition the run belongs to. Left out, it is the
protocol with its oil, cells and beads setpoints, `dropseq_3n o300 c40 b40`; give
one to call it something else. The record and the recordings carry it, Review
lists it, and `admet analyze` puts the run's videos into it when it scans them,
named `<condition>_rep1`, `<condition>_rep2`, … (see
[Generation conditions](../analyze/overview#generation-conditions)). In `admet control` the two
are the first rows of the parameter table.

A run of a designed experiment carries two more, which the experiment writes, not the operator:

```json
"run": {"name": "K7Q-03", "condition": "300/40/40", "id": "K7Q-03", "experiment": "ratio sweep 26 Nov"}
```

The **id** is the run's id in its experiment: a code of three characters (no 0/O, 1/I/L), a dash
and the run's number. Each replicate collected from the run is a sample with an id of its own,
the run's id and the replicate: `K7Q-03-r2`. The run's record carries the id as `run_code`, so
whatever is labelled with a sample's id, such as a folder of microscopy images, can be matched to
the run and the replicate it came from. The **experiment** is the experiment's name.

## Measurements

Values typed in during the run, such as vessel masses:

```json
"measurements": {
  "before_mg": {"label": "Vessel before", "unit": "mg", "step": 2, "required": true},
  "after_mg":  {"label": "Vessel after",  "unit": "mg", "step": 2, "required": true},
  "density":   {"label": "Fluid density", "unit": "g/mL", "min": 0}
}
```

`step` ties a field to an expanded, one-based step. Units: `mg`, `g`, `s`, `uL`,
`g/mL`, `mPa.s`, `mm`, `cm`, `1`. Missing values stay empty, never zero; a
required value blocks calculation, not execution.

## Calculations

A protocol lists the calculations its runs offer and binds them to steps and
measurements:

```json
"calculations": [
  {"type": "gravimetry", "units": "{units}", "liquid": "{liquid_name}", "density": "density_g_ml",
   "samples": [{"step": 1, "pass": 1, "before": "mass_before_{unit}_1", "after": "mass_after_{unit}_1"}]},
  {"type": "recording_summary"}
]
```

Unknown types or fields, wrong units and missing step references are refused
before the run. Use the [bundled templates](./templates) as complete examples.

## Validation

Unknown fields, duplicate keys, invalid numbers, unknown channels and out-of-range
targets are refused. A plan is frozen: editing the protocol afterwards needs a
new plan.
