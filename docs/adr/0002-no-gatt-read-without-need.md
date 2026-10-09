# No GATT read without need: the state is asked for, the device info is read last

- **Status:** accepted
- **Date:** 2026-10-05 (asking instead of reading); 2026-10-06 (the cause read
  off the controller; reading the device info in every session reaffirmed)

## Context

`facebd02` is readable, and from 0.2.0 the state was read first on every
connect. Measured on a G7 (firmware 4) from a Linux host with BlueZ 5.82,
outside the integration and between its ticks, on 2026-10-05: a link that was
connected, subscribed and written the state request stayed up 9 runs of 9; a
link on which any characteristic was read - `facebd02` (235 bytes),
`facebd80` (93), `facebd81` (1) - was gone 2.02-2.04 s later, 6 of 6. The read
succeeds; two seconds later, BlueZ's own timer, the link is reported dropped.
A read on every connect was a link made and lost on every tick, about a
hundred an hour, taken for a lamp at the edge of range.

The cause was read off the controller on 2026-10-06: the lamp answers a read
twice - the value, then an error response (`0x1e`) to the same request - and
BlueZ closes the ATT channel on a response that answers nothing. macOS drops
the stray response, so the bench on a laptop never showed it. No workaround.

## Decision

- The state is asked for: the ids in `STATE_KEYS` are written to `facebd02`
  and the lamp reports them in a notification, waited for up to
  `_REPORT_TIMEOUT` (3 s) while the keys of every notification since the
  request are collected. A lamp that answers in part has answered. A lamp that
  reports is never read.
- A read is left for a lamp that will not report: one that refuses the request
  (a G8 answers ATT `Insufficient authorization`) or acknowledges it and says
  nothing. A lamp that has refused is read first from then on, and still asked
  afterwards: the read stops at `0x15` and carries no indicator, mode or ramp.
- A refusal is told by what the error says (the ATT code where bleak gives
  one, the text otherwise), never by what worked before it.
- The device-info string (`facebd80`) is the one read left. It is read once
  per session, last - after the state, the bring-up, the clock and
  `turn.answered()`. On BlueZ that link is then spent; the tick makes another
  thirty seconds later on which nothing is read. A command's own connect reads
  nothing, and the probe of a silent link asks, never reads.
- It is read again in every session. Keeping the string from the last session
  and skipping the read was proposed on 2026-10-06 and rejected by the
  maintainer: firmware changes between starts, and what was remembered is not
  a reading. Do not propose it again.

## Consequences

- Each start costs one two-second link on BlueZ (`device info read`, then
  `disconnected`); the README names it as expected.
- `STATE_KEYS` is what the vendor app asks for plus the clock (`0x05`); an id
  is added only after it has been measured on a lamp.
- Whether to ask is never judged from the state mirror: it accumulates, so a
  key seen once would look covered for the rest of the session.

## Evidence

- ARCHITECTURE.md, "Priming state on connect" (the table of 2026-10-05; the
  trace of 2026-10-06; three links in three hours) and "Device-info string".
- `tests/test_coordinator.py::test_a_lamp_that_reports_is_asked_and_never_read`,
  `::test_the_device_info_is_the_last_thing_read_and_read_once`,
  `::test_a_command_connect_reads_nothing`;
  `tests/test_bus_lifetime.py::test_a_read_alone_is_not_taken_for_the_lamp_answering`.

## Revisit when

A refusing model is at hand to measure whether the refused request or the read
before it takes the link (nothing has been re-measured on a G8); a firmware
stops answering a read twice; or BlueZ stops closing the channel on it.
