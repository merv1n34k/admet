# Safety

::: danger The physical emergency stop is authoritative
A software stop cannot recover from a blocked vendor SDK, a lost USB link, an
operating-system crash or a power cut. Keep the physical emergency stop within
reach whenever the rig is pressurised.
:::

## Emergency stop

**EMERGENCY STOP** runs outside the normal command queue. It zeroes every channel —
protocol and manual alike — and latches safety. After finding the cause, use
**Reset safety** on the Cleanup stage; resetting does not resume the run.

## Pressure trips

A protocol can set software pressure trips per channel (`pressure_limits_mbar`).
They are **off** unless set: with no trip, ADMET provides no software
overpressure shutdown. A trip ends the run and closes its recording rather than
pausing it — inspect the cause and the partial recording before planning again,
since repeating a partial dispense can deliver extra volume.

Targets must always stay within the ranges the controller and flow units report,
and strictly below any trip that is set.

## Timeouts and gates

- A step that does not finish within its `timeout_s` fails the run and zeroes
  its channels.
- Gates (`confirm_message`) wait for you before a step applies its targets.

## Closing the app

Closing the window zeroes the channels, stops any run and recording, and
disconnects before exiting. Errors during shutdown stay visible.

## Other programs

Do not run vendor software or another Python script against the same devices
while `admet control` is connected; the desktop's lock only prevents a second
ADMET window.
