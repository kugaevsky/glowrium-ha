# A frame printed in the log is blanked of what places the lamp; the diagnostics repeat nothing after the lamp

- **Status:** accepted
- **Date:** 2026-10-05

## Context

Three log lines ask for a frame to be posted to an issue: the warning for a
frame with trailing bytes, the warning for a frame with an item the decoder
cannot read, and the debug line for a frame that could not be decoded at all.
A frame can hold the lamp's coordinates (`0x0a`, `0x0b`, float64 degrees) and
the sunrise and sunset times it works out from them (`0x34`), which give the
place away as well. These are exactly the frames that could not be decoded to
their end, so what to blank cannot be found by decoding. The first version
skipped past each match; a false match swallowed the real key and a coordinate
was printed whole (caught in review, 2026-10-05).

The diagnostics download exists to be attached to a public issue, and nearly
everything that could go into it is chosen by the lamp: which ids it reports
and what is under them, which fields its device-info string has and where
each ends. A deny-list over the mirror would be a list of what one lamp sent.

## Decision

- Every frame printed in the log goes through `mirror._for_the_log`: a coordinate
  key followed by a float, and the curve key followed by a byte string, are
  found by their bytes at every offset independently and put down as `xx`.
  The search does not skip past a match. A new line that prints a frame goes
  through the same function.
- The diagnostics file is rebuilt from what the integration can read, never
  copied from the mirror: a known property is read the way the integration
  reads it and written out from that reading (a schedule as its times, a ramp
  as seconds, the clock as its offset from the host's at the moment it came
  into the mirror, with the age of that reading); one that does not read as
  what its name means is `not as expected`; the coordinates are marked
  redacted; whatever has no name is only counted - not its id, not its size;
  the device-info fields are counted, and the model id and firmware are shown
  only in their strict shapes (`identity.as_model_id`, `identity.as_firmware`).
  No raw bytes, no hex, no serial number, no address.
- The same two shapes gate the model and firmware in the three warnings that
  ask to be reported (`unknown` until read, `not as expected` otherwise): the
  device-info string carries the serial number beside them.
- The repair for a stuck stack is filed by `entry_id`, not by the lamp's
  address: Home Assistant adds the open repair ids to every diagnostics file.
  The lamp's advertised name goes into Markdown only through `identity.as_text`.

## Consequences

- No deny-list, no raw bytes, no skipping past a match when blanking. A
  logged frame still gets one look: the search is by bytes.
- The `link` block is the link's own eight fields in a fixed order; a test
  holds the exact list, so a new field fails it until it has been looked at.
- Home Assistant wraps the file in a header of its own (version, time zone,
  the names of custom integrations), which the README says to look over.

## Evidence

- ARCHITECTURE.md, "Frames with an item that cannot be read" (last
  paragraphs) and "Diagnostics".
- `tests/test_coordinator.py::test_no_line_in_the_log_carries_the_coordinates`,
  `::test_wherever_it_stands_in_whatever_noise_a_coordinate_is_blanked`;
  `tests/test_diagnostics.py::test_the_download_does_not_say_where_the_lamp_is_or_which_one_it_is`,
  `::test_what_the_integration_cannot_name_is_only_counted`,
  `::test_each_field_of_the_link_section_follows_its_own_source`.

## Revisit when

The decoder learns every item the family sends, so that a printed frame could
be blanked by decoding; or Home Assistant's diagnostics gain a redaction that
works on values rather than on key names.
