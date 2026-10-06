# Installation

ADMET needs **Python 3.12** and [uv](https://docs.astral.sh/uv/). The core
package has no user interface; each app is an optional extra, so a control PC
installs only Qt and an analysis server only its web and ML stack.

| Extra | For | Pulls in |
|---|---|---|
| `control` | `admet control` | PySide6, pyqtgraph, OpenCV, pypylon |
| `analyze` | `admet analyze` | NiceGUI, Cellpose, PyTorch, OpenCV, SciPy |

## macOS and Linux

```bash
git clone https://github.com/merv1n34k/admet.git
cd admet
uv python install 3.12
uv sync --locked --extra control      # or --extra analyze, or --all-extras
uv run --locked --extra control admet control
```

## Windows (control PC)

Install Git and uv from PowerShell, then reopen PowerShell so both are on `PATH`:

```powershell
winget install --id Git.Git -e --source winget
winget install --id astral-sh.uv -e --source winget
```

```powershell
git clone https://github.com/merv1n34k/admet.git
cd admet
uv python install 3.12
uv sync --locked --extra control
uv run --locked --extra control admet control
```

To update an existing clone, finish any run and close ADMET first:

```powershell
git pull --ff-only
uv sync --locked --extra control
```

Never copy a `.venv` from another machine; sync a fresh one on each PC.

## Device drivers

For live hardware, install the vendor drivers for your instruments:

- **Fluigent** — the controller and flow-unit drivers, and
  [Fluigent's SDK](https://github.com/Fluigent/fgt-SDK) in the same Python
  environment. To use an SDK copy elsewhere, set `ADMET_FLUIGENT_SDK_PATH` to its
  Python folder (the one containing `Fluigent/SDK`).
- **Basler** — the pylon runtime for the camera. Only needed for
  *Camera + fluidics* acquisition.

::: warning macOS on Apple Silicon
The Fluigent SDK has no native support for macOS on Apple Silicon. Use a Windows
or Linux PC to drive a Fluigent rig.
:::

Without hardware, `admet control` can still connect to a **simulated** Fluigent
controller (Fluigent page → *Simulated Hardware*). The camera is not simulated.

## Where projects live

Both apps look for projects in this order:

1. the folder given with `--projects` (analyze) or `--project` (control),
2. the `ADMET_PROJECTS_ROOT` environment variable,
3. a `projects/` folder in the current directory,
4. the current directory.
