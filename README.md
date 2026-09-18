# admet

Microfluidics acquisition and analysis, with no interface of its own. A terminal,
an MCP client, or a program drives it; each reaches the same three levels.

```
  low    an engine action        one call to a device, no guards
  med    an operation            the same call, plus the conditions under which
                                 using it is not a mistake
  high   a pipeline              several operations in order
```

It drives a Fluigent pressure controller and a Basler camera, runs protocols
against them, records synchronised video and fluidics data into a project, and
analyses the result.

```sh
make setup      # uv sync --all-extras
make test       # all simulated, no hardware touched
```

Nothing has to be read to start. Ask the system what it is:

```sh
admet describe                 # the three levels, and what is in each
admet describe run_priming     # one operation: its settings and its guards
admet describe acquisition     # one engine: its actions, and what drives them
```

## Running something

```sh
admet do connect_fluidics --set simulated=true
admet do apply_corrections
admet do run_priming --set prime_oil_volume_ul=40

admet run setup --set simulated=true      # a pipeline: several of the above
admet plan setup                          # what it would do, without doing it

admet call acquisition set_camera_settings --set camera_exposure_us=3000
```

`do` is an operation, `run` is a pipeline, `call` is one engine action with no
guards. `--set name=value` for scalars, `--json '{...}'` for anything structured.

Each invocation is its own process, so a connection does not outlive it. For a
sequence, use MCP or Python.

## Reading a name

Every id is a verb then its subject, and the same call is called the same thing
at every level: the operation `connect_fluidics` drives the action
`connect_fluidics`.

Each one also declares what calling it does, which the name alone cannot say:

| kind | meaning |
| --- | --- |
| `read` | answers a question and changes nothing |
| `write` | has finished having its effect when it returns |
| `start` | leaves something running after it returns — wait for it, or ask again |

`run_priming` is a `start`: it reports success while liquid is still moving.

And which half of the system it belongs to — `control` for the instrument,
`analyze` for what it produced, `general` for the session both work in:

```sh
admet operations --target analyze
```

## MCP

```sh
admet serve --simulated
```

63 tools over stdio, generated from the same declarations `describe` reads, so
there is no second description to fall out of step. The name says the level:

```
  acquisition_set_camera_settings   low    every camera setting, typed
  run_priming                       med    guarded, and it says what it needs
  pipeline_setup                    high   connect, correct, prime
```

`--simulated` keeps a session simulated: asking for real hardware is refused
rather than quietly given.

## Writing a protocol

A protocol is a list of steps. A step holds channels at setpoints until its
trigger fires.

Run one you wrote, without adding it to the build — over MCP, or with
`admet do run_steps --json`:

```json
{
  "steps": [
    {"name": "wet the oil line",
     "sensor_setpoints": {"0": 5.0},
     "trigger_type": "volume",
     "trigger_params": {"sensor_index": 0, "target_volume_ul": 2.0},
     "on_complete": "hold"},
    {"name": "settle",
     "sensor_setpoints": {"0": 2.0},
     "trigger_type": "time",
     "trigger_params": {"duration_s": 30.0},
     "on_complete": "zero"}
  ]
}
```

Triggers: `time`, `volume`, `stability`, `threshold`, `condition`,
`confirmation`. What each one needs is read from the trigger itself and reported
by `describe run_steps`, so it cannot drift. On completion: `hold`, `zero`,
`revert`.

To ship a protocol with the build, write a builder in
`src/admet/workflows/protocols.py` and add an `Operation` naming it in
`operations.py`. It then appears in the CLI, in MCP and in `describe` with no
other change.

A pipeline is plain Python — operations in order:

```python
def _setup(settings):
    yield step("connect_fluidics", simulated=settings["simulated"])
    yield step("apply_corrections")
    yield step("run_priming", prime_oil_volume_ul=settings["prime_oil_volume_ul"])
```

Each stage is checked by the operation's own guards as it is reached, so a
pipeline cannot do what the operation would have refused. A stage that starts a
protocol is waited on. A protocol that stops for the operator stops the
pipeline: it reports which stage it reached, the operator answers with
`confirm_protocol`, and the run resumes with `--from-stage`. Nothing is
auto-confirmed — a confirmation exists because somebody has to look at the rig.

## Guards

An operation refuses rather than warning, and says what to do:

```
run_priming: correction factors have not been applied, so flows would not be
true flows; run apply_corrections first
```

The guards are `project`, `fluidics`, `camera`, `corrections`, `idle`,
`running`, `sources`. `describe <operation>` lists the ones it needs and why.

Engine actions have no guards. That is what they are for, and `call` reaches
them deliberately.

## Projects

A project is a `.admetp` directory holding a manifest, recordings and results.
Core decides where every file goes — an engine is handed paths and writes to
them, never choosing its own, so nothing is written outside the open session.

```sh
admet do create_project --set path=runs/today.admetp
admet do open_project --set path=runs/today.admetp
admet do list_projects --set root=runs
```

Or for a single invocation:
`admet --project runs/today.admetp --create-project do read_status`

## From Python

```python
from admet.core.service import Admet

admet = Admet()
admet.create_project("runs/today.admetp")
admet.do("connect_fluidics", {"simulated": True})
admet.do("apply_corrections")
admet.do("run_priming", {"prime_oil_volume_ul": 40.0})
admet.wait_for_protocol()
```

`Admet` is the way in. It owns the session, decides where files go, and routes
to whichever engine is wanted. Engines stay reachable — `admet.engine_action(...)`
and `admet.engine(...)` — but they hold no protocols and no paths.

## Layout

```
  src/admet/core/        the Admet service: sessions, paths, routing
                         (core/engine.py is the contract engines implement)
  src/admet/workflows/   protocols, operations, guards, pipelines
  src/admet/engines/     the workhorses: acquisition, opencv, cellpose
  src/admet/mcp/         the stdio server, generated from the declarations
  src/admet/app.py       the command line
```

An interface used to live here and was removed. It is on the `admet2` branch and
can be merged back; nothing outside `app.py` imported it.

## Tests

```sh
make test        # unit tests
make test-all    # everything
make lint
```

Every test runs against the Fluigent simulator and the Pylon camera emulator,
and that is enforced rather than assumed. Nothing here has been validated on a
bench: simulation shows the software is consistent, not that a measurement is
right.
