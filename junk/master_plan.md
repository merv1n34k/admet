# admet — Consolidation Build Plan

## Context

Five separate tools in `~/.github_projects/` currently cover one ddPCR
(digital-droplet-PCR) workflow: **pylonguy** (Basler camera acquisition),
**droplegen** (Fluigent flow control), **dropline** (a hardcoded merger of the two
for droplet generation), **dropdrop** (Cellpose post-generation droplet/inclusion
analysis), and **admet** (this repo — OpenCV high-speed-video droplet measurement).

The split makes the workflow impossible for one person to track and impossible to
delegate or share. The goal is to **unify and simplify** them into one project,
still named **admet**: a **slim, minimal target-delivery system with ONE interface
model and different engines attached underneath** to perform specific actions.

The user's stated principle (2026-06-28): *the userspace is identical; only the
engines differ.* Two processes, two unified userspaces:

- **Analysis** — identical regardless of engine: **Import → set up batch matrix →
  Analyze → View → Export.** Cellpose (microscopy images) vs OpenCV (videos) is just
  which engine is attached; the user never sees that difference.
- **Acquisition (generation)** — one staged **TOC pipeline**; the user just follows
  the steps and never configures tools directly. Camera and Fluigent are **engines
  that declare their device settings/actions** to the workflow.

The engine is the only differentiated part: it **declares its settings** (a param
schema) and **implements the stage actions**. The interface renders the workflow and
the engine's declared settings generically.

This plan is the executable sequence. No code is moved until it is approved.

---

## Key decision: one interface technology — NiceGUI (no Qt)

NiceGUI runs **both** as a local **native desktop window** (`ui.run(native=True)`,
pywebview, Python backend in-process — can drive pypylon/Fluigent directly) **and**
as a **browser app**. So a single interface stack serves both surfaces:

- **control** mode → NiceGUI **native window** on the acquisition machine.
- **analyze** mode → NiceGUI **browser** app (localhost, single-user), dynamic.

Consequences:
- **PySide6 / Qt is dropped entirely.** One interface model, one UI toolkit.
- **dropletui is dropped** (it is a Qt widget library — unused without Qt).
- The pylonguy/droplegen **backends must be decoupled from Qt** (QThread → plain
  threads, Qt signals → callbacks/async, pyqtgraph → NiceGUI plotting). We extract the
  *control logic*, not the Qt widgets, and rebuild the UI in NiceGUI.
- **CLI is deferred** (added later as a third renderer over the same workflow+engines;
  the workflow/engine layer is kept headless-callable so this stays easy).

---

## Core principle → architecture

One package `admet/`, organized as **interface model + attachable engines + one
NiceGUI renderer**, with two opt-in install modes via extras so a machine installs
only its half's heavy deps.

```
admet/
  pyproject.toml            # extras: admet[control], admet[analyze]; Python 3.12
  admet/
    core/
      workflow/             # interface model: stage/pipeline state machine (engine-agnostic)
      engine/               # Engine protocol + registry (attach + lazy import); settings schema
      schema/               # shared data model: param declarations + result records/stats
      logging/              # one logger (replaces the per-repo copies)
    engines/                # ATTACHABLE engines — only installed ones register
      control/
        camera/             # Basler control (from pylonguy), decoupled from Qt
        fluidics/           # Fluigent control (from droplegen), decoupled from Qt
      analyze/
        cellpose/           # dropdrop detection + scanprotocol + cache (headless)
        opencv/             # admet BEO/BTVF/detect (headless, mid-rewrite)
        stats/              # pure compute: sample stats, Poisson, chi-squared
    workflows/
      control.py            # the staged TOC acquisition pipeline definition
      analyze.py            # Import → Matrix → Analyze → View → Export definition
    ui/                     # NiceGUI renderer of ANY workflow (native + browser)
    app.py                  # entry: `admet control` (native) / `admet analyze` (web)
```

**Why one package, not a uv workspace:** the user wants minimal surface. A single
package with **extras + lazy-importing engines** achieves the install split without
the workspace ceremony. Engines register only if their deps are present, so a
control-only box never imports torch and an analyze-only box never imports Fluigent.

### Install modes (extras), standardized on **Python 3.12**

3.12 satisfies every dependency at once (Fluigent SDK `<3.13`, torch, cellpose,
nicegui, pypylon) — one Python version sidesteps the per-extra version clash.

| Mode | `pip install` | Key deps (on top of base) | Excludes |
|---|---|---|---|
| base (always) | `admet` | nicegui, numpy, pandas | torch, hardware SDKs, pywebview |
| control | `admet[control]` | pypylon, Fluigent SDK, pywebview (native window), opencv, ffmpeg | torch, cellpose |
| analysis | `admet[analyze]` | cellpose/torch, opencv, scipy | pypylon, Fluigent, pywebview |

- Fluigent/pypylon are **x86_64-only**, so the control machine is x86_64; analysis is
  arch-free. The package does not force arch globally — only the `control` extra's
  wheels do.
