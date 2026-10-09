# Entity availability follows presence or a held link, not the GATT connection

- **Status:** accepted
- **Date:** 2026-07-19 (initial release); the rule and its warning to reviewers
  written into ARCHITECTURE.md on 2026-07-20

## Context

A GATT link to this lamp does not last, and nothing the integration does keeps
one up for good. Early notes put the lamp's own drops at every 30-60 minutes;
the one link timed after the state stopped being read (2026-10-05, a G7 on
BlueZ) held 2 h 44 min, went down by itself, and was rebuilt and primed 43 s
later - one link, not a rate. In 0.2.0 and 0.2.1 the integration ended every
link itself two seconds after making it (ADR 0002): about a hundred an hour.

`available` is what every entity shows and what the recorder keeps. Tied to
the connection, every entity would flap to `unavailable` on each reconnect and
spray that into the history while the lamp was fine: it advertises
continuously, at about 1 Hz, whether or not anyone is connected.

## Decision

An entity is available while the lamp is in reach: a connected client is held,
or the lamp is heard advertising (`Link.in_reach`, read by
`GlowriumEntity.available` through `GlowriumCoordinator.available`). Presence
is seeded from `bluetooth.async_address_present`, set by the advertisement
callback and cleared by `bluetooth.async_track_unavailable`; the watchers are
the coordinator's and hand what they see to `Link.advertising`.

The link is rebuilt silently underneath an available entity: from the
advertisement callback when the lamp reappears, and from the 30-second tick
(`Link.tick`) regardless. Between links each entity shows the last value it
had, and a command made then dials its own link first.

## Consequences

- Do not simplify `available` back to the connection state. The flapping is
  the whole reason the rule exists; a reviewer who reads `connected or present`
  and "fixes" it to `connected` brings it back.
- An advertising lamp comes up available with nothing read: the light and the
  diagnostic entities read `unknown` until the first state arrives (ADR 0006).
- A connected lamp that is quiet is not out of reach: the `is out of reach` /
  `is back in reach` log lines (`Link.log_reach`) use the entities' expression.
- The entities are told wherever either half of the expression can change: a
  client taken (`Link.open`), a client let go of for what it did
  (`Link._drop`), a command that failed, an advertisement that started or
  stopped. A stopping coordinator says nothing.
- Mode-dependent entities gate themselves further on the operating mode, and
  stay available while it is unknown (`mode_allows`).

## Evidence

- ARCHITECTURE.md, "Availability model"; the 2026-10-05 link in "Priming state
  on connect".
- `tests/test_coordinator.py::test_available_follows_presence_not_connection`,
  `::test_a_lamp_with_a_link_is_not_out_of_reach_for_being_quiet`,
  `::test_going_out_of_reach_and_coming_back_are_each_said_once`,
  `::test_a_lamp_that_is_advertising_at_start_is_in_reach_from_the_start`.

## Revisit when

A model or a host stack is found on which a link holds for days and drops only
when the lamp is really gone - and only if the history noise of reconnects is
then shown to cost more than an entity reading available while the lamp cannot
be commanded.
