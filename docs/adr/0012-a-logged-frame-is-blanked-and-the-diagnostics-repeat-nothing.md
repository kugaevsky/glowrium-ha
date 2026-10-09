# A frame printed in the log is blanked of what places the lamp; the diagnostics repeat nothing after the lamp

- **Status:** accepted
- **Date:** 2026-10-05; 2026-10-09 (no frame above debug; the place found under
  more of its spellings)

## Context

A frame the integration could not read in full is printed, so that it can be
posted to an issue: one with trailing bytes, one with an item the decoder
cannot read, one that could not be decoded at all, one that decodes to
nothing of use. Until 2026-10-09 the first two were printed in a warning -
visible without debug logging, in a log that is posted for reasons that have
nothing to do with this integration.
A frame can hold the lamp's coordinates (`0x0a`, `0x0b`, float64 degrees) and
the sunrise and sunset times it works out from them (`0x34`), which give the
place away as well. These are exactly the frames that could not be decoded to
their end, so what to blank cannot be found by decoding. The first version
skipped past each match; a false match swallowed the real key and a coordinate
was printed whole (caught in review, 2026-10-05).

The search knows the shapes seen so far, and a frame that gets printed is one
the decoder could not finish. Measured on 2026-10-09: the decoder takes the
times' id under `19 00 34`, and the search looked for `18 34`; a coordinate
behind a tag, the times behind a tag and the times as a string of indefinite
length were printed whole, each in a warning it caused itself; and a frame
that begins inside the times - the second half of a split map - is taken for
trailing bytes and was printed in that warning with every time readable.

The diagnostics download exists to be attached to a public issue, and nearly
everything that could go into it is chosen by the lamp: which ids it reports
and what is under them, which fields its device-info string has and where
each ends. A deny-list over the mirror would be a list of what one lamp sent.

## Decision

- No log record above debug holds bytes of a frame. The warnings for a frame
  with trailing bytes and for one with an item that cannot be read say what
  was wrong and that the frame is in the debug log; the one for properties
  there was no room for carries nothing of the frame either. A frame is
  printed at debug, the first one too, and rendered only when that line is
  written. What the lamp chose is not repeated where nobody asked for it: the
  log follows the diagnostics.
- Every frame that is printed goes through `mirror._for_the_log`: the
  coordinates, each a float, and the curve, a byte string, are found by their
  bytes at every offset independently and put down as `xx` - an id by its
  last byte, so under every width it can be written in; behind any tags; the
  curve under every head a byte string can have, an indefinite length among
  them. The search does not skip past a match. A new line that prints a frame
  goes through the same function, and is a debug line.
- The search is linear in the frame. No more than four tags, and no more than
  four pieces of a string of indefinite length, are stepped over from one
  offset; past that what is left of the frame is blanked. Every offset is
  looked at, so a walk that could run to the end from each of them made a
  frame built for it quadratic to render: 4 ms for 512 bytes where an
  ordinary one takes a quarter of one (measured in review, 2026-10-09).
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
- The same two shapes gate the model and firmware in the four warnings that
  ask to be reported (`unknown` until read, `not as expected` otherwise): the
  device-info string carries the serial number beside them.
- The repair for a stuck stack is filed by `entry_id`, not by the lamp's
  address: Home Assistant adds the open repair ids to every diagnostics file.
  The lamp's advertised name goes into Markdown only through `identity.as_text`.

## Consequences

- No deny-list, no raw bytes, no skipping past a match when blanking. A
  logged frame still gets one look: the search is by bytes.
- What no search finds stays readable in the debug log: a value in a form
  nobody has met, a value under an id nobody named, and a value without its
  id - a frame that begins inside one. The lines that say where the frame is
  say to look it over.
- Whoever sends a frame enables debug logging first; the README says how.
- The `link` block is the link's own eight fields in a fixed order; a test
  holds the exact list, so a new field fails it until it has been looked at.
- Home Assistant wraps the file in a header of its own (version, time zone,
  the names of custom integrations), which the README says to look over.

## Evidence

- ARCHITECTURE.md, "Frames with an item that cannot be read" (last
  paragraphs) and "Diagnostics".
- `tests/test_coordinator.py::test_no_line_in_the_log_carries_the_coordinates`;
  `tests/test_mirror.py::test_no_bytes_of_a_frame_are_logged_above_debug`,
  `::test_a_frame_goes_into_the_log_without_what_says_where_the_lamp_is`,
  `::test_wherever_it_stands_in_whatever_noise_a_coordinate_is_blanked`;
  `tests/test_diagnostics.py::test_the_download_does_not_say_where_the_lamp_is_or_which_one_it_is`,
  `::test_what_the_integration_cannot_name_is_only_counted`,
  `::test_each_field_of_the_link_section_follows_its_own_source`.

## Revisit when

The decoder learns every item the family sends, so that a printed frame could
be blanked by decoding; or Home Assistant's diagnostics gain a redaction that
works on values rather than on key names.
