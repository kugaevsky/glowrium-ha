# A failed command is vouched for only by a report of what it set, newer than the write

- **Status:** accepted
- **Date:** 2026-08-24 (newer than the write); 2026-10-05 (about the command);
  2026-10-09 (a command that was never written fails at once)

## Context

Writes use write-with-response, and on a marginal link it is the
acknowledgement that goes missing. Observed once on a G7 at RSSI -88: both
attempts of a `light.turn_on` raised `GATT Protocol Error: Unlikely Error`
while the lamp lit and notified its new state 32 ms before the error
surfaced. The user got an error toast while watching the light change, so
0.2.0 added a wait for a confirming report. Three things then let a report
vouch for a command that had not landed:

- the state mirror is never invalidated - a drop clears the client, not the
  state - so "turn it off" against a stale mirror that already said off
  confirmed at once while the lamp stayed on;
- the lamp reports of its own accord all day, its brightness as the circadian
  curve moves it, and a fresh report of brightness says nothing about a power
  flag the mirror got wrong hours ago;
- a command that never got a connection was checked all the same whenever
  something else had been written while it waited - the clock, on connecting -
  and could be called delivered (found while #21 was being done, 2026-10-09).

## Decision

When a write fails, `Link.send` asks the device half's `vouch`
(`_async_device_confirms`) once, for up to `_CONFIRM_TIMEOUT` (2 s): has the
lamp reported the state the command asked for? Yes only when all three hold:

- the write reached the characteristic (`said` in `Link.send`); a command that
  never got that far has nothing to be vouched for and fails at once;
- every property of the command the lamp can report (keys in `STATE_KEYS`;
  `0x2c` and `0x32` are never reported back) matches the mirror;
- at least one of them was reported since the command was taken up
  (`_reported_at`, a per-id count of reports, against `_reports` noted before
  the wait for the lock). One, not all: the lamp reports what changed, and a
  mode command carries a ramp that is usually what it already was.

A command vouched for is delivered, and the mirror is not echoed for it.

## Consequences

- A command that really failed takes up to 2 s longer to say so.
- The client behind the last failed write is hung up only after the window:
  its notifications are the channel the vouch listens on.
- Do not go back to "any fresh report confirms" or to matching the mirror
  alone; each is a case above. Do not require every key to be reported: no
  mode command would ever confirm.
- `reports_before` is taken before the wait for the lock, so a report that
  came while the command waited is as fresh as one after it.

## Evidence

- ARCHITECTURE.md, "Reconnect": "A failed write is checked against what the
  device reports"; CHANGELOG 0.2.0, 0.3.0 and the unreleased entry for #21.
- `tests/test_coordinator.py::test_lost_acknowledgement_is_not_reported_as_failure`,
  `::test_a_stale_mirror_does_not_vouch_for_a_failed_write` (a report from
  before the command is its case "by a report, some time ago"),
  `::test_a_report_vouches_only_for_what_it_carries`,
  `::test_what_else_was_written_meanwhile_vouches_for_no_command`,
  `::test_a_command_that_never_reached_the_wire_fails_at_once`.

## Revisit when

A model is found that acknowledges reliably on a marginal link, so the window
only costs time; or one that reports nothing of what a command sets, so the
window can never vouch and should be skipped for it.
