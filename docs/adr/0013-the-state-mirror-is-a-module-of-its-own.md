# The state mirror is a module of its own: it takes frames, and nothing else writes it

- **Status:** accepted
- **Date:** 2026-10-09 (designed three ways and compared; design C chosen
  with two of B's choices; bounded the same day)

## Context

What the lamp has said lives in a `dict[int, Any]` on the coordinator, fed
by `_ingest` from the notify callback and by the echo of an acknowledged
write, and read by every entity, by `protocol.*` and by the diagnostics.
Around it the coordinator keeps a report counter, a per-id "reported at"
map, a set of ids carried since the last state request, the moment the
clock last came in, two said-once flags and the blanking of frames for the
log - some 290 lines, and the place the last four frame defects landed in
(the split map of the G8, #5; the unreadable item; the blanking that
skipped past a match; the stale mirror that vouched for a failed write).
Three of the four were the mirror's; one was a caller's. The rule "what
writes `state` goes through `_mirror`" was prose in CLAUDE.md, held by
review. The frame tests ran a whole coordinator to feed one frame.

And the dict kept every id of every frame, for the life of the session. A
lamp reports the same few ids again, so that cost nothing with a lamp. But
the protocol has no pairing: whatever answers at the lamp's address fills the
mirror. Measured on 2026-10-09, a device sending ids never sent before kept
9.5 to 31.6 KiB a frame in memory - a host's free memory in hours. Every
other parser looked at (bthome-ble, xiaomi-ble, Home Assistant's passive
processor, ESPHome) keys its state by its own table; here the device chose
the keys.

## Decision

- `mirror.py` holds `Mirror`, a read-only `Mapping[int, Any]` built from the
  lamp's address, the ids the integration knows, a `described` callable (the
  model-and-firmware phrase, read after the first frames) and a `now`
  callable. Two ways in and none out: `take(frame)` returns the ids taken in
  from the frame (empty: no report; never raises; the decoder's outcomes and
  their log lines are behind it, every printed frame blanked) and
  `echo(payload)` merges an
  acknowledged write (no report, nobody woken, the clock stamped as `take`
  stamps it). Written from nowhere else: a write from outside is a
  `TypeError`.
- The mirror is bounded. It always keeps the ids it is told the integration
  knows (`const.KNOWN_KEYS`: what the lamp is asked for, what only commands
  write, the curve) and every echo. Of the ids nobody named it keeps the
  first 64 a session brings and drops none of them to make room. A property
  beyond that is not stored, and counted each time it is reported
  (`Mirror.not_kept`); it is in nothing the mirror says of a report, and a
  frame of which nothing was kept is no report. The first is said once a
  session at WARNING without a byte of the frame, and the diagnostics say how
  many times it happened.
- One primitive for "what has the lamp reported since a mark":
  `reports` and `reported_since(n)`; and one wait, `next_report()`, which
  resolves on the first report after the call and never on an echo.
  Priming asks whether `reported_since(before)` covers `STATE_KEYS`;
  vouching whether it meets the command's ids (ADR 0005).
- The two cross-concerns stay the coordinator's, in its one intake,
  `_ingest`: `note_answer` before `take`, unconditionally - a garbage frame
  is still the lamp speaking (a read alone is not, ADR 0002) - and the ramp
  seed and the listeners after it, for a report only. The mirror is handed
  no callbacks and emits no events: it has no initiative, so whoever calls
  it is on the stack and acts on the return value.
- `coordinator.state` keeps its name and returns the mirror. `_writes_sent`
  and `_desired_ramp` stay the coordinator's.

## Consequences

- The next frame defect is found in `tests/test_mirror.py` by a hex frame
  and fixed in `mirror.py`; a wrong conclusion drawn from the mirror stays
  in four coordinator callers.
- A test that plants state does so through `echo` or `take`, never by
  assignment; the one state no production path can reach (a clock without
  its moment) is no longer plantable; the diagnostics keep their branch for
  it as a guard, held by a test of the reader alone.
- `protocol.*` and `diagnostics._state` read a `Mapping`. The typed readers
  of #23 attach to the mirror's values without the mirror changing.
- Moving the mirror changed nothing a user sees; bounding it does only for a
  device that sends more than a lamp has been seen to. The link is not
  touched.
- #23 wrote "keep nothing under an id nobody reads". The bound keeps 64 of
  them instead, by the maintainer's decision: the bound is of the same kind,
  and the bench goes on listing what a new model reports. What a value under
  a known id may be, and how large, is still #23's.

## Evidence

- ARCHITECTURE.md, "The state mirror"; `tests/test_mirror.py` (the mirror
  alone, frames in); `tests/test_coordinator.py::test_the_intake_notes_the_answer_and_tells_of_a_report_only`,
  `::test_no_line_in_the_log_carries_the_coordinates`,
  `::test_a_report_read_only_in_part_is_still_the_answer_to_the_request`;
  `tests/test_mirror.py::test_no_more_than_the_limit_of_ids_nobody_named_is_kept`,
  `::test_what_was_not_kept_was_not_reported`;
  `tests/test_coordinator.py::test_what_the_lamp_is_asked_for_is_kept_whatever_else_it_sent`;
  the mirror's own mutation gate.

## Revisit when

A second consumer of frames appears that must see them before the
coordinator does, or the mirror has to forget what it holds - then it needs
an initiative of its own, and the no-callbacks rule is the thing to reopen.
Or a model is met that reports more than 64 properties nobody named: the
warning is how it will be found.
