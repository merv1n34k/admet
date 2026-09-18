# Plan — unified logic + one shared design across analyze & control

## Mandate (from product owner)

1. **Keep logic simple & unified** — one presenter drives both apps; adapters only paint.
2. **One design, no visible difference** — analyze (NiceGUI/web) and control (Qt) MUST share
   an **identical color palette + spacing + component styling** and the **identical layout**.
   Fidelity = *shared design language*: same tokens/layout/vocabulary, each rendered
   toolkit-native (Qt widgets vs DOM) — but **no palette/CSS/layout/behavior difference may be seen.**

### Canonical layout (both apps, identical structure)
- **Left panel:** workflow ToC (one row per stage, each with a small **progress-indicator dot**
  colored by stage status — as the old control panel had) + **instructions card** + **notification card**.
- **Top bar:** project **select / create / load**.
- **Main window**, in this order:
  1. **Action box** — progress indicator + primary control buttons.
  2. **Stage settings** — a **table** that holds *either* the analyze **matrix** *or* regular
     key/value settings (one table component, two data shapes).
  3. **Editor / viewer** — the main visual surface (video viewer / camera preview / live plot).
  4. **Results panel** — aggregates what the project contains (stored runs, recordings, summaries).
  5. **Action log** — actions performed this run session.

---

## Current state (post-refactor, reviewed)

**Good — logic is genuinely unified. Do not regress this.**
- `presenter.build_screen(workflow, state, runtime) -> ScreenModel` drives both `nicegui_app.py`
  and `qt_app.py`. Command router + `WorkflowRunner` shared. `stage.id ==` count = 0.
- 2413 lines total (from ~6000+). Tests green (134/13/37).

**Broken — design is neither shared nor present.**
- **Two design systems, zero shared tokens:** `qt_app` uses `theme.py` (Qt QSS + colors);
  `nicegui_app` has its own inline `_style()` (web CSS, hardcoded colors). They will drift.
- **Rich surfaces deleted to git; stubs shipped:** every `SurfaceVM.kind` renders a placeholder.
  `control_widgets.py` (LivePlot, PreviewDisplay, ChannelControlPanel, FluidicsMonitorTable) was
  **deleted** (recover from commit `8a8be6e^`). Analyze viewer/transport/ROI/comparison-grid
  recover from `40d4732^:src/admet/ui/analyze.py` (392-line CSS + markup).
- **One behavior divergence:** `qt_app.py:355-379` (`_collapsed_params`/`_ordered_params`/
  `_correction_primary_params`, keyed on `stage.id in {"corrections"}`) exists only in the Qt
  adapter — control reorders/collapses that stage's fields, analyze does not. The `stage.id ==`
  gate missed it because it uses `in {...}`.

---

## Target architecture

```
ui/
  design.py        # NEW. SINGLE source of truth: palette, spacing, typography, radii,
                   #   variant->style, step-status->dot color, notice-kind->color. Toolkit-free.  (~150)
  theme_qt.py      # Qt QSS generated from design.py tokens (rename/refit of theme.py)             (~200)
  theme_web.py     # NEW. Web CSS generated from the SAME design.py tokens                          (~200)
  presenter.py     # extend ScreenModel with the named layout regions below                         (~300)
  window.py        # KEEP. mount/sync state machine                                                 (127)
  project.py       # KEEP                                                                            (35)
  nicegui_app.py   # paints ScreenModel regions in DOM; imports theme_web; NO private _style()       (~250)
  qt_app.py        # paints ScreenModel regions in Qt; imports theme_qt; NO stage.id logic            (~350)
  runtimes: analyze_runtime.py, control_runtime.py   # KEEP (data providers per app)
  surfaces/
    __init__.py    # NICEGUI_SURFACES + QT_SURFACES registries, keyed by SurfaceVM.kind
    video_preview.py  charts.py  matrix.py            # web renderers (restore rich markup)
    camera_qt.py  liveplot_qt.py  channels_qt.py  monitor_qt.py   # Qt renderers (restore from git)
```

### ScreenModel gains named regions (so BOTH adapters build the identical layout)

The layout must be data, not adapter code. Extend `ScreenModel`:

```python
@dataclass(frozen=True)
class ScreenModel:
    workflow_id: str
    steps: tuple[StepVM, ...]            # left ToC; StepVM.status -> dot color
    title: str; description: str
    instructions: tuple[str, ...]        # left instructions card
    notice: NoticeVM | None              # left notification card (message + kind)
    project: ProjectBarVM                # top bar: name, choices, can_create/load
    action_box: ActionBoxVM              # progress (fraction/label) + primary ButtonVMs
    settings: SettingsTableVM            # rows: either matrix rows or key/value field rows + `mode`
    surfaces: tuple[SurfaceVM, ...]      # main viewer/editor region
    results: ResultsVM                   # aggregated project contents
    log: tuple[str, ...]                 # action log lines
```

