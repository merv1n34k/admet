# Review runs

The **Review runs** stage plots the pressure and flow of finished runs. It reads the
saved files only; it needs no device and changes nothing on the rig.

- The **runs table** lists the project's recorded runs, newest first, with when they
  were recorded, how they ended and how long they ran. Tick a run to plot it; its
  line style appears in the *Line* column.
- **Load file…** adds any fluidics CSV with the same columns, for example an older
  DropleGen log. Loaded files stay in the table until ADMET closes.
- **Channels** hides or shows Oil L, Cells M and Beads M. Each channel keeps the
  colour it has on the live plots.
- **Bin** averages readings into windows of that many seconds; *off* plots every
  reading.
- **Align** lines runs up on their first protocol step (▶ 1); a loaded file, which has
  no steps, on its first row:
  - *To first run* lines up runs only: the top ticked run keeps its own clock and every
    other run moves, whole, so its first step lands on the same point;
  - *Stacked* lines up steps too: every step of every run, from its start to its end,
    is drawn from 0, so steps and replicates overlay;
  - *Recorded time* shows each run as recorded, in seconds since ADMET began reading
    the Fluigent.
- **Reset view** fits the plots to the data again.

The four plots sit in a grid, pressure and flow above, droplets and their CV below,
all on one time axis. The droplet plots show the QC each recording wrote while it
ran (see [Live QC](./running#live-qc)): the rate with its 95 % interval shaded and,
dashed, the rate the flows measured at that moment must make at the size measured
then; and the CV in the colour of its state, green stable, amber settling, red
unstable, with the 5 % limit drawn. Each recording is its own curve, so nothing is
drawn across the gaps between them. The runs table adds the droplets made over the
run's recordings, the median rate, and the median CV with the state at the end. A
loaded file has no QC and leaves those plots empty.

Each plot has a legend naming its curves, `run · channel` above, `run · rate` and
`run · expected` for the droplets, the three states and the limit for the CV; drag
it aside when it covers a curve.

Pressure and flow share the time axis with the droplet plots. Step starts (▶) and ends (■) are marked as on
the live plots, for runs recorded in the project. A crosshair follows the cursor with
the time and value under it; a click pins a reference (+), after which the readout
also shows the difference from it, and a double-click clears it.

**Ctrl + wheel** over any plot zooms it, here and on the live plots; the wheel alone
scrolls the page.
