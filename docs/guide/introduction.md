# Introduction

ADMET drives a microfluidics rig, records what happens on it, and analyses the
results. It has three entry points, all from one `admet` command:

| Command | What it does | Touches the rig |
|---|---|---|
| `admet control` | Desktop app (Qt). Connects the flow units and camera, runs protocols, records runs, calculates results. | Yes |
| `admet analyze` | Web server (NiceGUI). Opens projects from the lab network and analyses videos and images. | No |
| `admet describe` | Lists the operations and engines the Python API offers. | No |

## The rig

ADMET is built around a Fluigent pressure controller with three flow units, and
an optional Basler camera:

| Channel | Name | Typical use |
|---|---|---|
| 0 | Oil L | Fluorinated oil (e.g. dSurf), on an L flow unit |
| 1 | Cells M (M1) | Aqueous phase, on an M flow unit |
| 2 | Beads M (M2) | Aqueous phase, on an M flow unit |

Each channel is flow-regulated through its flow unit, or set to a fixed pressure.

## How work is organised

Everything belongs to a **project**: a folder ending in `.admetp` that holds the
protocols that ran, their recordings, the measurements you typed in and the
results calculated from them. Copy the folder and you have the whole experiment.
See [Projects](./projects).

A typical session in `admet control`:

1. Open or create a project.
2. Connect the camera (optional) and the Fluigent controller.
3. On **Calibration**, set each channel's liquid profile and **Apply All Corrections**.
4. **Prime** the lines.
5. Add protocol steps, **Plan** each one, review it, and **Execute**.
6. Type measurements (masses, volumes) into the run's table as you go.
7. On **Calculations**, calculate results from the finished runs.
8. **Wash**, then **Cleanup** to release the devices.

`admet analyze` then reads the same project from another machine to analyse the
recorded videos and images.

## What ADMET does not do

- It never applies a calculated correction to the rig by itself. Calculations
  suggest; you enter the values on the Calibration page.
- It never guesses missing data. A step that was paused, skipped or recorded
  with gaps is left out or reported, not filled in.
- It does not replace the physical emergency stop. Keep it within reach.
