# Parameters and units

Parameters make one protocol serve many runs. `admet control` shows them in the
*Parameter / Value* table; the run archives the values it used.

## Declaring parameters

```json
"parameters": {
  "liquid_name": {"type": "text",    "label": "Liquid name", "default": "water"},
  "flow":        {"type": "number",  "label": "Flow, µL/min", "default": 15, "min": 0, "max": 100},
  "filtered":    {"type": "boolean", "label": "Filtered", "default": true},
  "channel":     {"type": "choice",  "label": "Channel", "default": 1, "options": [0, 1, 2]},
  "settle_s":    ["Settling time, s", 10]
}
```

The short form `[label, default]` is a non-negative number.

## Expressions

Numeric fields — setpoints, trigger parameters, timeouts, repeats, pressure
limits — accept expressions of declared numbers:

```json
"sensor_setpoints": {"1": "base_flow * 2"},
"trigger_params": {"duration_s": "settling_s + averaging_s"},
"timeout_s": "settling_s + averaging_s + 10"
```

Only names, numbers, `+ - * /`, signs and parentheses are allowed — no
functions, no Python. Division by zero and out-of-range results are refused.

## Text substitution

`{name}` inserts a parameter's value into names, gate messages, groups and some
text fields:

```json
"confirm_message": "Collection {n} on channel {channel}: place the vessel."
```

Substitution happens once; inserted text is never evaluated.

## Choosing a channel

A channel key can come from a parameter, so one protocol runs on any channel:

```json
"sensor_setpoints": {"{channel}": "working_flow"}
```

## Units masks: several channels at once

For protocols that can run one channel or several together, a `units` parameter
holds a mask, read right to left:

| Mask | Channels |
|---|---|
| `001` | L |
| `010` | M1 |
| `100` | M2 |
| `110` | M1 and M2 |
| `111` | all three |

A step marked `"units": "{units}"` keeps only the setpoints of the masked
channels, so each channel can still have its own flow:

```json
{
  "units": "{units}",
  "sensor_setpoints": {"0": "working_flow_l", "1": "working_flow_m", "2": "working_flow_m"},
  "trigger_type": "time",
  "trigger_params": {"duration_s": "collection_ul * 60 / step_min_flow"}
}
```

`step_min_flow` is the lowest flow the step sets after masking. Timing a step by
it gives every channel at least the nominal volume.

Measurements marked `"for_each_unit": "units"` are repeated for each masked
channel: `{unit}` becomes `ch0`, `ch1`, … in keys, and `{unit_name}` the
channel's name in labels.

```json
"mass_after_{unit}_1": {"label": "Collection 1 · {unit_name}: mass after",
                        "unit": "mg", "step": 1, "for_each_unit": "units"}
```
