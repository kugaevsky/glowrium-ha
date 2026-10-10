# A remembered value stands in for a setting not yet read, never for one read and unusable

- **Status:** accepted
- **Date:** 2026-08-25 (settings shown again after a restart, 0.2.1);
  2026-10-07 (a lighting mode the model does not have is `unknown`, #30);
  2026-10-10 (every setting tells the two apart, #23)

## Context

Since the light stopped claiming a confident `off` for a state it had never
read (0.2.0), every control sat at `unknown` after a restart until the lamp
answered - and if it could not answer, the page stayed that way. 0.2.1 made
the settings (`GlowriumSettingEntity`, a `RestoreEntity`) show their last
known value again: a setting changes when someone changes it, not while Home
Assistant is down. The light is deliberately not restored: a lamp said to be
on while it is off is worse than `unknown`, and automations reason from it.

The fallback then overreached. The lighting-mode select fell back on the
remembered mode whenever the reported preset was none of the model's own - a
case meant for a lamp not yet read - so after a restart it showed whatever it
had shown before, and Home Assistant recorded a change to it for an automation
to act on (#30).

## Decision

- A remembered value is shown while the mirror has nothing under the setting's
  id: no report and no echo of a write. From the first it steps back for the
  session. A report from the lamp always wins.
- A value that is in the mirror and unusable is `unknown`, not the remembered
  one: a lighting-mode index outside the model's profile shows `unknown`.
- A remembered value is only shown, never written from. A change that builds
  on a setting the lamp has not reported - the ramp, which rewrites the
  lighting mode with it; a schedule field, which rewrites the slot - is
  refused with a message saying so, not written over a guess. Choosing a
  lighting mode is not refused: the index is the one the user picked.
- The ramp re-applied after a switch to Circadian is remembered once the lamp
  has it - seeded from a report, or kept after a write that went through -
  and never from a refused or failed write.
- The light and the diagnostic entities read `unknown` until the lamp speaks.

## Consequences

- Every setting tells "not read" from "read and unusable" since #23: the
  readers answer the two apart, and a setting shows the remembered value for
  the first only (ADR 0015). Until then only the lighting-mode select did.
- A value remembered in a shape the entity no longer uses - a preset by its
  pre-0.3.0 English name - is converted where it can be and let go otherwise.
- On a first start nothing is remembered and the settings read `unknown`.

## Evidence

- CHANGELOG 0.2.1, 0.3.0 ("A ramp that was refused ... is not applied later"),
  0.3.1 (#30); README, "How state is updated".
- `tests/test_init.py::test_a_report_from_the_lamp_overrides_what_was_restored`,
  `::test_a_remembered_mode_the_lamp_does_not_have_is_not_shown`,
  `::test_an_index_the_model_does_not_have_is_not_the_remembered_mode`,
  `::test_a_report_of_nothing_is_a_report_and_the_remembered_mode_steps_back`,
  `::test_a_value_remembered_in_a_shape_it_no_longer_has_is_let_go`,
  `::test_a_value_that_cannot_be_used_is_shown_as_unknown`,
  `::test_a_setting_shows_what_was_remembered_only_until_the_lamp_speaks`;
  `tests/test_coordinator.py::test_a_ramp_that_was_refused_is_not_remembered`,
  `::test_ramp_refuses_when_lighting_mode_unread`.

## Revisit when

A model is found whose settings change while Home Assistant is away often
enough that a remembered value misleads more than `unknown` would.
