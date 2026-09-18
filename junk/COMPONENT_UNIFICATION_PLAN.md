# Plan — component-based UI unification (control + analyze)

## The idea

Both apps are the **same 9 components in the same shell**. Only the **Editor** differs.
Build each component once (designed, data-driven), compose them with one fixed layout,
and make the Editor a per-stage plugin. Kill all per-stage duplication.

```
┌───────────────────────────────────────────────────────────────┐
│ (2) PROJECT PANEL  — select / create / load (top bar)          │
├────────────┬──────────────────────────────────────────────────┤
│ (1)        │ (3) ACTION PANEL   — progress + primary buttons   │
│ WORKFLOW   │ (4) SETTINGS PANEL  (control)  ── ParamSchema form │
│   ToC+dots │     · or ·                                          │
│            │     MATRIX PANEL    (analyze)  ── ParamSchema table │
│ (8) INSTR  │ (5) EDITOR PANEL   — UNIQUE PER STAGE (the plugin) │
│ (9) NOTIF  │ (6) RESULTS PANEL                                  │
│            │ (7) LOG PANEL                                      │
└────────────┴──────────────────────────────────────────────────┘
```

Components **1,2,3,4,6,7,8,9 are generic** — identical contract in both apps.
**Component 5 (Editor) is the only per-stage code.** In control it's one shared editor
for every stage; in analyze it's registered per stage (import / video / imaging / view).

## Why this is not the failed presenter rewrite

The presenter attempt failed because it **rewrote** screens from scratch and reduced the
custom panels to placeholders, losing the design. This plan does the opposite:

- The recovered `analyze.py` **already contains these 9 components** as methods
  (`_mount_topbar`, `_render_toc`, `_mount_action_box`, `_mount_action_panel`,
  `_mount_main_window`, `_mount_results`, `_mount_log`, `_render_instruction_card`,
  `_render_notice_card`). Same for `control.py`. We **extract and formalize** those —
  design and markup preserved verbatim — into components. Nothing is redesigned.
- The only genuinely-new work is (a) making Settings/Matrix read a `ParamSchema` instead
  of hardcoded columns, and (b) turning the per-stage `if stage.id == ...` bodies of the
  Editor into a registry.

---

## Component contracts (toolkit-free view models)

One presenter builds these from `Workflow + state + runtime`. Pure data, no toolkit.

| # | Component      | VM (what it renders)                                                    | Events out            |
|---|----------------|------------------------------------------------------------------------|-----------------------|
| 1 | Workflow ToC   | `steps: [{id,label,status}]`, `current`                                 | `activate(stage)`     |
| 2 | Project panel  | `name, choices, root, can_create, can_load`                            | `select/new/load`     |
| 3 | Action panel   | `progress: 0..1`, `buttons: [{label,command,variant,enabled}]`         | `command(cmd)`        |
| 4a| Settings panel | `schema: ParamSchema`, `values` (control)                              | `set(field,value)`    |
| 4b| Matrix panel   | `schema: ParamSchema`, `rows: [target]` (analyze)                      | `set(uid,field,value)`|
| 5 | Editor panel   | `stage_id` + `runtime` handle (opaque payload; the editor owns it)     | editor-specific       |
| 6 | Results panel  | `columns, rows` (+ optional charts)                                    | —                     |
| 7 | Log panel      | `lines`                                                                | —                     |
| 8 | Instruction    | `title, lines`                                                         | —                     |
| 9 | Notification   | `message, kind`                                                        | —                     |

**Settings (4a) and Matrix (4b) are the same `ParamSchema` rendered two ways:** a form
(one field per `Param`, vertical) vs a table (one column per `Param`, one row per target).
`Param.kind` (PATH/TEXT/INTEGER/FLOAT/BOOLEAN/CHOICE) drives the input widget **and** the
value cast — one mapping, used by both. This is what deletes the three
`_*_matrix_columns/_row/_wire` copies and the `_NUMERIC/_FLOAT/_TEXT_SETTING_FIELDS` sets.

**Single source of truth:** the engine `ParamSchema` (`OPENCV_SETTINGS`,
`CELLPOSE_SETTINGS`, `CONTROL_ENGINE_SETTINGS`). Delete the drifted `Stage.settings`
duplicates so a param is declared exactly once.

## The Editor registry — the only per-stage code

