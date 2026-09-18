# Markup-driven protocols: design note and migration plan

Status: in progress. This note is the plan the work follows, and is updated as
phases land. It records what must stay true, what is being added, and what has
not been done yet.

## Why

Protocols live in Python today (`engines/acquisition/protocol.py`), so a new
experiment means a code change, a release, and a developer. The acquisition
engine also *owns* protocol meaning, which is the wrong layer: the engine should
drive hardware, not know what "Drop-Seq" is. Behaviour is additionally keyed off
human-readable strings -- a protocol's display name decides which stage records a
check, and a confirmation *sentence* decides when recording starts -- so editing
a prompt can change what the instrument does.

## Invariants

These hold before and after every phase. A change that breaks one is a bug, not
a trade-off.

1. **Simulation only in tests.** No test may touch real hardware. The simulated
   backend stays the default in every automated path.
2. **No code execution from data.** A protocol file can never cause Python to be
   imported, evaluated or called by name. Substitution is a closed, typed
   grammar; anything unrecognised is an error, not a pass-through.
3. **Nothing actuates before validation.** Every setpoint is checked against the
   connected hardware's real ranges, the protocol's declared limits and the
   project's limits *before* the first hardware call of a run.
4. **Semantic names only.** Protocol files name `oil`, `cells`, `beads`. They
   never carry SDK indices. Resolution happens through a map verified against
   the attached hardware.
5. **Emergency stop is independent.** E-STOP does not route through protocol
   state and cannot be disabled by a protocol.
6. **Interrupted runs leave channels explicitly safe.** Cancellation, error and
   timeout paths all run the declared finalisation.
7. **A timeout never looks like success.** Every bounded wait reports which of
   the two happened, and that reaches the stored events.
8. **Existing projects stay readable.** Manifests written before this work load
   unchanged; any migration is versioned and additive.
9. **Existing built-ins keep their behaviour** until their YAML equivalent is
   covered by simulated end-to-end tests, and only then is the Python removed.

## Architecture

The protocol domain sits outside the hardware engine, in
`src/admet/workflows/protocols/`:

| module | owns |
| --- | --- |
| `model.py` | typed protocol, parameter, step, capability, safety and event models |
| `loader.py` | safe YAML reading, discovery across the search path, content hashing |
| `validator.py` | schema, semantic, capability and safety validation; errors vs warnings |
| `compiler.py` | parameter binding, substitution, deterministic loop/matrix expansion, stable step ids |
| `runtime.py` | execution against an adapter over the existing `PipelineEngine` |
| `artifacts.py` | run directory: source protocol, resolved protocol, events, samples, summary |
| `analyzers.py` | registry of trusted, typed analyzers (no callables named from YAML) |

The acquisition engine keeps doing what it does well -- driving channels and the
camera. It gains no knowledge of protocols; the runtime adapter translates
compiled steps into the engine's existing step representation.

`hardware_map.py` (in the fluidics package, next to the hardware it describes)
holds the persisted semantic -> physical map and its verification.

## Phases

Each phase is independently testable and leaves the application working.

1. **Domain core.** `model`, `loader`, `validator`, `compiler` with tests. No UI,
   no runtime, nothing wired in. Proves parsing, rejection, substitution,
   expansion and stable ids.
2. **Hardware identity.** Persisted semantic map, verification on connect,
   refusal on ambiguity or change. Range discovery for validation.
3. **Safety validation.** Static checking of every expanded setpoint against
   hardware ranges, protocol limits, project limits and headroom.
4. **Runtime and events.** Adapter driving `PipelineEngine`; typed events with
   outcomes; artifacts written atomically and registered in the manifest.
5. **Built-in migration.** Priming, Wash, Drop-Seq, Characterise, Gravimetry as
   YAML, each with simulated end-to-end coverage, simplest first. Python
   definitions deleted only after their replacement is covered.
6. **UI.** Discovery, selection, validation report, generated parameter form,
   expanded preview, explicit recording state. Removal of name- and
   text-keyed behaviour.
7. **Hydrostatic oil density.** The acceptance protocol plus its registered
   analyzer, demonstrating a new experiment with no Python protocol change.
8. **Documentation.** Schema reference, authoring guide, migration notes.

## Decisions

- **YAML parsing uses PyYAML's `safe_load`.** PyYAML is currently present only
  as a transitive dependency; it must be declared explicitly before the loader
  can rely on it. See open questions.
- **Substitution is `${name}` against bound parameters and loop variables only.**
  No arithmetic, no attribute access, no function calls. A reference to an
  unknown name is an error at compile time, not a runtime surprise.
- **Adapter over rewrite.** The existing `PipelineEngine` keeps running steps;
  the compiler emits its step representation. This keeps the tested execution
  path and confines new risk to translation.
- **Adaptive branching is out of scope for v1** and is rejected explicitly by the
  validator rather than silently ignored.

## Open questions

- Declaring PyYAML as a direct dependency: required for the loader, currently
  transitive. Needs sign-off before it goes in `pyproject.toml`.

## Not done yet

Everything from phase 2 onward. This section shrinks as phases land; anything
listed here is not implemented, whatever the surrounding prose implies.
