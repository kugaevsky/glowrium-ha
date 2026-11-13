# The mode command carries back the program the lamp holds, asked for as the vendor app asks

- **Status:** accepted
- **Date:** 2026-11-13 (decided by the maintainer the same day: that the
  command carries back what the lamp holds, and that a mode command asks
  for it first); 2026-11-13 (both set from Home Assistant, each with the
  presets that take it)

## Context

The lighting-mode command - `{0x2b, 0x2c, 0x2f, 0x32}` in that key order -
was copied from the first capture of the vendor app with a G7, and `0x2c =
0x02d0` and `0x32 = 0x001e` were taken for fixed parameters: they went out
with every preset, every ramp and every switch back to Circadian. They are
the app's *Daily Light Goal Time* and *Set Enhanced Lighting Duration*, in
minutes (screens of the app with a G7 and with a G6, 2026-11-13). The goal
is how long the light is on in a day, for every preset but Sun SYNC; the
Enhanced duration belongs to Enhanced Two-Phase. A G7 whose owner had set
13 hours in the app held `0x2c = 780`; a G6 held 660 and 90. Home Assistant
put both back to 12 hours and 30 minutes each time.

What was seen, on a G6 from a laptop, on a G7 from the Home Assistant
container under BlueZ, and in two btsnoop captures of the vendor app
(2026-11-12 and 13; one lamp of each):

- Both report `0x2c` and `0x32` when asked for them, and after their clock is
  set. Neither reports them with the state the integration asks for.
- The app asks for every property on every connect, eight ids to a request;
  `2c 2d 2e 2f 31 32 33 34` is one of its requests. Every mode command it
  sends carries the lamp's own goal and Enhanced duration.
- Both lamps keep them through Circadian off and on, and setting the clock
  does not touch them. Switching Circadian on (`{0x09: true}`) resets the
  preset to Sun SYNC and the ramp to 30 minutes on the G6; writes of `0x0d`
  reset nothing.
- Asked for more ids at once than one notification can answer - forty-four,
  about 400 bytes against 244 - the G6 hung, its button too, until it was
  unplugged for a minute. Thirteen ids to a request (`STATE_KEYS`) both lamps
  take, and the app's eight.
- A G8 refuses a request with an ATT error and drops the link (issue #3).
- Under BlueZ the G7 held five fresh links on which the request was made
  right after the answer to `STATE_KEYS`, with no pause, as the first
  exchange makes it, and five on which only `STATE_KEYS` was asked. A first
  link that asked for `0x2c` with no pause had dropped 1.3 s after the answer
  earlier that day; the five did not.

## Decision

- The command carries the goal and the Enhanced duration back as the lamp
  last reported them, and only when they are two bytes of minutes, no more
  than a day. A lamp that has said nothing of them gets the two values of
  the first capture.
- The first exchange asks for them once the state has arrived, with the
  app's request word for word (`PROGRAM_KEYS`), and only of a lamp that
  reported its state when asked. They are asked apart and not added to
  `STATE_KEYS`: thirteen ids is what both lamps have been seen to take.
- Every mode command asks again first, on its own link, of a lamp known to
  report when asked: a command can make its own link, with no first exchange
  on it, and the app is used while Home Assistant holds no link. A lamp not
  known to report is not asked: asking a G8 would drop the link of every mode
  command.
- What vouches for a failed mode command is a report newer than the answer
  to that question. The answer reports the ramp, and a command that sets the
  ramp the lamp already has would match it.
- The mode command sent after switching Circadian on carries the preset as
  it was before the switch, not one the lamp reported since.
- Both are set from Home Assistant, as two numbers under Configuration, and
  setting either is this same command with the one set in it: the vendor app
  set a G6's goal so, and a G6 kept an Enhanced duration written so in a
  round of commands from the bench. In minutes - two bytes of them, not the
  seconds of the ramp - from `protocol.PROGRAM_MINUTES_MIN` to `_MAX`; what
  is set past a bound is written as that bound, so that the echo of it reads
  back as a value and the next mode command carries it on.
- Each is available in Circadian with a preset that takes it, as the app
  shows them: the goal with every preset but Sun SYNC, which follows the sun,
  and the Enhanced duration with Enhanced Two-Phase alone
  (`models.preset_takes`, judged in the base entity's `available`). A preset
  not known hides nothing, as a mode not known does not - and a preset is
  not known for a lamp that has not named its model, nor for a model with
  no profile of its own, a G8 among them: both are driven by the G7's
  presets taken on trust, and a setting is not hidden on a guess.

## Consequences

- A mode command sent after Home Assistant starts and before the lamp's
  first exchange still carries the two old values, and so does every mode
  command to a lamp that refuses to be asked, a G8 among them.
- A mode command costs one request more (an answer of about 60 bytes, some
  80 ms on both lamps), and so does every connect of a lamp that reports.
- Both are properties with readers (ADR 0015), named in the diagnostics
  download in minutes. An answer that leaves out the ids a lamp does not
  have is a report like any other: the head of its map counts the ids that
  were asked for.
- The scripted lamp of the tests answers a request with the ids it was asked
  for and nothing else, as a lamp does.
- The re-sent command after `{0x09: true}` held on the G6: the write was
  acknowledged about 180 ms after it was made, the lamp's report of the reset
  came about 300 ms after, and the lamp applies its commands in order - two
  seconds later the preset, the ramp, the goal and the Enhanced duration were
  the user's.
- Switched into Circadian from Home Assistant, the lamp is in Sun SYNC until
  the preset is put back, and for that moment the goal reads unavailable, as
  the lighting mode reads Sun SYNC and the ramp 30 minutes. Not hidden: that
  would mean holding back what the lamp reports, which nothing here does.
- Neither bound is the app's: what its wheels offer at the top has not been
  confirmed, and a whole day has not been tried on a lamp. The two are kept
  together in `protocol.py`, so that a confirmed bound is one change.
- Shown in hours, both round to whole hours, as the ramp does (README).