`build_screen` fills every region from the `Workflow`/`Stage` declaration + `runtime`. All
per-stage shaping (ordering, collapsing, progress %, which settings mode) happens **here, once**,
so both apps are identical by construction. `runtime` (analyze/control) supplies only *data*
(matrix rows, log lines, results, live surface handles), never layout.

### One palette, two emitters (the "no visible difference" guarantee)

`design.py` holds the tokens *as data*. `theme_qt.stylesheet()` and `theme_web.stylesheet()`
are pure functions of those tokens. Same variant → same color/border/radius in both. Change a
token once → both apps move together. Neither adapter may define a color/spacing literal.

---

## Execution (move-first, delete-last; test gate every phase)

**Phase 0 — Single token source.**
Extract every color/spacing/type value from `theme.py` + `nicegui_app._style()` into `design.py`.
Refit `theme.py`→`theme_qt.py` to derive from it; write `theme_web.py` from the same tokens;
`nicegui_app` imports `theme_web` and **deletes** its private `_style()`. Verify both apps use
the same palette (temporary side-by-side screenshot).
Commit: `refactor(ui): single design-token source for both apps`.

**Phase 1 — Unify behavior + layout regions.**
Move `qt_app._collapsed_params/_ordered_params/_correction_primary_params` into `presenter`
(field ordering/grouping becomes part of `SettingsTableVM`). Extend `ScreenModel` with the named
regions. Rewrite both adapters to render the **identical** region contract (left ToC+dots, top
project bar, action box, settings table, viewer, results, log). Add presenter unit tests asserting
both workflows yield the full region set and identical field ordering.
Commit: `refactor(ui): shared layout regions in presenter`.

**Phase 2 — Restore rich surfaces into both registries.**
Recover `control_widgets.py` from `8a8be6e^` → split into `surfaces/*_qt.py` (camera preview,
live plot, channel manager, monitor) and register in `QT_SURFACES`. Recover the analyze viewer/
transport/ROI/comparison-grid markup from `40d4732^:ui/analyze.py` → `surfaces/video_preview.py`
+ `charts.py` in `NICEGUI_SURFACES`. Every shipped `kind` gets a real renderer in **both**
registries. No placeholder for any kind a workflow actually declares.
Commit: `feat(ui): restore rich surfaces via kind registry`.

**Phase 3 — Settings-as-table + results aggregation + ToC dots.**
One table component renders both `mode="matrix"` and `mode="settings"`. Fill `ResultsVM`
(stored runs, recordings, summaries) in both runtimes. ToC dots colored by `StepVM.status` from
`design.py` status colors — identical in both apps.
Commit: `feat(ui): unify settings table, results panel, toc dots`.

---

## Acceptance criteria

1. **One palette.** `grep -rnE "#[0-9a-fA-F]{6}" src/admet/ui/{nicegui_app,qt_app}.py` → 0
   (all colors come from `design.py`). `nicegui_app._style()` deleted.
2. **No design difference.** Both apps show: left ToC with status dots + instructions + notice;
   top project bar; main = action box → settings table → viewer → results → log. Same colors,
   spacing, button/step/notice styling (visually verified side-by-side).
3. **Behavior unified.** `grep -rnE "stage\.id (==|in)" src/admet/ui src/admet/workflows` → 0.
   Field order/grouping identical in both apps (presenter test proves it).
4. **Surfaces real.** Every `SurfaceVM.kind` a workflow declares has a renderer in BOTH
   `NICEGUI_SURFACES` and `QT_SURFACES`; zero placeholder stubs for shipped kinds.
5. **Logic stays thin.** Adapters contain no domain/persistence logic and no color literals;
   `presenter.py`/`design.py` import neither `nicegui` nor `PySide6`.
6. `make test-all` green on a cold run (clear `.pyc` first); presenter has region + ordering tests.

---

## Guardrails (so this isn't "code moved, not unified" again)
- The layout is **in the ScreenModel**, not in either adapter. If an adapter has an
  `if`/`match` on stage identity or a hardcoded panel order, it is wrong.
- A color or spacing literal in an adapter is a bug — it must come from `design.py`.
- A surface `kind` shipped by a workflow with only a placeholder renderer is incomplete.
- Recover design from git; do not re-invent it (it drifts).
