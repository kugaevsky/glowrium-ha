# The device half is kept, and asks its keeper for what it needs of whatever runs it

- **Status:** accepted
- **Date:** 2026-10-11 (decided on three designs made independently; built
  the same day, #54)

## Context

The coordinator was handed `hass: HomeAssistant | None`. It was `None` for one
caller, `tools/bench.py`, and for that caller seven branches asked whether
Home Assistant was there: background work had three places to go, the start
raised, Sync location returned without a word, the repair was not raised. The
config entry was a field set by the start, after the coordinator was built -
so there were three configurations where two were meant, and the third, Home
Assistant with no entry yet, was one only tests built. The rule that keeps the
lamp's single connection safe - a connect set off by an advertisement belongs
to the entry (ADR 0006) - was held by the order of two lines in the start.
The bench, for its part, reached into the coordinator for a private flag and
for the link.

Three designs were drawn up independently. One kept Home Assistant's watchers
outside the seam, with three entry points: it cost the coordinator two shallow
public methods and went against ADR 0008. One had ten, with the decisions
about the repairs left in the coordinator: a wider seam, and an adapter for
the bench that would have shipped in the package. One split the coordinator
into two classes: the entities hold one object and use some forty of its
names, so the second class either forwards them or is the adapter under
another name.

## Decision

- The coordinator is built with a **keeper**: what keeps it, and everything
  the device half asks of whatever runs it. A protocol of seven entry points,
  declared in the coordinator's module as the link's module declares its dial
  (`coordinator.Keeper`):
  - `spawn(work, name)`: work that does not outlive what the coordinator is
    kept for - the config entry, the bench's run;
  - `run_lasting(work, name)`: work nothing that ends the coordinator ends -
    a hang-up - returning its task;
  - `watch(advertising, tick)`: watch the air and the clock, and say whether
    the lamp is there now; it may report the lamp heard before it returns;
  - `unwatch()`: cease, idempotent, safe on a keeper that never watched;
  - `stack_fault(count | None)`: a stack that will not hang up, or its end;
  - `lamp_described(model_id, firmware, serial)`: what the lamp said it is;
  - `coordinates()`: where home is; zero and zero from a keeper with no home.
- A coordinator cannot be built without a keeper and a dial. It is handed no
  instance of Home Assistant; its module names of Home Assistant its two
  exceptions, `callback`, `Event` and the clock, and nothing in it asks
  whether Home Assistant is there.
- Home Assistant's keeper is `keeper.EntryKeeper(hass, entry)`, both required.
  It has the entry from when it is built, so work can be put on the entry
  before anything is watched: "the entry before the advertisements" holds by
  construction. Behind it are both lifetimes of work, the three watchers, both
  repairs and their ids, the device registry, the remembered model id and the
  home's coordinates - and the whole of the notice for a model without a
  profile, with the mark of having ceased to watch that guards it (ADR 0014).
  After it has ceased it still writes the registry and the model id when the
  lamp describes itself, and withholds only the notice.
- The dial of a config entry is put together where the entry is set up
  (`keeper.dial_for`).
- The bench has a keeper of its own (`BenchKeeper`), starts the coordinator
  as a setup does, and says where it builds it that no lamp is to be brought
  up (`bring_up=False`). It reaches for no private name.
- The word is *keeper*, not *host*: the glossary keeps "host" for the machine
  whose Bluetooth stack it is.
- The link is not touched: its constructor and the twelve names stand.

## Consequences

- Nothing changes for the owner of a lamp. Three things do where there is no
  Home Assistant, which is the bench:
  - a first connect that fails is reported from what the link says of itself
    - no link is held, or one the first exchange was not made on - and not
    with the lost link it ended in, which is the integration's to log; an
    error of any other kind is printed;
  - the link is looked over on the bench's clock as it is on Home Assistant's
    timer, so one that drops while the bench runs is dialled again. The
    design named two changes and not this one: it follows from the bench
    watching as a keeper does, and is said in the changelog;
  - Sync location with no home is refused where it silently did nothing. No
    run of the bench presses it.
- Two tests could no longer be written and were removed by the maintainer's
  word: that watching needs Home Assistant, and that without Home Assistant
  and without a dial nothing is found.
- No repair is filed under the bare `bluetooth_stack_stuck`: that id was for
  a coordinator started on no entry.
- The seam has three adapters - Home Assistant's, the bench's, the tests' -
  and the type checker reads only the first. The bench's is held to the
  protocol by a test.
- This amends ADR 0006 ("the coordinator keeps it itself"), ADR 0008 ("HA's
  watchers stay in the coordinator") and ADR 0014 ("the coordinator carries
  its own mark of having stopped"); each says so where it did.

## Evidence

- ARCHITECTURE.md, "Reconnect" (the keeper) and "Testing".
- `tests/test_coordinator.py::test_the_device_half_names_five_things_of_home_assistant_and_no_more`
  (reads the source),
  `::test_work_is_started_through_the_keeper`,
  `::test_the_lamp_is_watched_through_the_keeper`,
  `::test_background_work_is_tied_to_the_entry`,
  `::test_a_hang_up_is_not_tied_to_the_entry`,
  `::test_a_reconnect_started_while_starting_belongs_to_the_entry`,
  `::test_a_start_that_fails_part_way_leaves_no_watcher_behind`,
  `::test_what_the_lamp_says_it_is_is_kept_and_handed_to_the_keeper`,
  `::test_a_coordinator_told_not_to_brings_no_lamp_up`.
- `tests/test_init.py::test_a_coordinator_that_was_stopped_raises_no_notice`
  (the registry and the model id are still written).
- `tests/test_bench.py::test_the_bench_reaches_for_no_private_name_of_the_integration`,
  `::test_the_bench_keeps_a_coordinator_by_all_that_the_device_half_asks`,
  `::test_the_bench_drives_a_lamp_as_the_setup_of_an_entry_does`.

## Revisit when

A third thing runs the coordinator and needs an entry point the seven do not
have; or the link is handed to the device half as one thing in place of its
callables, which was decided against for now.
