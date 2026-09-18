# admet

Microfluidics acquisition and analysis, without an interface.

This is the engine layer: it drives a Fluigent pressure controller and a Basler
camera, runs experiment protocols against them, records synchronised video and
fluidics data into a project, and analyses the result. You talk to it from a
terminal, from Python, or over MCP — there is no window.

An interface used to live here and was removed; see [What is not here](#what-is-not-here).

## Start

```bash
make setup                 # uv sync --all-extras
make test                  # 218 tests, all simulated, ~4s
```

Nothing above touches hardware. Every test runs against the simulated backend,
and that is enforced rather than assumed.

Prove it works end to end without a rig attached:

```bash
uv run admet describe acquisition
uv run admet run acquisition connect_fluidics --set simulated=true
```

## Where things are

11,700 lines. Read them in this order:

| Path | Lines | What it is |
| --- | --- | --- |
| `engines/acquisition/protocols.py` | 310 | **The experiments.** Edit this to add one. |
| `engines/acquisition/triggers.py` | 281 | What a step can wait for: time, volume, stability, a threshold. |
| `engines/acquisition/pipeline.py` | 323 | Runs declared steps against the hardware. Knows no experiment by name. |
| `engines/acquisition/engine.py` | 752 | The action surface: 29 actions, 62 settings. Everything outside talks to this. |
| `engines/acquisition/fluidics/` | ~900 | The Fluigent SDK, channels, polling, CSV logging. |
| `engines/acquisition/camera/` | ~700 | Basler camera, live frames, video writing. |
| `engines/{opencv,cellpose}/` | ~4,000 | Droplet detection and segmentation. |
| `core/` | 1,142 | Engine contract, parameter validation, project store. |
| `workflows/` | ~1,400 | Planning arithmetic, batch analysis, stored check history. |
| `mcp/` | ~290 | Engine actions as tools a program can call. |

An engine declares what it can do (`ActionSpec`) and what each action takes
(`Param`). Everything else — the CLI, the MCP tool list — is generated from that
declaration, so there is no second description to keep in step.

## Driving it

### From a terminal

```bash
uv run admet describe acquisition          # actions and settings, as JSON
uv run admet run acquisition connect_fluidics --set simulated=true
uv run admet run acquisition run_protocol \
    --set pipeline_name=Wash --set wash_oil_volume_ul=500 --set tick_s=0.2
```

`--set NAME=VALUE` reads the value as what it looks like: `true` is a boolean,
`42` an integer, `2.5` a number, anything else text. For values a shell would
mangle, use `--settings-json '{"pipeline_name": "Wash"}'`.

### From Python

```python
from admet.app import create_engine_api
from admet.core.run import RunJob

api = create_engine_api("acquisition")
api.run(RunJob(id="connect", engine=api.id, action="connect_fluidics",
               settings={"simulated": True, "start_polling": True}))
api.run(RunJob(id="run", engine=api.id, action="run_protocol",
               settings={"pipeline_name": "Priming", "prime_oil_volume_ul": 40.0,
                         "prime_aqueous_volume_ul": 5.0, "tick_s": 0.2}))

while api.engine.pipeline_state == "running":
    if ...:                                    # a step is waiting for the operator
        api.run(RunJob(id="ok", engine=api.id, action="confirm_protocol"))
```

Progress arrives as `PipelineEvent` objects on `api.engine.pipeline_queue`.

### Over MCP

```bash
uv run admet serve --simulated      # JSON-RPC on stdio
```

Each engine action becomes a tool named `<engine>_<action>` — for example
`acquisition_run_protocol` — carrying the parameter types, ranges and choices the
action accepts. Point any MCP client at the command above.

**Simulated mode guards the connection, not each action.** Connecting is forced
simulated; asking for real hardware is refused with the reason. Once connected
simulated, everything downstream acts on the simulation, so protocols run
normally. The camera has no simulated flag, so it is refused unless
`PYLON_CAMEMU=2` is set before the server starts.

Use `uv run admet serve` without the flag to drive real hardware.

## Adding an experiment

Protocols are Python, declared as data. A step says what to set, what to wait
for, and what to leave the channel doing.

**1.** Write the builder in `engines/acquisition/protocols.py`:

```python
def build_rinse_protocol(settings: dict) -> list[ProtocolStep]:
    volume_ul = float(settings["rinse_volume_ul"])
    return [
        ProtocolStep(
            name="Rinse Oil L",
            sensor_setpoints={OIL_L_SENSOR: 250.0},
            trigger_type="volume",
            trigger_params={"sensor_index": OIL_L_SENSOR, "target_volume_ul": volume_ul},
            on_complete="zero",
            confirm_message=f"Rinse with {volume_ul:g} uL. Proceed?",
        ),
    ]
```

**2.** Add it to `PROTOCOLS` in the same file:

```python
PROTOCOLS = {..., "Rinse": build_rinse_protocol}
```

**3.** Declare each new setting in `engines/acquisition/settings.py`, and list it
on the `run_protocol` action in `engines/acquisition/engine.py`. A setting that
is not in both places never reaches your builder — validation rejects unknown
settings before the action runs.

It is then selectable everywhere at once: `--set pipeline_name=Rinse`, the
`pipeline_name` enum in the MCP tool schema, and `build_protocol("Rinse", ...)`.

### Triggers

| `trigger_type` | Waits for | Bounded by |
| --- | --- | --- |
| `time` | a duration | itself |
| `volume` | a channel to deliver a volume | nothing — pair it with a plausible flow |
| `stability` | flow to hold steady within a tolerance | `timeout_s`, and it reports timing out |
| `threshold` | a reading to sit near a target | `stable_duration_s` |
| `condition` | a reading to cross a bound | latching |

A step with `confirm_message` waits for the operator before it runs. Nothing else
in the system is keyed off that text.

`on_complete` is `hold` (leave it running), `zero` (setpoint to nothing) or
`revert` (release the channel).

## Projects

A project is a directory ending in `.admetp`, holding `manifest.json` and a
`records/` tree: `camera/*.avi`, `fluidics/*.csv`, and `checks/*.json` for stored
system checks. `core/project.py` is the only writer. Existing projects are
readable unchanged.

## What is not here

A Qt control window and a NiceGUI analysis interface were removed — 10,483 lines,
42% of the codebase — to leave something a new maintainer can read. They are in
git history on the `admet2` branch and can be reattached against this engine API.

A YAML protocol system was written and removed in the same pass: it parsed and
compiled documents but had no runtime, and with the same person writing both the
protocols and the code, a markup language earned nothing that thirty lines of
Python did not. Also on `admet2`.

## Testing

```bash
make test          # everything, simulated
make lint          # ruff
```

Hardware is never touched by tests. The Fluigent simulator is driven through the
real SDK path, and the camera through Basler's emulator (`PYLON_CAMEMU`), so the
code under test is the code that runs on the rig — but **simulation is not bench
validation**: nothing here proves a pressure or a flow is correct on real
hardware.
