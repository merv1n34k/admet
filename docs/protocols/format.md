# Protocol format

A protocol is a JSON document: a name, ordered steps, and optionally parameters,
measurements and calculations. Saving and planning never move anything; only
executing a reviewed plan does.

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

Channel keys are channel indices (`"0"`, `"1"`, `"2"`). Channels in one step run
at the same time. A channel cannot be flow- and pressure-controlled in the same
step. Targets are non-negative and must fit the ranges the hardware reports.

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
