# The link and the device half: no client and no library error leaves `link.py`

- **Status:** accepted
- **Date:** 2026-10-08 (the design chosen); built 2026-10-08 to 2026-10-09 (#21)

## Context

By 0.3.1 one module held the link and the device together, and the three
rules that had each cost an incident (every GATT call guarded; one door for
letting go of a client, ADR 0003; whoever lets go tells the entities,
ADR 0001) were held only by tests that read the source and planted state.

Two designs were drawn up independently. A, "the lease": the link lends a
connected client to the device half for a while, which makes its own GATT
calls under a guard. B, "the conversation": frames cross the seam, never a
client; the link makes every GATT call and the device half speaks on a turn it
is given. They were close on the measure, and A had the smaller interface. B
was chosen for one reason: with no client outside `link.py`, the three rules
cannot be broken from the other module, and no test has to catch it.

## Decision

- `link.py` (`Link`) decides *when* the lamp is spoken to: the dial, the lock,
  the background connect, the first exchange and the probe (both timed by the
  link), a command's delivery with its one retry, every deadline, the hang-up
  and the bus, the stack fault, reach. No client leaves it and no error of the
  Bluetooth library does: `_LINK_ERRORS` and `_gatt_call` are known there
  only; out come `LinkLostError` (`NoNewLinkError` for its own "no") and
  `RefusedError`, plain exceptions.
- `coordinator.py` (the device half) decides *what* is said, on a `Turn`: in
  `_greet` (state request, bring-up, clock, `answered()`, device info) and
  `_probe`, two callables the link is handed and calls itself; and in a
  command, handed to `Link.send(say, vouch=)`. `turn.answered()` is the device
  half's verdict that the lamp answered; an exchange ending without it was
  held on a link that answers nothing, and the link lets go of it.
- The link knows neither Home Assistant nor the protocol. It is handed, by
  name: `dial`, `notify_uuid`, `heard`, `greet`, `probe`, `reach_changed`,
  `stack_fault`, `spawn` (tied to the entry), `run_lasting` (a hang-up, on
  `hass`), `unclosed`. HA's watchers stay in the coordinator and tell it two
  things: `advertising(present)` and `tick()`.
- The coordinator reaches the link by twelve names only: `send`,
  `advertising`, `tick`, `initial_connect`, `let_go`, `shut_down`, `begin`,
  `halt`, `in_reach`, `log_reach`, `note_answer`, `diagnostics`.
- The dial is handed in (`Dial`: given the lost-link callback, returns a
  connected client; `dial_by_bluetooth` by default); the tests stand a
  scripted lamp at it (`tests/lamp.py`) and patch `establish_connection`
  nowhere but in the tests of the default dial and, through `in_range`, for a
  coordinator the integration's own setup made.

## Consequences

- No change of behaviour that was not named and agreed. The one named: the
  entities are told when a client is taken (`Link.open`), not after the first
  exchange. The one fix found on the way: ADR 0005 (a command never written).
- `answered()` is a convention the device half has to keep: a greet that read
  before calling it would spend the link for nothing on BlueZ. A read alone
  is never taken for an answer.

## Evidence

- ARCHITECTURE.md, "Reconnect" (its opening) and "Testing".
- `tests/test_bus_lifetime.py`, reading the source:
  `test_only_the_link_connects_hangs_up_or_knows_what_the_library_raises`,
  `test_every_gatt_call_is_made_where_a_closed_bus_is_a_lost_link`,
  `test_the_coordinator_holds_no_client_and_reaches_the_link_for_its_offer`;
  `tests/test_dial.py` for the dial and the scripted lamp.

## Revisit when

A second transport appears that cannot be expressed as a `Dial`, or a caller
outside the integration needs more of the link than the connect the bench makes.
