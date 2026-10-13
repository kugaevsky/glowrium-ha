# A Bluetooth stack that will not hang up is a state, not an event

- **Status:** accepted
- **Date:** 2026-10-05 (the count, the backoff, the warning and the repair);
  2026-10-11 (two states and not one; the day to revisit made checkable)

## Context

Seen on a host with BlueZ 5.82 (2026-10-04): bluetoothd went on reporting the
lamp connected after the controller had lost the link. Every dial was handed
that dead link at once, every GATT call answered `Not connected`, and no
`Disconnect` was answered - `Failed to disconnect device: Disconnected (0x0e)`
in the journal, once per attempt. The kernel is saying the link is already
gone; BlueZ 5.82 treats that as a failure and keeps its state, 5.84 treats it
as the disconnection it is. Nothing a client does ends it: the adapter has to
be power-cycled or bluetoothd restarted. Each dial cost a bus connection
(ADR 0003), two a minute, and `is_connected` went on reading true, so nothing
dialled again for five hours until a command failed.

## Decision

- A hang-up BlueZ leaves unanswered - a timeout, with the bus still open - is
  counted (`Link.note_stuck_hang_up`). From the third in a row
  (`_STACK_FAULT_AFTER`) it is a fault: background dials, the tick's and the
  advertisement's alike, back off, doubling from the 30-second tick interval
  to five minutes (`_STACK_FAULT_BACKOFF_MAX`); one warning says what it is
  and what clears it; a repair goes up under the config entry's id.
- Only silence counts. A hang-up answered with an error is an answer; one that
  goes through breaks the run; a Bluetooth proxy's client is not BlueZ's to
  answer for. Whether the bus could then be closed does not come into it:
  kept or closed, the stack is as stuck.
- The episode ends only with the lamp: its first notification or acknowledged
  write (`Link.note_answer`) clears the count and the backoff, takes the
  repair down, and says so at the level the warning was given.
- A command is never held back by the backoff; it waits only for a hang-up
  still under way (ADR 0003).
- A coordinator that has stopped watching announces no episode (the third
  unanswered hang-up can arrive after the unload, and a repair raised then has
  nobody to take it down) and ends none it did not announce (the link
  remembers whether it did): after a reload the repair is its successor's.
- `is_connected` is a claim and an answer is evidence: a link whose state
  request failed without a refusal gets `_LOST_GRACE` (10 s) to be reported
  dropped and is then let go; a held link silent for `_PROBE_INTERVAL`
  (5 min) is asked for its state again - asked, not read, unless it is a
  lamp that refuses to be asked (ADR 0002).

## Consequences

- The repair is filed by `entry_id`, not by the lamp's address: Home Assistant
  lists open repair ids in every diagnostics download (ADR 0012). The lamp's
  name in it passes `identity.as_text`: the repair renders as Markdown.
- Once a link is only ever written to, a healthy link and the phantom look the
  same from outside (`Connected: yes`); the phantom is told by `hcitool con`.
  The state is not persisted: the next start finds out for itself.

## Evidence

- ARCHITECTURE.md, "Reconnect": "A stack that will not hang up is a state".
  Seen on the host: three unanswered hang-ups, one warning, dials at 30 s,
  90 s and 150 s; adapter power-cycled; the lamp back in four and a half min.
- `tests/test_bus_lifetime.py::test_unanswered_hang_ups_count_only_in_a_row`,
  `::test_a_fault_ends_with_the_lamp_and_not_with_a_hang_up`,
  `::test_a_command_is_not_held_back_by_the_backoff`,
  `::test_a_client_that_is_not_bluezs_is_neither_kept_nor_blamed_on_bluez`,
  `::test_a_stopped_coordinator_ends_no_episode_and_takes_down_no_repair`,
  `::test_the_repair_is_filed_under_the_entry_and_not_under_the_address`.

## Revisit when

Debian stable and Raspberry Pi OS ship BlueZ 5.84 or later. From then on an
unanswered hang-up no longer means a phantom link on the hosts this was seen
on, and the count, the backoff and the repair could go. The bus-closing half
(ADR 0003) stays: that is bleak's.

Made checkable on 2026-10-11 (the map's ticket on the stuck stack, #47):

- The change is one commit of BlueZ, `8c9977b02169`, first in 5.84: a
  disconnect that the kernel answers "already disconnected" is taken for the
  disconnection it is. It has the hang-up answered; it does not keep
  bluetoothd from believing in a dead link until somebody hangs up.
- Home Assistant OS has had 5.85 since 17.0, and Ubuntu 26.04 has it. That is
  not the day: a Container installation uses the host's bluetoothd.
- On 2026-10-11 Debian stable and Raspberry Pi OS had 5.82 and Ubuntu 24.04
  had 5.72, none with a backport of the change and none with a date for one.
  Where to look: Debian's package tracker for `bluez` in stable, and the
  `Packages` list of the Raspberry Pi archive.
- That the change ends an episode is read from BlueZ's code. No host on 5.84
  or later has been seen to go through one.

Decided the same day, and not to be reopened without a second host showing
the fault: the episode stays on the link and starts from nothing after a
reload, while a client that would not close stays with the lamp and outlives
one. They are two states with two texts. Moving the count beside the client
left behind would give a reload nothing to learn again - about a minute and
a half, and a repair that comes back as new - at the price of a change to
the link, for code that the day above retires.
