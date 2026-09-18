• Full execution plan with “move first, delete last” discipline.

Rule For All Phases
No code is deleted until its replacement is wired, imported, tested, and committed. For every removal:

1. Move or copy the useful code into the new owner.
2. Update imports/callers.
3. Run focused tests.
4. Only then remove the old file/package.
5. Run tests again.

Phase 1: Flatten Core Contracts
Target:

core/
  api.py
  discovery.py
  engine.py
  project.py
  run.py
  session.py

Move/recycle:

- core/schema/params.py -> core/engine.py
- core/schema/__init__.py exports -> core/engine.py
- core/engine/base.py, actions.py, registry.py -> core/engine.py
- Keep core/run.py for RunJob, RunResult, sinks.
- Replace imports from admet.core.schema and admet.core.engine.*.

Remove only after imports pass:

- core/schema/
- core/engine/
- core/logging/

Logging replacement:

- Keep no custom logging package.
- Modules use logging.getLogger(__name__).
- Only app.py configures process logging if needed.

Commit:
refactor(core): flatten engine contracts

Phase 2: Make Workflows The Sequencing Layer
Target:

workflows/
  model.py
  runner.py
  analyze.py
  control.py

Move/recycle:

- core/workflow/model.py -> workflows/model.py
- core/workflow/runner.py -> workflows/runner.py
- analyze/runner.py -> workflow-owned analyze orchestration.
- Existing workflows/analyze.py workflow definition stays and receives/imports the runner logic.
- Existing workflows/control.py becomes the only owner of control stage/protocol sequencing.

Remove only after callers/tests pass:

- core/workflow/
- root analyze/

Commit:
refactor(workflows): centralize orchestration

Phase 3: Normalize Engine Public Surface
Target:

engines/
  registry.py
  analyze/
    opencv.py
    cellpose.py
  control/
    camera.py
    fluidics.py

Move/recycle:

- engines/analyze/registry.py + engines/control/registry.py -> one engines/registry.py.
- engines/analyze/opencv/engine.py public engine facade -> engines/analyze/opencv.py.
- Keep OpenCV helper code under private implementation modules only if needed.
- engines/analyze/cellpose/engine.py public facade -> engines/analyze/cellpose.py.
- Control camera logic from control/camera/ becomes public engines/control/camera.py facade.
- Control fluidics logic from control/fluidics/ becomes public engines/control/fluidics.py facade.

Do not delete helper code yet:

- Camera acquisition/video/backend helpers stay until facades are stable.
- Fluidics hardware/acquisition/channel helpers stay until facades are stable.

Remove after replacement:

- separate registry modules
- old public engine facades

Commit:
refactor(engines): unify public engine surface

Phase 4: Remove Engine-Owned Project/Metadata Logic
Target behavior:

- Engines do not create project structure.
- Engines do not update .admetp.
- Engines do not aggregate reports/tables/plots.
- Engines only read inputs/settings and write raw streams/artifacts to provided outputs.

Move/recycle:

- Useful recording filesystem layout logic from engines/control/recording.py -> core/project.py or core/
  session.py.

- Useful recording runtime coordination that truly belongs to hardware stays with workflow/control runner, not
  engine metadata.

- metadata.json creation for control records moves to core project/session persistence.
- Analyze cache/raw path selection stays in workflow/core runner, not engines.
- Raw writing remains through RunJob.outputs / raw sink.

Remove only after tests prove current behavior:

- engines/control/recording.py
- any engine-side .admetp/metadata mutation
- engines/analyze/stats/dropdrop.py

For dropdrop stats:

- Do not delete its formulas blindly.
- If any formulas are still needed for view/export, move them to workflow/UI result processing with a neutral
  name, for example workflows/results.py or later ui view helpers.

- Engines stop computing/exporting those tables.

Commit:
refactor(core): centralize run persistence

Phase 5: Move Control Pipelines Out Of Engines
Target:

- Pipeline/protocol sequencing belongs to workflows/control.py.
- Fluidics engine only exposes primitives: connect, disconnect, set flow, set pressure, read sensors, stop.
- Camera engine only exposes primitives: connect, disconnect, configure, live, snapshot/record primitive if
  needed.

Move/recycle:

- engines/control/pipeline/steps.py, triggers.py, engine.py logic -> workflows/control.py or workflow helper
  functions.

- Existing protocol parameters from engines/control/fluidics/config.py move to workflow-owned protocol definitions
  if they define sequence.

- Low-level fluidics channel constants can remain in engine/control if they are hardware mapping, not workflow
  sequence.

Remove after wired:

- engines/control/pipeline/

Commit:
refactor(workflows): own control protocols

Phase 6: UI Shared Structure
Target:

ui/
  theme.py
  scaffold.py
  render.py
  analyze.py
  control.py

Move/recycle:

- From ui/analyze/renderer.py:
    - CSS/colors/sizing -> ui/theme.py
    - workflow/sidebar/panel/action specs -> ui/scaffold.py
    - mount/sync signatures/state rules -> ui/render.py
    - NiceGUI adapter code -> ui/analyze.py
    - media preview helpers -> keep temporarily in ui/analyze.py or move to workflow/result helpers if not
      framework-specific.

- From ui/control/window.py:
    - colors/sizes -> ui/theme.py
    - action box/panel/table specs -> ui/scaffold.py
    - mount/sync logic -> ui/render.py
    - PySide widget translation -> ui/control.py

Do not delete old files until:

- admet analyze imports from ui/analyze.py.
- admet control imports from ui/control.py.
- Both targets start/import cleanly.
- Tests pass.

Remove after wired:

- ui/analyze/
- ui/control/
- old megafiles.

Commit:
refactor(ui): introduce shared scaffold

Phase 7: Final Source Root Cleanup
Target source root exactly:

src/admet/
  app.py
  core/
  workflows/
  engines/
  ui/
  vendor/

Actions:

- Remove empty packages.
- Remove stale __init__ exports.
- Remove tracked .DS_Store if present.
- Update tests/imports/docs.
- Run full available test suite.

Commit:
chore: remove stale source layout

This sequence avoids the dangerous path of deleting first. Every phase starts by moving useful code into its new
owner, then rewiring, then deleting the old location only after tests prove the new path works.