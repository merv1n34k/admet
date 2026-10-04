# admet

## Qt desktop

ADMET provides a Qt device/camera window and validated JSON protocols.
The desktop and direct Python binding share the same acquisition service.

```powershell
uv sync --locked --extra control
uv run --locked --extra control admet qt
```

With the project environment activated, open the app with `admet qt`
(optionally `--project PATH`). Without activation, use `uv run --extra control admet qt`.

The desktop defaults to real devices. Step 2 has a **Simulated Hardware** selector
for Fluigent; choose before connecting. This does not simulate the camera.
Devices remain disconnected until you click Connect. Create/open an
`.admetp` project, connect devices, apply corrections, then use the original
TOC: **Priming → Preflight → Experiment steps → Calculations → Wash → Cleanup**. Add experiments with **+ Protocol
step**. Select a protocol and review the plan directly between the existing
action bar and graphs/camera; use **Plan** and **Execute** in that action bar.
Confirmation gates, pause/skip/abort, emergency stop, and run artifacts use the
existing backend. Closing the window stops and disconnects; it does not detach.
Execute uses the protocol's own confirmation gates, without an extra approval prompt.
The selector, parameter table and JSON editor stay visible alongside the preview.
Execution hides the JSON editor, leaving parameters and the run table visible.
Editing parameters, selecting a template or building another plan shows JSON again.
Protocol edits and plan previews stay in memory, without changing bundled templates.
Closing the app or switching/reopening projects discards them. Only Execute saves
the exact protocol, parameters and run. Run measurements/results remain persistent.

Preflight is a non-actuating setup page for flow/phase ratios, fluidics layout and
consumption formulas; **Save Project** preserves the layout. Legacy check data
remain in existing projects but are no longer shown on this page. The protocol
selector offers density, Drop-Seq, flow stability scout, gravimetry, dead volume
and viscosity. Gravimetry, viscosity and dead volume run one selected fluidics
channel, defaulting to M1. Their existing Parameters table selects 0 Oil-L / 1 M1 /
2 M2, low flow (default 15 µL/min) and working flow (set 67 for M1/M2 or 250 for L).
Channel selection does not silently change working flow. Gravimetry uses only
before/after weights, with 100 µL nominal per collection by default.

The former Python operations `run_characterisation`, `run_gravimetry`,
`run_dropseq` and `validate_oil_capacity` are retired. Use JSON protocols for
experiments (including the bundled Drop-Seq protocol). Priming, Wash, expert
`run_steps` and protocol save/list/file-planning operations remain available.

Custom JSON can declare editable parameters with arithmetic step expressions.
They appear in the same settings table as Priming; change a base flow once to
update every linked step when rebuilding the plan. See the
[parameter syntax](docs/json_protocols.md#editable-parameters-in-qt).

Start oil testing with **flow stability scout**: a single-height M1
flow sweep, with editable base flow and point duration. Review its settling/fit
recommendations before preparing a density run. See
the protocol's editable parameters and saved calculation report.

**Calculations** is a separate TOC section: choose a finished recorded run and
one of its declared calculations, then **Calculate**. Gravimetry, dead volume,
viscosity, flow scout, density and recording summary share this panel. It reads archived
files, never operates devices, and saves each result separately under
`records/protocols/<run_id>/calculations/`. Saved results are available after
reopening the project; Checkup and protocol execution do not run calculations.
Density uses the confirmed density templates' height/pass labels and recorded
step times (10 seconds settling, then sampling). Keep those labels unchanged;
arbitrary CSV files without geometry and step timing cannot establish density.
Older recordings without the saved polling clock origin return an inconclusive
density result rather than guessing measurement windows. The calculation
selector is backed by `workflows/calculations.py`; additional calculators can
register there without adding experiment-specific TOC pages.

Enter masses or dead volumes in the experiment's separate **Measurements** table,
enabled when execution starts. Choose reference results explicitly in Calculations;
the app never chooses the latest calibration or applies a computed multiplier to
hardware. Changed measurements or reference inputs mark saved results outdated.
See [calculation definitions and workflow](docs/json_protocols.md#run-calculations)
for formulas, reference compatibility and uncertainty limits. New recipes and
calculations have offline/simulated coverage; validate them on your physical setup
before relying on their numerical accuracy.

Historical project/recording aliases and relocated recording paths are read through
`core/compat.py`; old label-based density definitions through `workflows/compat.py`.
These readers do not migrate files or change executable protocols. Saving preflight
settings retains historical check entries, although the removed checkup UI no longer
displays them. Current density metadata takes precedence over historical step labels.

See [Windows desktop setup and workflow](docs/windows-qt.md). Desktop operation
is covered by offscreen Qt, simulated fluidics, and mocked camera tests on macOS;
Windows drivers and physical devices still need acceptance testing on the target PC.

## Python API

`admet describe` lists operations and engines; `admet describe run_priming`
describes one target. The CLI has only `qt` and `describe`.

```python
from admet.core.service import Admet

admet = Admet()
admet.create_project("experiment.admetp")
admet.do("connect_fluidics", {"simulated": True})
try:
    admet.do("apply_corrections")
    plan = admet.plan_protocol("run_priming", {"prime_oil_volume_ul": 40})
    # Review plan and physical setup before explicitly executing:
    # admet.execute_protocol_plan(plan["plan_id"])
finally:
    admet.do("disconnect_fluidics")
```

Planning validates and freezes settings without operating hardware. Execution
rechecks guards and rejects stale/cancelled plans. Preview plans stay in memory;
executed runs save their definitions, digest, measurements, events and recordings
in the project. See [JSON protocols](docs/json_protocols.md).

The Python binding also permits direct guarded operations via `Admet.do()`,
without a plan, and unguarded expert engine actions via `Admet.engine_action()`.
Library callers own device lifecycle and must stop/disconnect in `finally`.
There is no background owner, remote server, client lease or terminal controller.
Manual flow persists until stopped; keep the physical emergency stop accessible.
The Qt desktop handles shutdown and prevents a second desktop instance.

## Layout

- `src/admet/core/`: shared Python service, projects and data models.
- `src/admet/engines/`: acquisition, hardware and analysis engines.
- `src/admet/workflows/`: protocol validation, planning and calculations.
- `src/admet/ui/`: Qt desktop and existing analysis UI.
- `templates/`: editable protocol definitions, also included in distributions.
- `tests/`: simulated and mocked tests; no physical device required.

## Development

Use `make setup`, `make dev`, `make test`, `make test-all`, `make lint`
and `make build`. `make test` runs core and mocked acquisition tests;
`make test-all` adds simulated integration, analysis and offscreen desktop
tests without repeating groups. Use `make test-integration`, `make test-analyze`
or `make test-desktop` to run those groups separately. Simulation does not validate physical wiring,
fluid calibration or Windows device drivers.
