# Manual control

Each channel has its own controls for quick, hand-set commands — priming a line,
a short test, flushing a bubble.

## Commands

- Type a **flow** (µL/min) or a **pressure** (mbar) and press **Enter** to apply
  it. Decimals use a dot.
- **Stop** ends the command the way it was given: a flow command is regulated
  to zero flow, a pressure command is set to zero pressure.

Flow commands need corrections applied first. Detected ranges and any pressure
trip still apply.

## Ending a command automatically

A command can end itself. Choose what ends it, then the value:

| End after | Ends when |
|---|---|
| none | You press Stop |
| volume | This many µL have been delivered since the command started |
| time | This many seconds have passed |
| flow above / below | The flow crosses the value |
| pressure above / below | The pressure crosses the value |

A new command on the channel cancels the previous rule.

### Volume stops

Flow keeps arriving for a moment after a stop. Volume stops can account for it:

- **integral** — stop when the counted volume reaches the target; the run
  overshoots by that tail.
- **adaptive** (default) — stop early by the tail the channel is expected to
  deliver, learned from its earlier flow stops. With nothing learned yet it
  behaves like integral.

The learned tail, and how far recent stops landed from their target, show in a
small caption under each channel's graph. Applying corrections resets what was
learned, since the readings change.

## Stats line

Under each channel: its current flow and pressure, and how long the current
command has been running.

## Manual control during a protocol

Channels the protocol does not use stay under your control. Channels it uses are
locked while it runs; pause the protocol to adjust them. On resume, the protocol's
targets take over again.
