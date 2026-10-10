# A value is judged where it is read, by one reader; what cannot be used is unknown

- **Status:** accepted
- **Date:** 2026-10-10 (#23)

## Context

The state mirror keeps whatever a frame carried under an id (ADR 0013), and
every consumer decided for itself what it would take: an entity by the
truthiness of a flag or by the length of a byte string, a command by writing
back what stood there, the diagnostics by strict readings of their own. No
entity raised on any of it, and this is what was shown: text under the power
id as on, one zero byte under the Circadian flag as Circadian mode, a flag as
the first preset, five bytes of `ff` as a ramp of 1092 minutes, a schedule of
twelve bytes as the one in its first eleven, `inf` as a latitude. A command
wrote such a value back to the lamp, and one that held a null could not be
written at all. The file and the device page disagreed about the same value,
and each layout was written down twice.

No lamp has been seen to report any of this. Another model could, a faulty
lamp could, and so could anything answering at the lamp's address: the
protocol has no pairing.

ADR 0011 had the lighting-mode select tell a setting not read from one read
and unusable, and named the other settings as owed.

## Decision

- Each property has one reader, in the semantic codec, and everything that
  wants its value goes through it: the entities, the commands that build on a
  reported value, the diagnostics, the log. A reader has three answers:
  nothing was reported under the id; the value, as what the id means; or that
  what is there cannot be used.
- The check is made where a value is read. The mirror does not change: it
  keeps what the lamp said. A value turned away at the intake could not be
  told from one never reported, and a known id with a bad value would stop
  counting as reported - which turns an answer to the state request into
  silence, and silence into a read (ADR 0002).
- One rule says what a value is, for every reader: a flag is a boolean; a
  ramp two bytes of seconds and no more than two hours; a schedule eleven
  bytes whose flag is 0 or 1 and whose hours are hours; and so on, in
  ARCHITECTURE, "What a value is". It is the rule the diagnostics applied,
  with the one exception already on record: a brightness need not be a whole
  number.
- What cannot be used is shown as `unknown`. Not as the remembered value,
  which stands in only for what was not reported (ADR 0011), and not as a
  coercion. Nothing becomes unavailable for it.
- No command writes back what cannot be used. Where a default exists it is
  written - the ramp with a lighting mode, the hour with the daylight-saving
  flag. Where the command has nothing else to build on - the lighting mode
  for the ramp, the slot for a field of the schedule - it is refused, in words
  that do not promise that waiting helps.
- The bring-up runs on the boolean `False` and is settled by the boolean
  `True`. A clock of seven bytes that shows no date is corrected; a clock in
  any other shape draws no write.
- The first value that cannot be used under a property is said in the log,
  once a session for that property, without the value. Not a clock of seven
  bytes that shows no date: it is known what that is, and it is mended.
- The answer that is no value cannot be taken for one: it is not a number
  and not bytes, the type checker holds every caller to the three answers,
  and it refuses to be truth-tested.

## Consequences

- A model whose layout differs from the table loses a reading it had: it
  reads `unknown` where it read a guess, and a change that builds on that
  reading is refused. The warning is how such a model will be found, and a
  new row in the table - with the frame or the diagnostics download that
  shows it - is how it is mended.
- A reported null is a report. "Not reported" is the id being absent from
  the mirror, never a value of nothing under it.
- The bring-up of a lamp that reports itself not activated still writes the
  clock, whatever stands under its id: the vendor app's sequence is replayed
  whole. A model that keeps its clock another way and has to be brought up
  has not been met, and that sequence would be the first thing to look at.
- The diagnostics keep how the file words a value and nothing else. A
  brightness that is not whole is written as the whole level it is nearest
  to, so the file still repeats no float of the lamp's choosing.
- The scripted lamp of the tests answers a state request with zero under
  every id it was given no value for. Most of those cannot be used, and such
  a test now draws the warning for each; none of them looks at it.

## Evidence

- CHANGELOG, Unreleased (#23); README, "How state is updated" and
  "Troubleshooting"; ARCHITECTURE, "What a value is".
- `tests/test_protocol.py::test_a_reader_has_one_of_three_answers_whatever_is_under_its_id`,
  `::test_what_cannot_be_used_is_neither_true_nor_false`,
  `::test_a_flag_is_a_boolean_and_nothing_that_only_reads_as_one`,
  `::test_a_lighting_mode_is_a_whole_number_that_fits_one_byte`,
  `::test_a_coordinate_is_a_finite_float_within_its_range`,
  `::test_a_ramp_is_two_bytes_of_seconds_and_no_longer_than_two_hours`,
  `::test_daylight_saving_is_a_flag_byte_and_an_offset_in_five_bytes`,
  `::test_a_schedule_is_eleven_bytes_and_each_is_what_its_place_means`,
  `::test_a_clock_is_seven_bytes_that_are_a_date_and_a_time`,
  `::test_the_operating_mode_is_read_from_two_flags`,
  `::test_what_a_g8_was_seen_to_report_reads_as_values`.
- `tests/test_init.py::test_a_value_that_cannot_be_used_is_shown_as_unknown`,
  `::test_a_setting_shows_what_was_remembered_only_until_the_lamp_speaks`,
  `::test_no_value_under_an_id_makes_an_entity_raise`,
  `::test_no_platform_reads_the_state_mirror_itself`,
  `::test_nothing_goes_unavailable_for_a_mode_that_cannot_be_used`.
- `tests/test_coordinator.py::test_a_lighting_mode_is_not_written_with_a_ramp_that_cannot_be_used`,
  `::test_a_ramp_that_cannot_be_used_is_not_the_one_remembered`,
  `::test_the_ramp_is_refused_while_the_lighting_mode_cannot_be_used`,
  `::test_a_schedule_field_is_refused_while_the_slot_cannot_be_used`,
  `::test_the_dst_switch_writes_the_hour_where_the_slot_cannot_be_used`,
  `::test_only_a_flag_that_says_yes_settles_the_bring_up`,
  `::test_only_a_flag_that_says_no_starts_the_bring_up`,
  `::test_a_clock_kept_in_some_other_shape_is_not_written_over`,
  `::test_a_value_that_cannot_be_used_is_said_once_for_its_property`,
  `::test_each_property_the_integration_reads_is_named_when_it_cannot_be_used`,
  `::test_a_clock_that_shows_no_date_is_put_right_and_not_reported`.
- `tests/test_diagnostics.py::test_the_file_calls_unusable_what_the_entities_are_shown_as_unusable`,
  `::test_a_level_that_is_not_whole_is_read_out_as_a_whole_one`,
  `::test_the_download_says_what_the_lamp_reported`.

## Revisit when

A lamp is seen to report a property in a layout that is not the table's: that
is a new row, not a looser rule. Or the judgement has to be made before a
value is kept - a value so large, or so many of them, that keeping it is the
harm - which is the mirror's bound and ADR 0013.