- **No matplotlib/seaborn/PDF stack on the core path.** All visualization is rendered
  dynamically in NiceGUI. A static-export stack is an optional **`admet[export]`**
  extra, pulled only if/when the deferred expert-export feature is built.
- `make` targets follow the project standard: `setup`, `dev`, `build`, `test`,
  `test-all`, `lint`, `fmt`, plus chunked `test-core` / `test-analyze` / `test-control`.

---

## Component detail

### core (the interface model — light, always installed)
- **workflow/** — an engine-agnostic **stage/pipeline state machine**: ordered stages,
  advance/rewind/skip, per-stage state, confirmation gates, pause/resume. Both the
  acquisition TOC and the analysis Import→…→Export pipeline are instances of this.
- **engine/** — the **Engine protocol** (the load-bearing abstraction): an engine
  *declares its settings* (a typed param schema the UI renders generically) and
  *implements the actions* a stage invokes. Plus a **registry** that attaches engines
  by lazy import (missing deps → engine simply not available).
- **schema/** — the shared **data model**: param declarations + result records/stats.
  This is the contract every frontend and every engine speaks. Data only, no rendering.
- **logging/** — one implementation replacing the per-repo copies.

### engines/analyze (surface from `project-analysis-surface`)
Identical userspace; engine chosen by input type, **NOT overridable** (microscopy
z-stack images → Cellpose; videos → OpenCV).
- **cellpose/** = dropdrop, kept **1:1 functionally**. Refactor seams:
  - Extract a headless `detect()` from `Detection.run()` returning records +
    visualization data + layout; pull file IO, prints, tqdm into a separate writer.
  - Drop interactive `prompt_settings`/`discover_subdirectories`; the `settings` dict
    is the DI boundary callers build directly.
  - **Fixes**: `detect_droplets_cellpose` `sys.exit()` → `raise`; make cache dir
    configurable (currently hardcoded to package root); add inclusion params to the
    cache config-hash (currently omitted → stale-cache bug).
  - `scanprotocol.py` (EVOS grid parsing) is already pure — move as-is.
- **opencv/** = admet BEO→BTVF→detect (this repo, mid-rewrite). Only requirement:
  expose a **headless compute entry returning the schema**, same contract as Cellpose.
- **stats/** — port dropdrop's `_compute_sample_stats` / `_calculate_poisson` /
  `_chi_squared` / size-distribution binning as **plain numeric functions**. NO
  matplotlib, NO PNG/PDF. dropdrop's static rendering is **not ported** — replaced by
  dynamic NiceGUI rendering.

### engines/control (surface from `project-dropline-surface`)
Extract the **control logic** from pylonguy/droplegen; **decouple from Qt**; declare
device settings + stage actions through the engine contract. Drop both embedded GUIs.
- **camera/** (from pylonguy): connect/disconnect/enumerate, `init_settings` defaults,
  live preview (throttled to display rate), video recording (raw → FFmpeg → `.avi`,
  output path + per-run prefix), framerate readouts.
  - Declared settings — basic: Width, Height, Exposure, SensorReadoutMode; advanced:
    Gain, PixelFormat, Binning, AcquisitionFrameRate.
  - **CUT**: kymograph/waterfall (WaterfallWorker, `wtf2png.py`, capture-mode combo),
    presets, ROI rubber-band + crop, transforms/rulers, offset sliders, single-frame
    capture.
- **fluidics/** (from droplegen): Controller + the 3-thread model (decoupled from
  QThread to plain threads/async), simulated mode, 3 channels, real-time monitor,
  live plots (in NiceGUI), CSV logging, emergency stop.
  - **Pipeline engine + ALL triggers kept intact** (Time/Volume/Threshold/Condition/
    Confirmation).
  - **CUT**: pipeline JSON save/load; built-in DOE/extra PIPELINES; standalone
    droplegen window; editable PipelinePanel editor. Protocols stay **code-defined**
    with a clean **extension seam to add future protocols in code**.

### workflows (dropline-native userspace, preserved)
- **control.py** — the staged TOC pipeline Scene→Fluigent→Runs→Wash→Calibration +
  step tracker (rewind/skip), confirmation-gated protocols (priming/runs/wash) with
  pause/resume/laps, **synchronized camera-`.avi` + fluidics-CSV recording** per run,
  report dir `reports/<stamp>/{video,droplegen}/` + config/runs/summary JSON,
  droplet-calibration view, gravimetric dead-volume calibration, E-STOP/Resume/Skip.
- **analyze.py** — Import → set up **batch matrix** → Analyze → View → Export, defined
  once over the analyze engines.

### ui (one NiceGUI renderer)
- Renders **any** workflow generically: stages from the state machine, settings from
  the engine's declared schema — so attaching a new engine surfaces its settings with
  no bespoke UI.
- **Dynamic-first analysis view**: size distributions, Poisson comparison, per-sample
  stats, multiplex comparison as **live interactive plots/tables**, driven straight
  from the schema; re-filter / re-bin / toggle droplets without re-running detection.
- **Web canvas correction**: rebuild dropdrop's inclusion-correction in NiceGUI,
  reusing the pure geometric logic `update_results_with_inclusions`. The OpenCV HighGUI
  editor is **retired**.
- **Acquisition UI**: the TOC pipeline in native mode; throttled live preview; one
  correction/calibration panel (fluid calibration + a/b/c scale) with per-channel
  manual flow/pressure/stop kept separately; a **master log viewer** for both camera
  and fluidics (filters, time binning, crosshair, Δt/Δy).
- **Export is deferred & opt-in** (expert, later): static PNG/PDF/summary behind the
  optional `admet[export]` extra; until then the only persisted analysis artifacts are
  the raw **data** (CSV/JSON).

---

## Phased port order

Each phase leaves the source repos untouched and runnable until verified.

1. **Scaffold.** Single `admet/` package: `pyproject.toml` with `[control]`/
   `[analyze]` extras, Python 3.12 baseline, NiceGUI in base, `make` targets. Verify
   each extra resolves independently (control has no torch; analyze has no Fluigent).
2. **Core interface model.** workflow state machine + engine protocol/registry +
   schema + logging. Engine-agnostic, no engines yet. Unit-test the state machine.
3. **NiceGUI shell.** A renderer that drives a trivial dummy workflow + dummy engine,
   proving both run modes (`native=True` window and browser) and generic
   settings-from-schema rendering.
4. **analyze engines.** OpenCV first (lightest) behind the engine contract emitting the
   schema; then Cellpose (dropdrop) with the headless `detect()` extraction + the three
   fixes; then `stats/` as pure compute. Verify the **numbers** match current dropdrop
   (not the old rendered artifacts).
5. **analyze mode end-to-end.** The Import→Matrix→Analyze→View→Export workflow + the
   dynamic NiceGUI view + the web canvas correction. This delivers the analysis surface
   on real inputs (`admet/data/` videos; dropdrop z-stacks).
6. **control engines.** Extract camera (pylonguy) + fluidics (droplegen) control,
   **decoupled from Qt** (plain threads/callbacks), simulated mode first; declare device
   settings + stage actions via the engine contract. Verify simulated acquisition +
   pipeline engine + all triggers.
7. **control mode end-to-end.** The TOC pipeline in NiceGUI native mode: throttled
   preview, synchronized recording, master log viewer, single correction/calibration
   panel. Verify simulated, then with hardware.
8. **CLI (deferred).** A third renderer over the same workflow+engines, non-interactive
   — added when wanted; the headless layer keeps this small.
9. **Decommission.** After each surface is verified, retire the source repos
   (archived/tagged, not deleted, until a full wet run passes).

---

## Verification

- **Per-area**: `make test-core` / `make test-analyze` / `make test-control`;
  `make test-all`; `make lint` / `make fmt` clean.
- **Install modes**: in a clean env, install `admet[control]` and confirm
  torch/cellpose absent; install `admet[analyze]` and confirm pypylon/Fluigent/pywebview
  absent; confirm the engine registry exposes only the installed engines.
- **One interface, two run modes**: launch `admet analyze` (browser) and `admet
  control` (native window) from the same UI codebase.
- **Analysis parity (the 1:1 guarantee)**: run current dropdrop and new admet on the
  same z-stack inputs (single + multiplex) and diff the **structured data** — records +
  computed stats (counts, diameters, Poisson, chi-squared). Numbers must match;
  dropdrop's static PNG/PDF reports are intentionally NOT reproduced.
- **Dynamic view**: distributions / Poisson / stats / multiplex render interactively
  from the schema, and re-filter / toggle-droplet updates the view without re-running.
- **OpenCV engine**: run on the sample videos in `admet/data/` and confirm detections
  populate the schema and render dynamically.
- **Acquisition (no hardware)**: run the full Scene→Fluigent→Runs→Wash workflow in
  **simulated mode**; confirm synchronized recording writes `reports/<stamp>/…`, the
  master log viewer shows both streams, all trigger types execute, preview renders.
- **Acquisition (hardware)**: final acceptance is a complete wet run (camera + Fluigent)
  producing a valid recording set.

---

## Notes / risks
- **NiceGUI native preview FPS**: live camera preview goes through the webview layer, so
  it is throttled to display rate (~15–30 fps) — fine for human monitoring; the *actual*
  recording is raw→FFmpeg in Python and bypasses the UI. Validate preview throughput
  early in phase 7.
- **Qt decoupling effort**: droplegen's 3-thread model and pyqtgraph plots are
  Qt-coupled; phase 6 must rework them to plain threads/callbacks + NiceGUI plotting.
  This is more than a lift-and-shift (called out so it is scoped).
- admet's OpenCV engine is being actively rewritten; phase 4 consumes whatever the
  rewrite lands, requiring only headless-contract conformance.
- The cache-hash and `sys.exit` fixes are intentional correctness changes (called out so
  parity-diffing accounts for them).
- **Static export is deliberately deferred** behind `admet[export]`; the system is
  dynamic-first. The `reports/<stamp>/…` dir in acquisition is raw recording data
  (video + CSV), unrelated to static analysis export — it stays.
