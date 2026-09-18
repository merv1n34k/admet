# Agent instructions — restore the ORIGINAL design, keep ALL current logic

## What this is / isn't

- **KEEP** every bit of current logic and engine work: `presenter.build_screen`/`ScreenModel`,
  the command router, `WorkflowRunner`, runtimes, and everything under `core/`, `engines/`,
  `workflows/`. **No git reset. Do not revert engine commits.**
- **RESTORE** the original *look* (CSS/QSS, layout, widgets) on top of that logic. The design was
  deleted in commits `40d4732` (web) and `8a8be6e` (Qt). The last-good design is at commit
  **`c809d7a`** (2026-07-03 21:45). A backup of the current state is on branch
  `backup/ui-rewrite-20260704`.
- **Control must end up looking EXACTLY as it did originally.** Its Qt theme and widgets are
  restored verbatim; only the wiring to the presenter is new.

## Files you may touch (design layer only)

`src/admet/ui/nicegui_app.py`, `src/admet/ui/qt_app.py`, `src/admet/ui/theme.py`,
`src/admet/ui/surfaces/*`, and CSS/QSS strings. **Do NOT edit** `presenter.py`, the command
router, `runtimes` data logic, `core/`, `engines/`, `workflows/`. (Adding a *field* to
`ScreenModel` to carry a layout region is allowed; changing existing logic is not.)

---

## The original layout (restore for BOTH apps — this is the target)

From `c809d7a:src/admet/ui/analyze.py`. Reproduce this exact structure in both adapters:

- **Top bar** — project select / create / load. Ref `_mount_topbar` (line 155).
- **Left sidebar** — workflow **ToC with a status dot per stage** + instructions card +
  notification card. Ref `_mount_sidebar` (180), `_render_toc` (395) — the dot is colored by
  stage status; keep that.
- **Content = 5 stacked panels**, each an `.admet-panel` > `.admet-panel-body`:
  1. **Action box** — progress indicator + primary buttons. Ref `_mount_action_box` (255).
  2. **Action panel / stage settings** — a **table** (analyze matrix OR key/value settings).
     Ref `_mount_action_panel` (289), `_render_matrix_table` (607).
  3. **Main window** — viewer/editor surface. Ref `_mount_main_window` (306).
  4. **Results** — aggregated project contents. Ref `_mount_results` (321).
  5. **Action log** — Ref `_mount_log` (344).

---

## Step 1 — Restore the web (analyze) stylesheet

Replace the 31-line stub `nicegui_app._style()` with the **full original** stylesheet:

```
git show c809d7a:src/admet/ui/analyze.py    # _style() spans line 2222 -> EOF (~390 lines)
```

Copy that entire `_style()` body verbatim into `nicegui_app._style()`. It restores every class
the layout/surfaces use: `.admet-panel`, `.admet-panel-body`, `.admet-sidebar`, `.admet-toc*`,
`.admet-viewer`, `.admet-transport*`, `.admet-roi`, `.admet-playhead`, `.comparison-grid`,
`.media-editor-grid`, button/notice/step styles.

## Step 2 — Restore the Qt (control) theme VERBATIM

The current `theme.py` (202 lines) is a gutted rewrite. Restore the original (654 lines):

```
git show c809d7a:src/admet/ui/theme.py       # full Theme + stylesheet() + widget factories
```

Bring back the complete `Theme` class, `stylesheet()` (QSS, lines 326-650), and the widget
factories (`button`, `stage_button`, `line_edit`, `int_box`, `double_box`, `combo_box`,
`check_box`, `section`, `button_row`, `field_row`, `control_row`, `toolbar`). If the current
`qt_app` references any *new* token/helper name not in the original, ADD it — do not drop the
original QSS to accommodate it. Control's colors/spacing/borders must match the original pixel-for-pixel.

## Step 3 — Reproduce the 5-panel layout in both adapters

- **nicegui_app**: reshape `_render_content` so it emits the five `.admet-panel`/`.admet-panel-body`
  panels in the order above, pulling from the existing `ScreenModel` regions (action box buttons,
  settings/matrix, surfaces, results, log) and the sidebar ToC+dots + instruction + notice.
  Use the original class names so Step 1's CSS applies.
- **qt_app**: build the same five panels using restored `theme.section()` / `field_row()` /
  `control_row()` / `button()` factories, and the sidebar ToC with status dots. Replace the flat
  `QFrame` placeholder in `_render_surface` with the real surface widgets (Step 4).

## Step 4 — Restore the rich surfaces into the `kind` registry

**Web (analyze) — from `c809d7a:src/admet/ui/analyze.py`:**
- `surfaces/video_preview.py` ← the viewer + transport + ROI markup (`_opencv_preview_html`,
  line 918, and the transport row markup) — the `.admet-viewer`/`.admet-transport`/`.admet-roi` DOM.
- `surfaces/matrix.py` ← `_render_matrix_table` (line 607).
- `surfaces/charts.py` ← the `.comparison-grid` chart cards.

**Qt (control) — from `8a8be6e^:src/admet/ui/control_widgets.py` (671 lines):**
- Restore `LivePlot`, `PlotPanel`, `PreviewDisplay`, `ChannelControlPanel`, `ChannelControlRow`,
  `FluidicsMonitorTable`, `NotificationCard`, `video_table` into `surfaces/*_qt.py`, and register
  them in `QT_SURFACES` keyed by `SurfaceVM.kind` (`camera`→PreviewDisplay, live plot→LivePlot/
  PlotPanel, channel manager→ChannelControlPanel, monitor→FluidicsMonitorTable).

Every `SurfaceVM.kind` a workflow declares must resolve to a REAL renderer in both
`NICEGUI_SURFACES` and `QT_SURFACES` — no placeholder for any shipped kind.

---

## Acceptance

1. **Look matches original.** analyze shows sidebar ToC+dots, top project bar, and the 5 panels
   with the original CSS (viewer/transport/ROI/comparison-grid present, not placeholders).
   Control renders with its original `theme.py` QSS and its live plots / camera preview / channel
   manager — visually identical to before.
2. **No difference between apps** in palette/spacing/component styling (they use the same class
   vocabulary + restored token values).
3. **Logic untouched.** `git diff c809d7a..HEAD -- src/admet/{core,engines,workflows}` and the
   presenter/command-router are unchanged by this work; engine commits `66d9c60`, `40b792d` remain.
4. `grep -rnE "stage\.id (==|in)" src/admet/ui` → 0 (do not reintroduce stage branches while
   rebuilding layout — layout comes from `ScreenModel` regions).
5. `make test-all` green on a cold run (clear `.pyc` first).

## Recovery references (exact sources)
- Web CSS + analyze layout + viewer/matrix/charts: `c809d7a:src/admet/ui/analyze.py`
- Qt theme (full QSS + factories): `c809d7a:src/admet/ui/theme.py`
- Qt control widgets: `8a8be6e^:src/admet/ui/control_widgets.py`
- Full pre-rewrite snapshot if needed: `9f20dc1` (designs intact) / branch `backup/ui-rewrite-20260704`
