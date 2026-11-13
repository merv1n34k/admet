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

The last project opened comes back by itself; pick another at the top or browse for it.

| Stage | What you do there |
|---|---|
| **1. Files** | Add the videos and image folders to analyse. Tick **Use** on the ones to run and name the samples. The selected file's first frame and facts are shown below the list. |
| **2. Analyse** | Fill in **µm/px** and **FPS** for every ticked file (red cells are missing; FPS is prefilled for recordings made by `admet control`), set each file up on its preview, then **Run all ticked** or **Run selected**. |
| **3. Results** | View only: one row per file with its numbers and their distributions, the plots, and **Export CSV** / **Export plots**. |

Image folders are analysed with Cellpose and videos with OpenCV; both go through the
same three stages.

## The file list

Each row is one file with its sample name, engine and when it was last analysed.
**Use** decides which files run and which results are shown; **✕** takes a file
analyze added off the list and out of the project (recordings made by `admet
control` stay). The list and every file's settings are saved with the project, so
reopening it restores where you were.

## Setting a video up

The preview shows the frame at its native size, scaled down to fit. **Drag on the
frame to crop**; *Full frame* clears the crop. The bar under it sets the frames
analysed (start–end, end exclusive); the slider below it picks the preview frame.
After a run, *Show the stored analysis* draws the detected droplets and their paths
over the frame.

Droplets are found as what is darker than the empty channel. The background is the
bright side of each pixel over frames sampled across the video, each frame is matched
to it in brightness (so a lamp that drifts does not matter), and a pixel counts as
droplet when it is darker than the background by a few times the camera noise. An
opening the size of a satellite droplet keeps satellites, wall lines and specks off
the main droplets. Droplets are then followed from frame to frame and counted once
each as they pass a gate line, which gives the generation rate.

**µm/px** and **FPS** are applied when results are read, not by the run: the engine
measures in pixels and frames, so changing either updates Results and the export at
once. Crop, frame range and the detection settings change what is detected and need
a new run.

Detection settings, per file: **skip left / right** (% of the width left out, where
droplets form and where they leave); **gate** (% of the width, where every droplet
passes once); **satellite size**, **min droplet area** and **threshold × noise**.
Tick *Show detection stages* to see each step on the preview frame. *Reset file
settings* puts the selected file back to the defaults, keeping its µm/px and FPS.

## Setting an image folder up

Image folders are grouped into frames by name (`…_z01_…a01f001d4.tif`: z-slice and
frame), min-projected over z and segmented with Cellpose. The editor shows each
image with the stored droplets drawn over it: a circle per droplet with its
inclusion count. Corrections are made on the image: **click** a droplet to switch
it off (or back on), **shift-click** to add an inclusion, **alt-click** to remove
one. They are saved with the project and count on **3. Results** and in the export;
the stored rows stay as found, and *Clear corrections* drops them.

## Results

The table has one row per ticked file: droplets, diameter, area, CV, generation
rate and speed, each with a small histogram of its distribution (one value per
droplet), and when the file was analysed with which settings. The plots show the
diameter distribution, the droplet timeline, positions and area along the channel,
with the files overlaid. **Export CSV** downloads a zip with a summary and every
droplet row per file; **Export plots** downloads the plots as PNG.

## Where results go

- `analysis/` inside the project: what was analysed, with which settings, and the
  raw results.
- `~/.admet-cache/` on the analysis machine: intermediate files such as contours.
