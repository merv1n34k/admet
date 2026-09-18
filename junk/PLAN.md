# admet — Core & Engine Refinement Plan (headless, renderer-ready)

## Purpose

Harden the **headless layer** — `core/` (workflow, engine, schema, logging) and
`engines/` (control + analyze) — so it is a clean, rendering-neutral contract that
**any** front-end can drive later. **GUI work is out of scope here.** The proven
decision (recorded for later) is two renderers over this one core: **Qt+pyqtgraph for
control, NiceGUI for analyze, CLI possible as a third** — this plan builds *none* of
them, it only makes the core ready and fixes the engine logic.

This refines, and for the core/engine sections supersedes, `master_plan.md`. The GUI
half of `master_plan.md` is deferred, not cancelled.

---

## Settled decisions (do not re-litigate)

- **Control is ONE engine.** Camera + fluidics are always linked; there is no mode where
  they run unlinked. The combined engine is named `control` (today it mis-declares
  `id="fluidics"` — rename). No split into separate camera/fluidics engines. The rename
  is done **in this pass**; it touches `app.py`, the registry, and any saved config that
  keys on the `"fluidics"` string — change all of them together.
- **Telemetry is deferred.** The live-stream interface (frames/sensors/events/logs) is
  needed only by a GUI; it is **not** part of this plan. See *Deferred*.
- **Batch is native to analyze.** Batch/session is core analyze infrastructure shared by
  **all** analyze engines (opencv *and* cellpose), not an opencv add-on.

---

## Scope

**In scope**
- The renderer-facing contract: settings, actions, results, stage flow.
- **Batch & session as analyze core** (elevated, engine-agnostic).
- Symmetric, lazy engine **registry** for the install split.
- Engine **logic-fidelity fixes** found in audit (parity + correctness).
- Headless tests for all of the above.

**Out of scope (deferred — see end)**
- Any GUI (NiceGUI/Qt/CLI renderers, preview widgets, plots, panels).
- **Telemetry / live streaming** and the associated frame-neutrality refactor.
- Static export (`admet[export]`).

---

## Guiding principle

> The engine is a **headless capability provider**. It declares *what it can do*
> (settings + actions) and *what it produces* (structured results), in terms that contain
> **no rendering and no UI vocabulary**. A renderer is a pure consumer of that contract.
> If a concept only exists to serve one front-end, it does not belong in the engine.

---

## The renderer-facing contract (four things)

A renderer needs exactly four things from the core. Each is data or a typed interface —
never a widget.

### 1. Settings — already good; make it single-source-of-truth
`core/schema/params.py` `ParamSchema` is solid (typed `Param`, defaults, validate).
**Fix:** the engine is the *only* owner of its settings. Today camera settings are
declared twice — `engines/control/engine.py` **and** `workflows/control.py:25-73` — a
drift hazard. Workflows reference the engine's schema; they never redeclare params.

### 2. Actions — NEW: declare them as data, like settings
Today `run_action` is a 24-branch `if action == "...":` dispatch with no catalog. A
renderer (or CLI) cannot enumerate, label, or group actions without hardcoding strings.
Add a declared **ActionSpec** catalog, the symmetric partner to `ParamSchema`:

