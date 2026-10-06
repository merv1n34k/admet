# admet analyze

![admet analyze](/admet_analyze.png)

`admet analyze` serves project analysis over the network. Run it on a machine
that can see the projects; anyone on the lab network opens it in a browser. It
reads project data only and never touches the rig.

```bash
uv run --extra analyze admet analyze                      # every interface, port 8080
uv run --extra analyze admet analyze --port 9000
uv run --extra analyze admet analyze --host 127.0.0.1 --projects /data/admet
```

| Option | Default | Meaning |
|---|---|---|
| `--host` | `0.0.0.0` | Address to listen on; the default is every interface |
| `--port` | `8080` | Port to listen on |
| `--projects` | see [where projects live](../guide/installation#where-projects-live) | Folder holding the `.admetp` projects |

Press **Ctrl+C** to stop. Running analyses are asked to stop cleanly first, so no
one's work is left half-written.

## The workflow

| Stage | What you do there |
|---|---|
| **Import** | Pick a project and add sources — the run's videos and imaging folders. They form the **batch matrix**. |
| **Video (OpenCV)** | Detect and track droplets in videos. |
| **Imaging (Cellpose)** | Segment droplets and inclusions in images. |
| **View** | Inspect stored analysis runs, droplet counts and plots. |
| **Export** | Export figures and tables from the stored raw data. |

## The batch matrix

Each row is one source with its engine, settings and status. The **Use** column
selects which rows run; the **Analyzed** column shows when a row was last
analysed. Settings in the matrix are the real settings — the preview sliders
follow them and edit them.

For videos recorded by `admet control`, the matrix is prefilled from the
recording: frame rate, frame count and crop. Values you change are kept.

The matrix, settings and progress are saved with the project, so reopening it
restores where you were.

## Video analysis (OpenCV)

The preview shows the video at its native size, scaled down to fit. Set the crop
and frame limits on it. After a run, the preview draws the detected droplet
contours and their trajectories over the video, clipped to the crop.

Key settings: microns per pixel, frame rate (taken from the recording when
known), crop and frame limit.

## Imaging (Cellpose)

Runs Cellpose segmentation on imaging folders, with optional inclusion detection.
Corrections made after a run apply to the raw droplet rows shown in **View**.

## Where results go

- `analysis/` inside the project: what was analysed, with which settings, and the
  raw results.
- `~/.admet-cache/` on the analysis machine: intermediate files such as contours.
