# One door for letting go of a client, and the invariant is "the bus is closed"

- **Status:** accepted
- **Date:** 2026-10-04 (every client let go of through one hang-up);
  2026-10-05 (the bus closed by hand, with a canary against bleak's own client)

## Context

bleak's BlueZ backend opens a D-Bus connection for each client and closes it
only on the last lines of a `disconnect()` that ran to its end - not when the
reference is dropped, not when the link goes down by itself. The system bus
allows one user 256 connections; a client left with its one keeps it for the
life of the process. Four gaps between "disconnect was called" and "the bus is
closed" have each cost an incident or would have:

- never called: 0.2.1 forgot the client behind every dropped link, and the
  bus was exhausted about two and a half hours after each start;
- cut short by a caller's deadline: unload under its three-second ceiling, one
  connection per reload;
- never answered by BlueZ (2026-10-04): bluetoothd held a link the controller
  had lost, two connections a minute until the bus refused at 256 (ADR 0004);
- answered with an error: not seen; bleak raises one line before it closes.

## Decision

- No client is let go of except through `Link.hang_up`, which runs
  `_async_disconnect` under `_HANG_UP_TIMEOUT` (10 s) as a task that outlives
  whoever asked for it (ADR 0006) - a client whose link is already gone too:
  bleak has no device left to disconnect, and the call only closes the bus.
- After `disconnect()` - returned, raised, timed out or cancelled -
  `_close_bus` closes the bus behind the client if it is still open, through
  bleak's private attributes (there is no public way), leaving bleak as it
  leaves itself when BlueZ reports a link gone. The backend is noted when the
  client is taken: Home Assistant's wrapper forgets it.
- If the attributes have moved or the bus will not close, the client is kept
  (`Unclosed`, held for the lamp across reloads): nothing is dialled over it,
  and the tick tries the hang-up again - one connection held, not one a tick.
- A dial waits for any hang-up still under way (2026-10-08): until a hang-up
  has ended nobody knows whether its client will close.
- The lost-link callback hangs up only the client the link holds; one still
  inside `establish_connection` is bleak's to clean up.

## Consequences

- No second disconnect path, no hang-up tied to the config entry, no going
  back to "a failed hang-up is only logged": each is one of the four gaps.
- A bus closed under a GATT call in flight ends it in `EOFError`/`OSError`,
  and in `AssertionError` for a call asleep between bleak's retries; so the
  link takes `_LINK_ERRORS` for a lost link and makes every GATT call under
  `_gatt_call`. Neither is known outside `link.py` (ADR 0008).
- A Bluetooth proxy's backend has no bus; `_is_bluez` tells by the module the
  backend's class lives in, not by what the object holds.

## Evidence

- ARCHITECTURE.md, "Reconnect": the four-row table and what follows it.
- `tests/test_bus_lifetime.py` counts open bus connections, not calls:
  `test_bleaks_own_client_ends_up_closed` (bleak's real `BleakClientBlueZDBus`
  over a stub bus), `test_bleak_still_calls_these_what_the_link_calls_them`,
  `test_a_disconnect_that_returns_is_not_taken_at_its_word`,
  `test_a_hang_up_cancelled_half_way_still_closes_the_bus`,
  `test_a_link_is_hung_up_and_the_entities_told_in_one_place`; CI runs them
  weekly with nothing pushed, for a bleak release that moves things (ADR 0009).

## Revisit when

bleak closes the bus itself on a lost link and on a disconnect that fails, or
offers a public way to close it; the canary test is what will say so. The
one-door rule stays even then: it is what makes the invariant checkable.
