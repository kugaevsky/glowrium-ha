# A model without a profile keeps the reference presets, and its owner is told

- **Status:** accepted
- **Date:** 2026-10-10 (#31, split out of #25)

## Context

`resolve_model` gives a lamp whose model has no entry in `MODELS` the family
name and the G7's presets, so that the lamp can be controlled at all. The
lighting-mode select then offers the G7's eight keys under the G7's names and
writes the G7's index for the one chosen. Whether that index selects the same
preset on another model, another preset or none is known for no model but the
G7. The write is echoed into the state mirror, so the select shows the choice
whether or not the lamp took it as that preset. Two G8s are run this way.

Nothing in Home Assistant said that these presets are a guess: the README
did, and the device page showed the family name where a model name would be.
#25 left three ways open - offer the reference presets as before, mark them
as unconfirmed for a model without a profile, or not offer them at all.

`resolve_model` cannot tell such a lamp from one that has not been read yet:
it answers both with the same generic profile. A lamp set up for the first
time has no model id until its device-info string has been read, which is the
last thing the first exchange does (ADR 0002).

## Decision

- The presets stay on offer as they are. For a model without a profile the
  options, their names and the command written for a choice are what they are
  for a G7. Taking them away would take a control from a lamp on which the
  indices may well be right, and nothing would then be learnt of them.
- While a lamp's model id is known and has no profile, one repair is open for
  its config entry: a warning with nothing to fix from the dialog. It names
  the lamp and its model id, says that the presets on offer are the G7's and
  that the select shows what was chosen either way, and links to what would
  confirm them.
- Not known yet is not the same as without a profile. Nothing is said of a
  lamp that has not named its model: it may be a G7. `has_profile` is asked of
  a model id, and neither the generic profile nor its name is read for the
  answer.
- The notice is raised from the model id remembered with the entry, at start,
  and in the session in which the model id is first read.
- It is left standing where the watching stops, and raised over itself at the
  next start. Home Assistant keeps "ignored" with a repair and forgets it when
  the repair is deleted; a notice taken down at every unload would be shown
  anew at every reload. It is deleted when the model id has a profile and when
  the entry is removed, and it is not persistent.
- Its id is built from the config entry's id, never from the lamp's address
  (ADR 0012). The lamp's name goes into the text as text. The model id goes in
  only when it has the shape of one; for one that has not, a second text does
  without it.
- Once the watching has stopped none is raised, as no stack fault is
  announced then (ADR 0004): nobody would be left to answer for it.

## Consequences

- An entry that is unloaded and not loaded again - a disabled one - goes on
  showing its notice until Home Assistant restarts.
- The notice has two texts in each of the six languages, which differ in their
  first sentence. The value of a placeholder is not translated, so no word
  could stand where the model id would be.
- Whoever raises the notice carries its own mark of having stopped. The link
  has one, and it is not the coordinator's to read (ADR 0008). The coordinator
  carried it until 2026-10-11; the keeper of the config entry does since, with
  the whole of the notice, and after it has ceased to watch it still writes
  the registry and the remembered model id (ADR 0016).
- The owner of such a lamp gets words and no more: the presets are as
  unconfirmed as they were, until a capture arrives.

## Evidence

- CHANGELOG, Unreleased (#31); README, "About the G8"; ARCHITECTURE, "A model
  without a profile"; CONTRIBUTING, "Sending protocol data for a device".
- `tests/test_models.py::test_a_model_id_has_a_profile_of_its_own_or_has_none`,
  `::test_a_profile_is_a_profile_whatever_it_is_called`.
- `tests/test_keeper.py::test_a_model_without_a_profile_is_put_in_front_of_its_owner_at_start`,
  `::test_a_lamp_with_a_profile_or_one_not_yet_known_is_told_nothing`,
  `::test_the_notice_comes_in_the_session_the_model_is_first_read_in`,
  `::test_the_notice_goes_once_the_model_has_a_profile`,
  `::test_the_notice_goes_when_the_lamp_names_a_model_that_has_a_profile`,
  `::test_a_notice_that_was_ignored_stays_ignored_through_a_reload`,
  `::test_a_restart_brings_the_notice_back_for_a_loaded_entry_and_as_it_was_left`,
  `tests/test_init.py::test_the_notice_goes_with_the_entry`,
  `tests/test_keeper.py::test_the_notice_shows_the_lamps_name_as_text`,
  `::test_a_model_id_that_is_not_shaped_like_one_is_not_printed`,
  `::test_the_notice_and_the_repair_for_a_stack_fault_leave_each_other_alone`,
  `::test_one_lamps_notice_is_not_anothers`,
  `::test_a_coordinator_that_was_stopped_raises_no_notice`,
  `tests/test_init.py::test_the_notice_has_words_for_what_it_brings`,
  `::test_the_notice_links_to_a_heading_the_guide_has`,
  `::test_a_choice_is_written_for_a_model_without_a_profile_as_it_was`.
- `tests/test_translations.py::test_the_notice_about_presets_has_its_two_wordings`,
  `::test_the_notice_says_whose_presets_they_are_in_every_language`,
  `::test_the_notice_calls_the_select_what_its_language_calls_it`.

## Revisit when

A model's presets turn out to differ from the G7's in a way that makes
offering them harmful and not merely unconfirmed - that model then needs a
profile, or the select a refusal; or Home Assistant gives a repair a way to be
withdrawn without forgetting that it was ignored, and the notice can come down
with the unload.
