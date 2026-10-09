# A frame read in part is a report; the decoder raises only `ValueError`

- **Status:** accepted
- **Date:** 2026-08-24 (short maps kept, trailing bytes rejected: #5, in 0.2.0);
  2026-10-05 (only `ValueError` leaves the decoder; nesting bound of four; an
  item that cannot be read keeps the pairs ahead of it)

## Context

`cbor.py` parses what comes off a radio, and its caller - the notification
callback, inside the Bluetooth stack's own message handler - catches exactly
`ValueError`. Three findings shaped it:

- A G8 sends a 55-byte frame headed `0xac`, a promise of 12 pairs, carrying 11.
  The decoder discarded the whole frame and every state entity on the G8 read
  `unknown` until an owner (@pentafive) diagnosed it (#5, merged 2026-08-24).
  The G7 has never been seen to do this.
- Fuzzing (2026-10-05): a map keyed by an array or another map left the
  decoder as a `TypeError`; a few hundred maps each the key of the next - one
  byte per level, about 500 bytes, one attribute value - as a `RecursionError`.
  It had been argued that the recursion was unreachable within a frame, judged
  by value-nesting (997 bytes) and arrays (996); the key form is what fit. With
  a key held to a property id, 499 nested arrays (500 bytes) still overflow.
- A pair that is there and cannot be read (a tag, a half-precision float) was
  taken for the end of a split map: the frame was merged as far as it went and
  nothing said that the rest had not been understood.

## Decision

- `decode_frame` (device frames) returns `(value, short)`: the outermost map
  may end with the buffer and yields the pairs that arrived. `decode` (our own
  payloads) stays strict. A map nested inside a value is all or nothing.
- A map key is a property id: an unsigned integer and nothing else.
- Nesting stops at `_MAX_DEPTH` (4); deeper is a malformed frame.
- An item with no reading in the outermost map raises `UnreadableItemError`,
  a `ValueError` carrying the pairs ahead of it (`ahead`). The coordinator
  keeps them (`_ingest`) and counts the frame as a report - the state request
  it answers is answered - warning once per session with the frame, then at
  debug. A frame of which nothing was read is still no report.
- Trailing bytes raise `TrailingBytesError` (a `ValueError`) on both paths and
  the frame is dropped: the remainder could decode to a short, plausible map,
  `{0x14: false}` among them - the value that triggers the bring-up.
- Whatever the bytes, nothing but `ValueError` leaves the decoder.

## Consequences

- Do not "simplify" an unreadable item to dropping the frame whole. A state
  request answered only by such a frame would count as unanswered, the connect
  would fall back on a read, and on BlueZ a read ends the link two seconds
  later (ADR 0002): a model with one unknown item in its report would lose its
  link on every connect, which is what 0.2.0 and 0.2.1 did to every lamp.
- The bound is load-bearing. A claim that an overflow is unreachable is to be
  measured over every form of nesting - keys, values, arrays - not the forms a
  reviewer happened to name; it was wrongly "corrected" once that way.
- Every frame these warnings print goes through `_for_the_log` (ADR 0012).

## Evidence

- ARCHITECTURE.md, "CBOR wire format" and its three subsections.
- `tests/test_cbor.py::test_nothing_but_a_valueerror_leaves_the_frame_decoder`
  (a seeded corpus of noise, mutated real frames and deep nesting),
  `::test_an_item_that_cannot_be_read_says_what_was_read_ahead_of_it`,
  `::test_trailing_bytes_raise_their_own_type`;
  `tests/test_coordinator.py::test_what_was_read_ahead_of_an_unreadable_item_is_kept_and_said_loudly`,
  `::test_a_report_read_only_in_part_is_still_the_answer_to_the_request`.

## Revisit when

A model is found whose reports legitimately nest deeper than four, or carry an
item the subset should learn to read: the warning is how it was found.
