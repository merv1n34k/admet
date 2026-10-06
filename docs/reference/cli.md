# Command line

Everything starts from one command, `admet`.

```text
admet control  [--project PATH]
admet analyze  [--host HOST] [--port PORT] [--projects DIR]
admet describe [TARGET]
```

## admet control

Opens the control desktop. `--project` opens an existing `.admetp` project.
Requires the `control` extra.

```bash
uv run --extra control admet control --project ~/experiments/today.admetp
```

## admet analyze

Serves project analysis over the network. Requires the `analyze` extra.

| Option | Default | Meaning |
|---|---|---|
| `--host` | `0.0.0.0` | Address to listen on (every interface) |
| `--port` | `8080` | Port |
| `--projects` | `ADMET_PROJECTS_ROOT`, else `./projects`, else `.` | Folder with the projects |

## admet describe

Lists what the Python API offers: every operation, or one operation or engine.

```bash
uv run admet describe              # everything
uv run admet describe run_steps    # one operation: its settings and triggers
uv run admet describe acquisition  # one engine
```

## Environment variables

| Variable | Meaning |
|---|---|
| `ADMET_PROJECTS_ROOT` | Default folder holding projects |
| `ADMET_FLUIGENT_SDK_PATH` | A Fluigent SDK Python folder to use instead of the bundled one |
