# admet analyze

![admet analyze](/admet_analyze.png)

`admet analyze` serves the analysis over the network. Run it on a machine that
can see the recorded runs, the server or your own; anyone on the lab network opens
it in a browser. It reads recorded data only and never touches the rig.

```bash
uv run --extra analyze admet analyze                      # every interface, port 8080
uv run --extra analyze admet analyze --port 9000
uv run --extra analyze admet analyze --host 127.0.0.1 --data /srv/admet/runs
```

| Option | Default | Meaning |
|---|---|---|
| `--host` | `0.0.0.0` | Address to listen on; the default is every interface |
| `--port` | `8080` | Port to listen on |
| `--data` | none | A folder of runs to watch: a data folder, or the export folder on the server |
| `--home` | `ADMET_HOME` or `~/.admet` | The admet home whose `analyze/` room holds the registry and the results |

Analyze keeps its own room in the home: `analyze/registry.json` lists the folders
it watches and every file it knows with its sample name, engine and settings;
`analyze/results/` holds one folder per file and detection settings, named by a
digest of the file's content and those settings, so the same video analysed the
same way is never analysed twice and a copy of the file finds its result.

Press **Ctrl+C** to stop. Running analyses are asked to stop cleanly first, so no
one's work is left half-written.

## The workflow

The file list comes back as you left it.

| Stage | What you do there |
|---|---|
| **1. Files** | **Add folder…** takes a folder of runs (a data folder, the export folder on the server, or one run) and lists every video in it with its run's name; **Add files…** takes single videos and image folders. Tick **Use** on the ones to run and name the samples. The selected file's first frame and facts are shown below the list. Only ticked files go on to 2. Analyse and 3. Results; an unticked file stays listed here. On those stages Use is a tick of its own: an unticked file stays in their table, greyed, and is left out of the run, the plots and the export. Use all in a table's header ticks or clears every file of that stage. |
| **2. Analyse** | Fill in **µm/px** and **FPS** for every ticked file (red cells are missing; FPS is prefilled for recordings made by `admet control`), set each file up on its preview, then **Run all ticked** or **Run selected**. |
| **3. Results** | View only: one row per file with its numbers and their distributions, the plots, and **Export CSV** / **Export plots**. |

Image folders are analysed with Cellpose and videos with OpenCV; both go through the
same three stages.

## The file list

Each row is one file with its sample name, engine and when it was last analysed.
**Use** decides which files run and which results are shown; **✕** takes a file off
the list (its stored results stay). **Rescan** at the top lists videos that arrived
in the watched folders since. The list and every file's settings are kept in the
registry, so the screen comes back as you left it.

## Setting a video up

The preview shows the frame at its native size, scaled down to fit. **Drag on the
frame to crop**; *Full frame* clears the crop. The bar under it sets the frames
analysed (start–end, end exclusive); the slider below it picks the preview frame.
After a run, *Show the stored analysis* draws the detected droplets and their paths
over the frame.

Droplets are found as what is darker than the empty channel. The background is the
bright side of each pixel over frames sampled across the video, each frame is matched
to it in brightness (so a lamp that drifts does not matter), and a pixel counts as
droplet when it is darker than the background by a few times the camera noise, never
less than two levels. A closed rim is filled; a rim open on its bright side, as a
droplet lit from one side shows, is completed by its convex hull when that hull has
a droplet's size and shape. An opening the size of a satellite droplet keeps
satellites, wall lines and specks off the main droplets. Droplets are then followed
from frame to frame for their speed, and the generation rate is the train's speed
over the spacing between neighbouring droplets, which needs no droplet to be
followed; when too few neighbours are seen, droplets are counted as they pass a gate
line instead. When the recording's record carries the set flows, the rate cell also
shows the rate those flows must make at the measured droplet size, every flow but
the oil over the volume of a sphere of the median diameter.

**µm/px** and **FPS** are applied when results are read, not by the run: the engine
measures in pixels and frames, so changing either updates Results and the export at
once. Both are filled from the recording's own record when control measured them,
the FPS as the frames arrived and the µm/px from the walls clicked on the preview;
a typed value wins. Crop, frame range and the detection settings change what is detected and need
a new run.

Detection settings, per file: **skip left / right** (% of the width left out, where
droplets form and where they leave); **gate** (% of the width, where every droplet
passes once); **satellite size**, **min droplet area** and **threshold × noise**.
Tick *Show detection stages* to see each step on the preview frame. *Reset file
settings* puts the selected file back to the defaults, keeping its µm/px and FPS.

To set one thing on many files, type it in the column's header: every ticked file
whose engine has that setting takes it as soon as the value is entered. The
**Columns** dropdown above the table chooses which settings are shown as columns
(µm/px, FPS, the frame range and the detection settings; the frames and crop
summaries too), and the box in the **Use** header ticks or clears every file at
once. Every setting column also edits per file, in its row. A value in a header
lands, for example *Detect inclusions* off on every image folder, or
one µm/px on every video. Unticked files are left alone.

## Setting an image folder up

Image folders are grouped into frames by name (`…_z01_…a01f001d4.tif`: z-slice and
frame), min-projected over z and segmented with Cellpose. The editor shows each
image with the stored droplets drawn over it: a circle per droplet with its
inclusion count. Corrections are made on the image: **click** a droplet to switch
it off (or back on), **shift-click** to add an inclusion, **alt-click** to remove
one. They are saved with the project and count on **3. Results** and in the export;
the stored rows stay as found, and *Clear corrections* drops them.

## Results

The stage is one page with one table, in the Action Panel: one line per file
with droplets, diameter, CV and rate, the rig's live number under the
analysis's, the recording's state in the rig's colour, and Use. Below it come
the plots, in sections that are there when they have data; the table's ticks
choose for all of them:

- **Aggregated**: diameter, CV and rate per sample as bars, a hatched bar for
  the live value beside the analysed one.
- **OpenCV**: the analysed videos' diameter distribution, droplets, diameter and
  rate over time, positions and area along the channel.
- **Cellpose**: the image folders' diameter distribution and inclusions per
  droplet.
- **Live QC + Fluidics**: the four plots of the control app's Review stage,
  stacked as its *Stacked* view: every ticked recording's step from its own
  start, so steps and runs overlay. Pressure and flow per channel, the channel
  told by the line pattern, then the droplet rate and CV the rig measured, each
  recording in its sample's colour.

Every series has a colour and pattern of its own. Files whose sample names end in
a replicate marker (`oil rep 2`, `oil_rep02`, `oil r2`, `oil #2`, `oil step 2`) are
replicates of one group and wear shades of one hue; a run's step videos are named
by their steps (`dropseq_3n 19:30 set01_rep01`), so a protocol's replicates come
out as such. Rename the samples on 1. Files to group them as you see fit.
**Export CSV** downloads a zip with a summary (the live numbers in its last
columns) and every droplet row per file; **Export plots** downloads every
plot on the page as PNG.

## Where results go

- `analyze/results/<key>/` in the home: one folder per file and detection settings,
  with the detections (`rows.jsonl`) and the summary (`result.json`).
- `analyze/registry.json`: the folders watched and the file list as left.