> This is the **one net-new abstraction** in the plan — everything else is relocation,
> cleanup, or parity fixes. Justified because the wide-table fix (#3) and batch-as-actions
> both read cleaner when actions are data, not strings. If a renderer never materializes
> it still pays for itself by killing the 24-branch dispatch. Kept (not deferred).

```python
# core/engine/actions.py
@dataclass(frozen=True)
class ActionSpec:
    id: str
    label: str
    category: str            # connection | recording | protocol | calibration | safety | diagnostics | batch
    params: tuple[str, ...] = ()   # subset of the engine's ParamSchema this action reads
    destructive: bool = False      # e.g. emergency_stop — renderers confirm/style accordingly
    description: str = ""
```

- The `Engine` protocol gains `actions: tuple[ActionSpec, ...]`.
- `run_action(id, settings, context)` validates `id` against the catalog and validates
  `settings` against that action's declared `params`.
- Any future renderer/CLI builds its action surface generically from this catalog — no
  engine-name or action-string conditionals anywhere.

### 3. Results — clean the shape (renderer-neutral; fixes the wide-table at source)
`core/schema/results.py` (`ResultRecord`, `SummaryStat`, `ResultSet`) is a good data-only
contract. **Fix the engine's *use* of it.** Today `_status_result`
(`engines/control/engine.py:557-561`) flattens `action` + every status flag + every
message + every param into **one record's `values`** → a 1-row × ~25-column blob.
Enforce one rule:

- **`ResultSet.metadata`** = status + messages + scalar state (`connected`, `*_ok`,
  `*_message`, action name). Not tabular.
- **`ResultRecord`s** = real data rows (per-droplet, per-channel readback, per-sample
  stats) — one row per logical entity, not one row per call.
- **`artifacts`** = file paths / bundle handles.

A contract fix, not a UI fix: results become correct for CLI, Qt, and web alike.

### 4. Stage flow — already good
`core/workflow/model.py` (`Workflow`, advance/skip/rewind/confirm/pause) is engine-
agnostic and shared by both workflows. Keep. Once the action catalog exists,
`core/workflow/runner.py` validates invoked actions against it.

---

## Batch & session — native analyze core (promote out of opencv)

The analyze surface **is** a batch surface: the workflow is Import → set up **batch
matrix** → Analyze → View → Export. Batch is therefore first-class, shared by every
analyze engine. Today the machinery exists but is mis-located and engine-coupled:

- `engines/analyze/opencv/batch.py` — `BatchRunner` runs a list of `BatchItem`s
  sequentially with started/progress/finished/failed callbacks. **But** it calls
  `DropletPipeline` directly, so it only works for opencv.
- `engines/analyze/opencv/session.py` — bundle persistence (`save_analysis`/
  `load_analysis` → `.admet` dir: `meta.json`, `results.pkl`, `background.npy`,
  `contours/`), **batch project files** (`save_project`/`load_project`), and the
  cache-validity check `analysis_matches_config` (skip re-analysis when config+input
  match). Genuinely useful, but opencv-shaped.

**Refactor:**
1. **Relocate** batch/session to a shared analyze home: `engines/analyze/batch.py` +
   `engines/analyze/bundle.py` (or `session.py`). They belong to the analyze surface, not
   one engine.
2. **Make `BatchRunner` engine-agnostic.** It drives the **engine contract** per item —
   `engine.run_action("analyze", item_settings, context)` returning an `EngineResult` —
   instead of importing `DropletPipeline`. The chosen engine (opencv *or* cellpose) is
   injected; the runner never imports either.
3. **Aggregate into the shared schema.** Per-item `EngineResult`s roll up into a batch
   `ResultSet` (records keyed by `video_id`/`sample_id`), so View/Export read one uniform
   structure regardless of engine.
4. **Persistence/caching seam.** Keep per-item bundles + the batch **project file** +
   `analysis_matches_config` skip-if-cached, but generalize the engine-specific payload
   (opencv's `background.npy`/`contours/`, cellpose's cache) behind a uniform
   `save_result`/`load_result`/`matches_config` seam the engine implements. The batch core
   orchestrates; the engine owns its bundle format. **This is the most invasive single
   change** — it rewrites how `.admet` bundles are written — so land it behind the
   relocation (step 1) with bundle round-trip tests before cellpose is wired in.
5. **Batch as actions.** Surface batch control through the ActionSpec catalog
   (`category="batch"`): e.g. `analyze_item`, `run_batch`, `load_bundle`,
   `save_project`/`load_project` — so the workflow (and any renderer/CLI) drives batch
   generically.
6. Progress reporting stays simple callbacks for now (a batch is a finite job, not a live
   stream); it does **not** depend on the deferred telemetry interface.

Net: one batch core; opencv and cellpose both plug in via the contract; the matrix stage
in `workflows/analyze.py` is backed by it.

---

## Other refactors

### A. Remove presentation params from the control engine's schema
The control engine declares CUT presentation params as if they were device settings:
`camera_waterfall`, `camera_ruler_*`, `camera_rotation`, `camera_flip_*`,
`camera_selection_*`, `camera_offset_*` (`engine.py:79-89`), and acts on them in
`_apply_camera_configuration` (waterfall `Height=1`, offset application). These are
overlay/renderer concerns and CUT per `master_plan.md:153-154`. Keep only real device
settings (Width, Height, Exposure, SensorReadoutMode, Gain, PixelFormat, Binning,
AcquisitionFrameRate). Safe to delete now — no renderer consumes them yet. (Frame
*delivery* cleanup is deferred with telemetry.)

### B. ActionSpec catalog + migrate `run_action` (control + both analyze engines).

### C. Enforce the result contract (#3) — rewrite control `_status_result`.

### D. Symmetric, lazy engine registry
Control is currently **not** registered; `app.py` imports `create_control_engine`
directly, so importing the app eagerly pulls the control stack
(`engines/control/engine.py:24-40`). Register `control` in a lazy registry exactly like
`engines/analyze/registry.py`, so a control-only box never imports torch and an
analyze-only box never imports pypylon/Fluigent. Discovery goes through
`registry.available_ids()`. Rename the engine id `fluidics` → `control`.

### E. Wire orphaned capabilities as actions
Backend exists + is unit-tested, but no action exists, so no renderer/CLI can reach them
(all KEEP items in `master_plan.md`):
- **Emergency stop** — `channels.py:160 emergency_stop_all()` → `emergency_stop`
  (`destructive=True`).
- **Manual per-channel flow / pressure / stop** — `channels.py:43-92` →
  `set_channel_flow` / `set_channel_pressure` / `stop_channel`.

### F. Recording auto-stop must not desync streams
Camera auto-stop on `max_frames`/`max_time` happens inside the acquisition thread
(`camera/acquisition.py:91-92`) but never notifies `RecordingSession`, so fluidics CSV
keeps logging and `summary.json` is never finalized (`session.py:116-144`). Add an
`on_recording_complete` callback to `CameraAcquisitionThread` and have `RecordingSession`
pass one that stops both streams together.

### G. Logic-fidelity fixes (parity + correctness)
- **scanprotocol** — restore grid scoring tuple to source order `(ratio_err, empty)`
  (swapped to `(empty, ratio_err)` at `cellpose/scanprotocol.py:56`); plan said move
  "as-is".
- **correction** — `detected=False` on surviving droplets (`cellpose/correction.py:35`)
  **looks** inverted, but this is an audit suspicion, not a confirmed bug. Verify against
  the source project's behavior **before** changing anything; if source matches, leave it
  and note why.
- **cellpose headless `detect()`** — finish the extraction (`master_plan.md:131-133`): a
  pure `detect()` returning records + viz data + layout, file IO/writer split out of
  `CellposeDetection.run()` (still inline, gated only by a `write_artifacts` flag). The
  three called-out fixes (sys.exit→raise, configurable cache dir, inclusion params in
  cache hash) are already done — keep.
- **stats/** stays pure numeric (no plotting) — add a guard test.

### H. Logging — one neutral logger
`core/logging/setup.py` is the single logger. No `print`/`tqdm` in engine compute paths.
(Structured log *streaming* is deferred with telemetry.)

---

## Phased order (headless; each phase ends green with tests, no GUI)

1. **Contract layer.** Add `ActionSpec` (`core/engine/actions.py`); extend the `Engine`
   protocol with `actions`; wire runner validation. Unit-test in isolation.
2. **Result contract.** Enforce metadata-vs-records-vs-artifacts; rewrite control
   `_status_result`; test asserts status results carry **0 wide records**.
3. **Batch core (analyze).** Relocate + generalize `BatchRunner`/bundle to drive the
   engine contract; aggregate into a batch `ResultSet`; per-engine save/load/matches
   seam; batch actions. Run the **same** batch over opencv and cellpose in tests.
4. **Control engine cleanup.** Remove presentation params (A); migrate `run_action` to
   the catalog (B); rename id, register lazily (D); verify install split by import-probe.
5. **Capabilities + recording integrity.** Wire E-STOP / manual channel actions (E); add
   the auto-stop callback + camera↔fluidics sync test (F).
6. **Analyze logic fixes.** scanprotocol revert, correction flag, cellpose `detect()`
   extraction, stats purity guard (G); logging unification (H).

GUI renderers and telemetry consume this contract **later** — not part of this plan.

---

## Verification (headless only)

- `make test-core` / `test-analyze` / `test-control` / `test-all` green; `lint`/`fmt`
  clean.
- **Contract tests:** every registered engine exposes a valid `ParamSchema` and a
  non-empty `ActionSpec` catalog; `run_action` rejects unknown ids and invalid params;
  status results contain no tabular blob.
- **Batch parity:** one `BatchRunner` runs a multi-item matrix through **both** opencv and
  cellpose via the contract; per-item bundles written; cache-skip honored on re-run;
  aggregate `ResultSet` correct.
- **Install split:** `admet[control]` imports without torch/cellpose; `admet[analyze]`
  without pypylon/Fluigent/pywebview; registry exposes only installed engines.
- **Recording sync:** simulated run with `max_frames` proves camera auto-stop also stops
  fluidics logging and finalizes `summary.json`.
- **Analyze parity:** dropdrop vs admet on the same z-stacks — records + stats match;
  scanprotocol layout matches source after the revert.

---

## Deferred (later — explicitly not now)

- **Telemetry / live streaming:** a rendering-neutral push/pull interface for camera
  frames (`ndarray`, never base64), sensor snapshots, pipeline events, and structured
  logs. This is also where the control engine's web-encoding leak gets removed
  (`_frame_to_data_uri`, `camera_preview_src`, `imencode` — `engine.py:580-603, 468-522`).
  Needed only when a GUI exists.
- **Renderers:** Qt+pyqtgraph (control), NiceGUI (analyze), CLI — each a thin consumer of
  the contract above.
- **Static export** (`admet[export]`).
