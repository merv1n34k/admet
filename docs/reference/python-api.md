# Python API

The desktop and the Python API share one service, `admet.core.service.Admet`.
Use it to script runs or to build your own front end.

```python
from admet.core.service import Admet

admet = Admet()
admet.create_project("experiment.admetp")
admet.do("connect_fluidics", {"simulated": True})
try:
    admet.do("apply_corrections")
    plan = admet.plan_protocol("run_priming", {"prime_oil_volume_ul": 40})
    # Review the plan and the physical setup, then execute explicitly:
    # admet.execute_protocol_plan(plan["plan_id"])
finally:
    admet.do("disconnect_fluidics")
```

## Plans

Planning validates and freezes a protocol without moving anything. Execution
rechecks the rig and refuses stale or cancelled plans.

| Call | Does |
|---|---|
| `plan_protocol("run_json_protocol", {"protocol": definition})` | Plans an inline JSON protocol |
| `plan_protocol_file(path)` | Plans a JSON file |
| `execute_protocol_plan(plan_id)` | Executes a reviewed plan |
| `control_protocol(action=..., plan_id=...)` | `execute`, `wait`, `confirm`, `skip`, `pause`, `resume`, `abort`; returns at the next event |

## Operations

`admet.do(name, settings)` runs one guarded operation directly, without a plan —
connecting devices, applying corrections, setting a channel, saving and listing
protocols. `admet describe` lists them all with their settings.

```python
admet.do("save_protocol", {"protocol": definition, "replace": False})
admet.do("list_protocols")
```

`admet.engine_action(engine, action, settings)` reaches engine primitives
without guards; it is meant for experts.

## Calculations

```python
from admet.workflows.calculations import calculate_run, saved_results, result_view

payload = calculate_run("experiment.admetp/records/protocols/run_<id>", "gravimetry")
view = result_view(payload)       # sections of facts, tables and notes
```

## Responsibilities

A script owns the devices it connects: stop and disconnect in `finally`. There is
no background owner or remote server. Manual flow continues until stopped; keep
the physical emergency stop accessible.
