# UI Minimization Plan — render only what `workflows/` declares

## Goal

Delete the hand-coded per-stage UI (~6,000 lines across `ui/analyze.py`,
`ui/control.py`, `ui/render.py`, `ui/control_widgets.py`) and replace it with a
**thin, generic renderer that draws whatever a `Workflow` declares** — nothing
stage-specific. Target UI surface: ~1,200 lines total, **zero `if stage.id == ...`
branches**, adding a stage or field becomes a data edit in `workflows/`, never a UI edit.

Aligns with the recorded decision in `PLAN.md`: two renderers over one core —
**Qt+pyqtgraph for control, NiceGUI for analyze, CLI possible as a third.**

---

## Why the current UI is slop (diagnosis)

The `workflows/` model is already a complete, toolkit-agnostic description of every screen:

| Model type      | Already carries                                                                 |
|-----------------|---------------------------------------------------------------------------------|
| `Stage`         | `label, description, instructions, settings (ParamSchema), controls, surfaces, show_settings, action, skippable, confirmation_required` |
| `StageControl`  | `label, action, variant, completes, advances, skippable`                        |
| `StageSurface`  | `kind, title, settings, controls, options`                                      |
| `ParamSchema`   | ordered `Param(name, label, kind, default, min, max, options)`; `ParamKind` is a closed enum: PATH/FLOAT/INTEGER/BOOLEAN/TEXT/CHOICE |
| `Workflow`      | state machine: `initial_state, current_stage, complete_current, skip_current, rewind, pause, resume` |
| `WorkflowRunner`| `run_current(state, settings)` → dispatches `stage.action` to `engine.run()`     |

The UI ignores this and re-implements it by hand. The proof:
`workflows/analyze.py` defines `analyze_button_specs()` and `analyze_stage_instruction()`
— `if stage.id == "import": ...` functions that **re-derive** what is *already* in
`stage.controls` and `stage.instructions`. `ui/analyze.py` has 34 `_render/_mount/_sync`
methods; `ui/control.py` is 2,527 lines of per-stage Qt wiring. All of it is duplicating
the model.

**The only irreducible UI is the custom interactive *surfaces*** — declared today via
`StageSurface.kind`: `"camera"` (live preview), `"matrix"` (editable batch table), plus
analyze's video-preview-with-ROI and result charts. These can't be generated from a
schema, but they are a *small finite registry keyed by `kind`*, not per-stage code.

---

## Target architecture

```
ui/
  presenter.py        # SHARED, PURE, toolkit-free. Workflow+State+runtime -> ScreenModel   (~200)
  window.py           # KEEP AS-IS. mount/sync state machine, already unit-tested           (127)
  project.py          # KEEP AS-IS. project create/load/save helper                          (35)
  theme.py            # KEEP (trim). styling only                                            (~400)
  nicegui_app.py      # analyze adapter: paints a ScreenModel in NiceGUI + emits commands    (~300)
  qt_app.py           # control adapter: paints a ScreenModel in Qt + emits commands         (~350)
  surfaces/
    __init__.py       # registry: kind -> renderer, per toolkit                              (~40)
    camera.py         # Qt live camera preview                                               (real)
    matrix.py         # editable batch/target table (both toolkits)                          (real)
    video_preview.py  # OpenCV frame + ROI editor (analyze)                                  (real)
    charts.py         # result charts (analyze)                                              (real)
```

### 1. `ScreenModel` — the neutral hand-off (in `presenter.py`)

Pure dataclasses. No toolkit imports. This is the entire contract between logic and pixels:

```python
@dataclass(frozen=True)
class FieldVM:      # one settings input, derived from a Param
    name: str; label: str; kind: ParamKind
    value: Any; minimum: float | None; maximum: float | None
    options: tuple[ParamOption, ...]

@dataclass(frozen=True)
class ButtonVM:     # one control, derived from a StageControl
    label: str; command: str          # command == StageControl.action or a lifecycle verb
    variant: str; enabled: bool; active: bool

@dataclass(frozen=True)
class SurfaceVM:    # one custom panel, derived from a StageSurface
    kind: str; title: str
    fields: tuple[FieldVM, ...]; buttons: tuple[ButtonVM, ...]
    options: dict[str, Any]

@dataclass(frozen=True)
class StepVM:       # one stage in the stepper
    id: str; label: str; status: StageStatus; current: bool

@dataclass(frozen=True)
class ScreenModel:
    workflow_id: str
    steps: tuple[StepVM, ...]
    title: str; description: str; instructions: tuple[str, ...]
    fields: tuple[FieldVM, ...]          # from stage.settings when show_settings
    surfaces: tuple[SurfaceVM, ...]      # from stage.surfaces
    buttons: tuple[ButtonVM, ...]        # from stage.controls + lifecycle (skip/back)
```

`build_screen(workflow, state, runtime) -> ScreenModel` is the whole presenter. `runtime`
is a small per-workflow object (e.g. counts, enable/disable predicates) that supplies the
*dynamic* bits (button.enabled, live instruction text). **This replaces
`analyze_button_specs` and `analyze_stage_instruction` entirely** — those `if stage.id`
functions are deleted; their dynamic parts (`enabled = opencv_count > 0`) become small
predicate hooks on `runtime`, keyed by control `command`, not by stage id.

### 2. Command dispatch — one table, not N handlers

Every button carries a `command` string. The adapter emits `(command, current_settings)`;
a single `CommandRouter` maps command → handler:

