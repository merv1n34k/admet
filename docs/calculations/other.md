# Flow scout and recording summary

## Flow stability scout

Calculated from a [flow stability scout run](../protocols/templates#flow-stability-scout).
It tries several averaging windows over the flow sweep and reports, for each,
which flows gave usable points and how precisely the static pressure is known
(its bootstrap SD, forward and reverse).

When a window passes every check, the result **suggests flows, a settling time
and an averaging time** for a density run. Nothing starts automatically; set
them in the density template yourself.

## Recording summary

Available for every run. For each recorded column — pressure and flow of each
channel — it gives the sample count, missing samples, mean, SD, minimum and
maximum over the whole recording.

The whole recording includes settling and time spent at gates, so these are a
quick look at a run, not measurements.
