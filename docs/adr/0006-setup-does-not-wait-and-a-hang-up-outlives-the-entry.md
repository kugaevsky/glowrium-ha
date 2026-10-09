# Setup does not wait for the connect; background work dies with the entry; a hang-up outlives it

- **Status:** accepted
- **Date:** 2026-08-24 (setup returns at once); 2026-10-04 (a hang-up runs on
  `hass`; the entry is taken before advertisements; HA's own stop hangs up)

## Context

The first connect used to be awaited in `async_setup_entry`. With the lamp out
of range a reconnect could hold the lock while setup waited behind it, so the
entry sat in "setup in progress" for as long as the connect took, and a reload
landing in that window cancelled the setup and left the entry in `setup_error`
(0.2.0). Tasks put on `hass` are awaited only at shutdown: a connect that
outlived its coordinator finished connecting and claimed the lamp's single slot
for an owner that no longer existed. A hang-up is the opposite case: cancelled
part-way by an unload's three-second ceiling, it has asked BlueZ to drop the
link and left the bus open - the leak of ADR 0003, one connection per reload.
And Home Assistant does not run an entry's unload on its own stop: a stop cut
short (a container restart allows ten seconds) left BlueZ holding the link,
the lamp reading as connected and answering nothing until a power cycle.

## Decision

- `async_setup_entry` builds the coordinator, starts it and returns in
  milliseconds. The first connect is a background task tied to the config
  entry (`entry.async_create_background_task`, through the coordinator's
  `spawn`), as is every connect, first exchange and probe: they die with it.
- The coordinator takes its entry before it registers for advertisements: Home
  Assistant replays the last advertisement from inside that registration for a
  lamp it already knows, and the connect the replay starts needs an entry to
  be put on, or it lands on `hass` and outlives the unload.
- A hang-up is the one task not tied to the entry. It runs on `hass`
  (`run_lasting`; with no `hass`, in `tools/bench.py`, the coordinator keeps
  it itself), and the deadline of whoever gave the client up ends only their
  wait for it. Unload waits up to `_STOP_TIMEOUT` (3 s) for the lock and as
  long again for the hang-up.
- A stopped coordinator holds no link and takes no new one; the check sits
  after the subscription, the last thing a connect waits for before it commits.
- `EVENT_HOMEASSISTANT_STOP` calls `async_shutdown`: the link is dropped the
  moment the stop is announced, without waiting for the lock, and every
  hang-up from then on gets `_STOP_TIMEOUT` rather than `_HANG_UP_TIMEOUT`.

## Consequences

- An advertising lamp comes up available with nothing read: the light and the
  diagnostic entities read `unknown` until the first state arrives, the
  settings show what they showed before the restart (ADR 0011).
- The device-info string arrives after the entities exist and is carried to
  the device registry by the coordinator (`_async_publish_device_info`); the
  model id is kept with the entry (`CONF_MODEL_ID`) so the next session's
  entities know their presets before anything is read.

## Evidence

- CHANGELOG 0.2.0 and 0.3.0; ARCHITECTURE.md, "Reconnect": "Setup does not
  wait", "A hang-up is the one piece of background work not tied to the
  config entry", "Home Assistant stopping is not an unload".
- `tests/test_init.py::test_setup_does_not_wait_for_the_connection`,
  `::test_the_background_connect_is_cancelled_on_unload`,
  `::test_stopping_home_assistant_hangs_up_the_lamp`;
  `tests/test_coordinator.py::test_a_hang_up_is_not_tied_to_the_entry`,
  `::test_a_link_subscribed_while_the_coordinator_stopped_is_not_kept`.

## Revisit when

Home Assistant runs unload callbacks on its own stop, or bleak closes the bus
on a cancelled disconnect (ADR 0003); or a connect becomes fast enough to be
awaited within what a reload tolerates. Whether the shutdown hang-up is enough
on a stop that is killed has not been measured.