- Lifecycle verbs (`complete`, `skip`, `back`, `pause`) → `Workflow` state-machine methods.
- `stage.action` commands (`analyze`, `run_protocol`, `connect_camera`, ...) →
  `WorkflowRunner.run_current` / engine calls (already exist).
- Surface verbs (`refresh_cameras`, `browse_source`, ...) → a registered handler.

No stage-specific UI code path — the router is data-driven off the command string.

### 3. Form generation — one factory per toolkit

`ParamKind` is a closed enum, so each adapter has exactly one `field -> widget` factory:

| ParamKind | NiceGUI            | Qt                    |
|-----------|--------------------|-----------------------|
| PATH      | input + Browse     | QLineEdit + button    |
| TEXT      | input              | QLineEdit             |
| FLOAT     | number             | QDoubleSpinBox        |
| INTEGER   | number             | QSpinBox              |
| BOOLEAN   | checkbox           | QCheckBox             |
| CHOICE    | select(options)    | QComboBox             |

~6 cases each. Every settings form in the app is generated from this. (Note: precise
numeric fields that are wrongly sliders today become correct spin/number widgets for free.)

### 4. Surfaces — the only custom code, kept behind a registry

`surfaces/__init__.py` holds `REGISTRY: dict[str, SurfaceRenderer]` per toolkit. The adapter,
for each `SurfaceVM`, looks up `vm.kind` and calls the renderer with `(vm, runtime)`. Adding
a new custom panel = register one `kind`; the workflow references it via `StageSurface(kind=...)`.
This is where genuinely-interactive code lives (camera live view, ROI editor, editable
matrix, charts) — and it shrinks because it no longer does stage wiring or mount/sync.

---

## What is deleted vs kept vs created

**DELETE (after replacement is wired + green):**
- `ui/analyze.py` (2193) — all per-stage NiceGUI code
- `ui/control.py` (2527) — all per-stage Qt code
- `ui/render.py` (611), `ui/control_widgets.py` (671) — fold the *reusable widget bits*
  into `qt_app.py`/`surfaces/`, delete the rest
- `workflows/analyze.py::analyze_button_specs`, `::analyze_stage_instruction` — replaced by
  presenter + runtime predicates

**KEEP:** `ui/window.py`, `ui/project.py`, `ui/theme.py` (trim), the whole `workflows/`
model, `WorkflowRunner`, engines, core.

**CREATE:** `ui/presenter.py`, `ui/nicegui_app.py`, `ui/qt_app.py`, `ui/surfaces/*`.

**Net:** ~6,000 → ~1,200 lines; per-stage UI code → 0.

---

## Execution (move-first, delete-last — same discipline as `new_scheme.md`)

Each phase: build new → wire → run focused tests → only then delete old → tests again → commit.

**Phase 0 — Presenter + ScreenModel (no toolkit).**
Write `ui/presenter.py` + dataclasses. Unit-test `build_screen` against
`create_analyze_workflow()` and `create_control_workflow()`: assert steps/fields/buttons/
surfaces match the declared model for every stage. Pure, fast, no GUI.
Commit: `feat(ui): add toolkit-neutral workflow presenter`.

**Phase 1 — Analyze adapter (NiceGUI).**
Build `ui/nicegui_app.py`: ScreenModel → widgets, `ParamKind` factory, command router,
surface registry (`matrix`, `video_preview`, `charts`). Wire `app.py` analyze entry to it
behind a flag. Manually verify parity, then delete `ui/analyze.py` + the two bridge funcs.
Commit: `refactor(ui): render analyze from workflow model`.

**Phase 2 — Control adapter (Qt).**
Build `ui/qt_app.py` reusing the same presenter + router; Qt factory + surface registry
(`camera`). Move the *reusable* widget helpers out of `control_widgets.py`/`render.py`,
delete the per-stage remainder and `ui/control.py`.
Commit: `refactor(ui): render control from workflow model`.

**Phase 3 — Collapse surfaces + theme.**
Ensure both adapters share `surfaces/matrix.py`. Trim `theme.py` to what the generic
renderer uses. Delete dead CSS/helpers.
Commit: `refactor(ui): consolidate surface registry`.

---

## Acceptance criteria

1. `grep -rn "stage.id ==" src/admet/ui src/admet/workflows` → **zero matches.**
2. No file in `src/admet/ui/` exceeds ~400 lines except a genuine surface renderer.
3. Adding a `Stage` (or a `Param` to `stage.settings`) changes **only** `workflows/`, and
   the new field/button/step renders in both toolkits with no UI edit.
4. `make test-all` green cold (clear `.pyc` first); presenter has direct unit tests.
5. `ui/` imports only from `core` + `workflows` (never the reverse); adapters are the only
   files that import `nicegui` / `PySide6`; `presenter.py` imports neither.

---

## Notes / gotchas for the executor

- **Surfaces need live runtime data** (frames, matrix rows, chart series). The presenter
  stays pure by putting only `kind + options` in `SurfaceVM`; the adapter's surface renderer
  receives the `runtime` handle and pulls data itself. Do **not** thread pixel data through
  the ScreenModel.
- **`show_settings=False`** (e.g. control `scene` stage) means the stage-level form is
  suppressed and settings live inside a surface instead — the presenter already honors this
  field; respect it.
- **Dynamic enable/instruction** (`opencv_count > 0`) belongs on `runtime`, keyed by control
  `command`, never by stage id. This is the one place the old `if stage.id` logic hides —
  convert it to predicates, don't reintroduce it.
- Keep `ui/window.py` untouched: it already provides the mount-once/sync state machine the
  adapters need, and it is unit-tested.