```python
# analyze: one editor per stage
ANALYZE_EDITORS = {
    "import":  ImportInventoryEditor,   # project inventory + source preview
    "video":   OpencvEditor,            # video preview + ROI/frame sliders (75/25)
    "imaging": CellposeEditor,          # image preview + correction counters
    "view":    ResultsEditor,           # the droplet plots
}
# control: one editor, all stages
CONTROL_EDITOR = ControlEditor          # camera preview / live plots / channel manager
```

The shell asks the registry for `editor_for(app, stage_id)` and drops it into slot (5).
Changing a stage's editor touches **only its registered class** — never the shell,
never the other 8 components.

---

## Layout / shell

One `Shell` per toolkit composes the 9 components in the fixed grid above and wires each
component's events to the command router. The Shell is ~80 lines and identical in
structure between apps; only the toolkit primitives differ (NiceGUI DOM vs Qt widgets).

## Cross-toolkit reality (control=Qt, analyze=web)

The toolkits can't share literal widgets, so we share the layers that matter:

- **Shared, toolkit-free:** the 9 VMs + presenter, the command router, the `ParamSchema`
  → widget-kind mapping (as data), the layout contract, and the Editor registry *keys*.
- **Shared design:** one `design.py` token module (colors/spacing/variants/step-dot
  colors) → a Qt QSS emitter and a web CSS emitter. Same tokens → identical look.
- **Per toolkit (written once):** 9 component renderers + the editor renderers. Each reads
  a VM and paints with tokens. Because they read the same VMs and tokens, control and
  analyze are visually and behaviourally identical except in the Editor.

So "identical components" = same VM + same tokens + same layout, rendered natively per
toolkit — not shared pixels.

---

## Migration (extract, don't rewrite — keep the recovered design)

**Phase 0 — Presenter + VMs (analyze).** Add `ui/components.py` with the 9 VM dataclasses
and `build_screen(workflow, state, runtime) -> {vm per component}`. No rendering yet; unit-test it.

**Phase 1 — Extract generic components (analyze).** Move the *existing* markup from
`analyze.py`'s `_mount_topbar/_render_toc/_mount_action_box/_mount_results/_mount_log/
_render_instruction_card/_render_notice_card` into 7 component render functions that take a
VM. Verbatim markup — design unchanged. Shell composes them.
Commit: `refactor(ui): extract generic panels as components`.

**Phase 2 — Schema-driven Settings/Matrix.** Replace the three
`_*_matrix_columns/_row/_wire` + field-set constants with one
`render_matrix(schema, rows)` and one `render_settings(schema, values)`, both reading the
engine `ParamSchema`; `Param.kind` drives cell type + cast. Delete `Stage.settings`
duplicates. This is the fix for the duplication you flagged.
Commit: `refactor(ui): schema-driven settings and matrix`.

**Phase 3 — Editor registry (analyze).** Turn `_mount_main_window`'s `if stage.id`
branches into registered editors (import/video/imaging/view), each owning its recovered
markup (opencv 75/25 editor, cellpose editor, view plots).
Commit: `refactor(ui): per-stage editor registry`.

**Phase 4 — Design tokens.** Extract `design.py`; `analyze` CSS derives from it. Verifies
the token path before control needs it.
Commit: `refactor(ui): single design-token source`.

**Phase 5 — Control onto the same components (Qt).** Reimplement the 9 component renderers
+ the one shared `ControlEditor` in Qt, reading the same VMs and tokens (QSS from
`design.py`). Delete the per-stage bodies in `control.py`.
Commit: `refactor(ui): render control from shared components`.

---

## Acceptance criteria

1. Adding a stage or a `Param` touches **only** the workflow/`ParamSchema` (+ an editor
   class if the stage needs a custom editor). No shell or component edits.
2. `grep "stage.id ==" src/admet/ui` → matches only inside the Editor registry.
3. One matrix renderer, one settings renderer, one change handler — no `_opencv_*` /
   `_cellpose_*` matrix triplets, no `_NUMERIC/_FLOAT/_TEXT` field sets.
4. Each `Param` declared once (engine `ParamSchema`); no `Stage.settings` copies.
5. Control and analyze share the VM layer, the design tokens, and the layout contract;
   only Editor renderers and toolkit primitives differ.
6. Design matches the recovered look (extracted markup, not reinvented). `make test-all`
   green.

## Guardrails
- Extract markup verbatim in Phase 1/3 — do not "clean up" the design while moving it.
- The Shell never branches on stage identity; only the Editor registry may.
- A `Param` or color literal that appears in a component renderer (not in schema/tokens)
  is a bug.
