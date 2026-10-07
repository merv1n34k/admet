# Development

## Setup

```bash
make setup     # uv sync --all-extras
make dev       # run admet control
```

## Make targets

| Target | Does |
|---|---|
| `make setup` | Install every extra |
| `make dev` | Run `admet control` |
| `make build` | Build the wheel and sdist |
| `make test` | Core and control tests |
| `make test-integration` | Simulated end-to-end runs, protocols and the desktop backend |
| `make test-analyze` | Analysis engines and the analyze app |
| `make test-all` | All of the above |
| `make lint` / `make fmt` | Ruff check / format |
| `make docs-dev` | Serve this documentation locally |
| `make docs-build` | Build the documentation site |
| `make publish-upstream [TAG=v…]` | Push master to the YP-Biotech repository; with `TAG`, also tag a release |
| `make clean` | Remove build and cache folders |

Tests use the Fluigent simulator and mocked cameras; no physical device is
needed. Simulation does not validate wiring, liquid calibration or Windows
drivers — check those on the instrument PC.

## Layout

```text
src/admet/
  app.py          # the admet command
  core/           # service, projects, discovery, plans
  engines/        # acquisition (Fluigent, camera), OpenCV, Cellpose
  workflows/      # protocol format, calculations, analysis runner
  ui/             # control (Qt) and analyze (NiceGUI)
templates/        # bundled protocols
tests/
docs/             # this site (VitePress)
```

## Adding a calculation

Register it in `CALCULATIONS` in `workflows/calculations.py` with a label,
version, `calculate` function and the run files it reads, add its schema to
`workflows/calculation_schema.py`, and give it a view in `result_view`. Protocols
then declare it in their `calculations` list.

## Documentation

The site is built with [VitePress](https://vitepress.dev) and [Bun](https://bun.sh):

```bash
cd docs
bun install
bun run dev        # http://localhost:5173
```
