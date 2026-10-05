# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Two things this integration did to its own link, and what they did to the host.
Since 0.2.0 it ended every link it made, two seconds after making it — a read
on each connect, which BlueZ answers by taking the link down. And every link
it let go of left a connection to the system bus behind, a limit it shares
with everything else on the host: left running, 0.2.1 used it up. With the
fixes for both comes what the same investigation turned up about how a link
is let go of — on a reload, when Home Assistant stops, when the Bluetooth
stack itself is the thing that failed — and three fixes that were waiting for
a release: the lamp's clock, its daylight-saving offset, and settings changed
from the vendor app.

A review of the whole integration before the release added the rest. The
device page gets the lamp's model, firmware and serial number back, a
malformed frame can no longer raise, and there are two new things to reach
for when something is wrong: a diagnostics download, and a repair for a
Bluetooth stack that has stopped answering.

**One change needs something from you.** The options of the *Lighting mode*
select are keys now (`sun_sync`) where they were names (`Sun SYNC`). An
automation, script or template that names a lighting mode has to be updated;
the table is under *Changed*.

### Fixed

- **The integration no longer ends its own link on every connect.** From 0.2.0
  the lamp's state was read as soon as a link was up. Where Home Assistant
  reaches the adapter through BlueZ, a read of this lamp ends the link: the
  read succeeds, BlueZ closes the channel behind it — the very next call
  answers `Not connected` — and two seconds later the lamp is disconnected.
  Measured on a G7 from the host itself, between the integration's own poll
  ticks: a link that was left alone, subscribed to, or asked for its state by
  a write was still up when the test ended it, nine runs of nine; a link on
  which one characteristic had been read — the 235-byte state, the 93-byte
  device info, a single byte — was gone 2.0 s later, six runs of six. With a
  read on every connect that was a link made and lost on every poll tick,
  about a hundred an hour. It was taken for a lamp at the edge of range, and
  it is what gave every other fault in this list its rate. The state is now
  asked for by a write, which the lamp answers at once in a notification, and
  a lamp that answers is not read at all. A lamp that will not report — a G8
  refuses the request — is still read, as before; that could not be
  re-measured without one. The device-info string, which only a read gives,
  is read once per session and last, so the one link it costs has done its
  work by then. Why BlueZ does this is not established; a read through macOS
  does no such thing. On the host, with the fix in place: the first link was
  read for the device info and was gone 2.0 s later, as measured; the next
  stayed up for 2 h 44 min, asked every five minutes and answering each time,
  went down by itself and was replaced 43 s later — three links in three
  hours where there had been a hundred an hour — and a command was answered
  in about a tenth of a second, three of three, with no connection of its own
  to make first.
- **Home Assistant no longer runs the system bus out of connections.** bleak
  opens a D-Bus connection of its own for every Bluetooth client and closes it at
  the end of a disconnect that BlueZ answers — not when the last reference to
  the client is dropped, and not when the link goes down by itself. The
  integration did both. A link that connected but answered nothing was
  forgotten, so was the client behind a failed write, and a link that
  dropped was only noted; each left one connection open for as long as Home
  Assistant ran. At a link every thirty seconds, and with the bus allowing a
  user 256 of them, nothing running as Home Assistant's user could open a new
  one about two and a half hours after a start. Every new Bluetooth
  connection needs one, and where that user is root so do host tools such as
  `networkctl` and `hostnamectl`. Every client the integration lets go of is
  now disconnected — in the background, and outside the deadline of whatever
  gave it up — and its connection to the bus is closed whether or not that
  disconnect went through: asking bleak to disconnect and the connection
  being closed turned out to be two different things, four times over (never
  asked; cut short by a deadline; never answered by BlueZ; answered with an
  error). Closing it goes through bleak's private attributes, as nothing
  public does it; if a bleak release moves them, the client is kept and
  nothing more is dialled, so the cost is one connection and not one per
  poll tick.
- **A Bluetooth stack that holds on to a dead link no longer takes the bus
  with it, and no longer goes unnoticed.** BlueZ before 5.84 can go on
  reporting a device connected after the controller has lost the link: it
  asks the kernel to disconnect, is told the link is already gone, and takes
  that for a failure (`Failed to disconnect device: Disconnected (0x0e)` in
  its journal). From then on every connect is handed that dead link at once,
  every call on it answers `Not connected`, and no disconnect is ever
  answered — until the adapter is power-cycled or the bluetooth service
  restarted. This is the state a container restart was already known to
  leave; it arose here with nothing restarted. The integration cannot end it,
  and now does what it can: each client's bus is closed, so nothing is
  leaked; after three disconnects in a row that BlueZ leaves unanswered the
  background connects back off, doubling from thirty seconds to five
  minutes; one warning says what the state is and what clears it, and a
  second one says when the lamp answers again. A command is never held back.
  Seen on the host: 55 unanswered disconnects with Home Assistant's
  connections steady at two or three; then one warning and connects at 30,
  90 and 150 seconds; the adapter power-cycled; the lamp back by itself four
  and a half minutes later.
- **A link that has died without the stack saying so is noticed.** In the same
  incident the last thing before the fault was a request that failed with
  `Not connected` — and BlueZ never reported the link dropped. The integration
  went on believing it for five hours, until a command failed. A link on
  which the state request fails is now given ten seconds to be reported
  dropped and is then let go, and a held link that has been silent for five
  minutes is asked for its state again: an idle lamp and a dead link look the
  same until asked.
- **Reloading the integration no longer leaks a connection on a slow link, or
  leaves an exception in the log on a dead one.** Unload disconnected under a
  three-second ceiling and walked away when it ran out, which left that
  connection open — the same leak, once per reload. It also let through the
  errors a bus that is out of quota answers with; Home Assistant carried on with
  the reload, but logged them as an exception nobody had handled, in the one
  state where the log is being read. Unload still waits no longer than it did —
  up to three seconds for the lock and three for the hang-up — and the hang-up
  finishes behind it.
- **A reload no longer leaves the old instance connecting, or connected.** Home
  Assistant replays the last advertisement the moment the integration starts
  listening, and the reconnect that started was created before the integration
  had been given its config entry — so nothing cancelled it when the entry was
  unloaded. After a reload the instance that had just been replaced went on
  connecting for up to ten seconds, competing with its successor for the lamp's
  single connection. A command still running during the unload could do the
  same and finish its connect afterwards, leaving the link with an instance
  nothing would ever stop again; on a lamp whose link holds, that keeps the new
  instance out until Home Assistant is restarted. An instance that has been
  stopped now takes no new link at all.
- **The lamp's link is hung up when Home Assistant stops.** Home Assistant does
  not unload integrations on shutdown, so the link was left to be dropped at
  the very end of a clean stop. A stop that is cut short never gets there — a
  container restart allows ten seconds — and BlueZ was left holding the link:
  the lamp then read as connected and answered nothing until the Bluetooth
  adapter was power-cycled. The integration now asks for the link to be dropped
  as soon as Home Assistant announces that it is stopping. Whether that is
  enough on a stop that is killed has not been measured.
- **Errors from the bus itself are handled as the lost link they are.** When the
  system bus refuses Home Assistant, or a connection to it closes with a call
  still waiting, bleak's calls end in `EOFError` or "Bad file descriptor" rather
  than in its own errors. Only the latter were caught, so a background connect
  could end as an unhandled exception with a traceback in the log, and a
  command could fail with a raw error — no retry, and no "cannot connect".
- **Turning the DST switch no longer overwrites the offset the lamp reported.**
  The `0x35` slot carries a flag and the offset to apply, written together, and
  only the flag was ever ours to change — sending a fixed hour turned a
  half-hour daylight-saving region into a full one the moment the switch was
  touched ([#4](https://github.com/kugaevsky/glowrium-ha/issues/4)). A lamp
  that has not reported yet still gets the near-universal hour, so the switch
  stays usable before its state arrives.
- **The device clock is kept right instead of being set once and forgotten.** It
  was only ever written during first-time bring-up, so a lamp set up months ago
  ran its schedule and its circadian curve off whatever date it had then — one
  owner's was six months out, with nothing to show it because the clock is not an
  entity ([#4](https://github.com/kugaevsky/glowrium-ha/issues/4)). It is now
  checked whenever state is primed and corrected only when it has actually
  drifted, so a clock that is right costs no write, however often a link is
  made. Verified on a G7: it was 39 minutes slow five weeks after this
  integration set its clock, and priming put it right with a single write.
- **A setting changed from the vendor app while Home Assistant was disconnected
  is picked up again.** The connect-time read never carries the indicator,
  lighting mode, ramp or DST, so once the batched request had supplied them, the
  check for "do we already have everything" was satisfied by the accumulated
  mirror for the rest of the session and the request was never sent again.
  Reconnecting therefore could not notice that anything had changed. The lamp
  is now asked each time a link is primed; where it has to be read instead,
  what is still to be asked for is judged by what that read carried, never by
  the mirror.
- **The device page shows the lamp's model, firmware and serial number
  again.** Since 0.2.0 the entities have described the device before anything
  had been read from it, and nothing told Home Assistant what the device-info
  string said when it arrived. The page showed the model as "Glowrium" and no
  model id, firmware or serial number — the fields a bug report is asked to
  quote — and every start took back what an earlier one had put there. What
  is read is now handed to the device registry when it arrives, and a start
  no longer overwrites what it has not read yet.
- **A model's own presets take effect.** The per-model profile is chosen by
  the model id, and the lighting-mode select fixed its list when it was built —
  before any lamp had said which model it was. Every lamp got the reference
  presets, and a profile added for another model would have changed nothing.
  The model id is now kept with the config entry once it has been read, so the
  next start knows it before the first entity exists, and the select looks its
  presets up when asked.
- **The light has its icon back.** It was taken from the model's profile at
  the same too-early moment, so it was never set. It is kept with the other
  icons now.
- **A malformed frame can no longer end in an exception.** The decoder parses
  what comes off the radio, and two kinds of frame left it as something its
  caller does not catch: a map keyed by a list or by another map — three bytes
  are enough — and maps nested a few hundred deep. In the notification
  callback that was a traceback per frame; on the path that reads the state it
  took the background connect with it. A map key is now read as what it is in
  this protocol, an unsigned property id, and nesting is bounded. A healthy
  lamp sends none of this: it was found by fuzzing the decoder, not in the
  field.
- **A ramp that was refused, or that never reached the lamp, is not applied
  later.** Setting the ramp on a lamp that has not reported its lighting mode
  is refused, but the ramp was remembered first. The next switch to Circadian
  changed the mode and then failed on re-applying that ramp, so the user was
  told the switch had not worked while watching it take effect. A ramp is now
  remembered once the lamp has it.
- **Entities go unavailable when a command finds the lamp gone.** A command
  that failed let go of the link and told its caller, and nobody else. If the
  lamp had stopped advertising as well, its entities went on reading as
  available until something else happened to tell them. They are told now,
  and so is the log.

### Changed

- **Breaking: a lighting mode is a key, and its name a translation.** The
  options of the *Lighting mode* select were the presets' English names, so
  the name was also the value: it could not be translated, and it could not
  be corrected without breaking whatever had it written down. An option is
  now a key and the name its translation, in all six languages.

  | Was | Is |
  | --- | --- |
  | `Sun SYNC` | `sun_sync` |
  | `Before Sunrise` | `before_sunrise` |
  | `Sunrise Sync` | `sunrise_sync` |
  | `Sunset Sync` | `sunset_sync` |
  | `After Sunset` | `after_sunset` |
  | `Two-Phase` | `two_phase` |
  | `Balance` | `balance` |
  | `Enhanced Two-Phase` | `enhanced_two_phase` |

  The device page looks the same. What has to be updated is anything that
  names a mode: `select.select_option` with `option: Sun SYNC`, a trigger or
  condition on the select's state, a template that compares it. The value the
  select remembered across a restart is converted by itself.
- **A frame with an item the integration cannot read is said to be one.** It
  used to be taken for a map split across two notifications: what came ahead
  of the item was merged, and nothing said that the rest of the frame had not
  been understood. What came ahead is still kept — a lamp whose report
  carries one item nobody has written a reading for goes on working, and its
  report still counts as an answer, so its state is not read instead — but
  the frame is now named in the log, once as a warning, with the bytes it
  takes to write that reading.
- **The lamp's state is asked for, not read, and its clock with it.** The
  request now carries the clock (`0x05`) as well as what the vendor app asks
  for; the lamp answers all of it in one notification. Entities fill in from
  that answer.
- **A link that has said nothing for five minutes is asked whether it is
  still there.** One write, answered with the lamp's state.
- **A command that has to retry now reconnects for real.** The retry used to dial
  while the client it had just given up on was still connected. It now waits for
  that link to be closed first, so a command that needs its second attempt takes
  longer than it did; one that works first time is unaffected.
- **"Enable debug logging" takes the Bluetooth libraries with it.** The button
  on the integration's page now also raises the level of bleak and
  bleak-retry-connector, which is where a link problem is actually read from.
  They are noisy while it is on.

### Added

- **A diagnostics download.** *Settings → Devices & services → Glowrium → ⋮ →
  Download diagnostics* gives one file with the model and firmware, what the
  lamp last reported, and where the link stands — what a bug report is
  otherwise asked for piece by piece. What the integration writes into it
  repeats nothing after the lamp: each property is read the way the
  integration reads it and written out from that reading; the lamp's clock is
  given as how far it was from the host's when it was reported, and how long
  ago that was; the coordinates the lamp keeps are marked as redacted; and
  whatever the integration has no name for is only counted. The lamp's serial
  number and address are not put in. Home Assistant adds a header of its own
  to every diagnostics file - its version, the host's time zone, the names of
  your custom integrations - so look it over before attaching it to anything
  public.
- **A repair for a Bluetooth stack that will not let go.** When BlueZ stops
  answering disconnects (see *Fixed*), Home Assistant now shows it under
  *Settings → System → Repairs* as well as in the log, with what clears it.
  It goes away by itself when the lamp answers again.
- **The log says when the lamp goes out of reach, and when it is back.** Once
  each way, at info level. Reach is what the entities' availability follows:
  an advertisement or a link.

## [0.2.1] - 2026-08-25

Follow-up to 0.2.0, which shipped a defect that showed up on real hardware
within the hour: the light responded to commands while four of its entities
never appeared at all.

### Fixed

- **A lamp at the edge of range is no longer mistaken for one that refuses to
  report.** 0.2.0 decided a device had refused the batched state request if the
  read just before it had worked. On a weak signal that is exactly what happens
  anyway — the read answers, the link drops, and the request fails with "not
  connected" — so the request was silenced on a perfectly good lamp and the
  indicator, lighting mode, ramp and DST never arrived, while commands kept
  working and hid the problem. The request is now silenced only on an error that
  positively reads as the device declining — an authorization or permission
  error. Anything else, including anything unrecognised, is treated as the link:
  asking an unusual device once too often costs a reconnect, while silencing a
  working lamp costs it four entities with nothing in the log above debug.

- **The device page is usable again while the lamp is out of reach.** Since the
  light stopped claiming a confident `off` for a state it had never read, every
  control sat at `unknown` until the lamp answered — and if it could not answer,
  the page stayed that way. The lamp's *settings* now show their last known value
  again after a restart: lighting mode, ramp, DST, the indicator and the schedule
  change when someone changes them, not while Home Assistant is down. The light
  itself is deliberately **not** restored — reporting a lamp as `on` while it is
  physically off is the lie that was removed on purpose, and automations reason
  from it. A report from the lamp always wins over a remembered value.

## [0.2.0] - 2026-08-25

A reliability release. The lamp behaves the same when everything is working;
what changes is what happens when it is not — a device whose state arrives in
pieces, a signal at the edge of range, a command whose acknowledgement is lost.

### Fixed

- **State is now reported on models whose property map spans more than one frame.**
  A device frame may declare more CBOR pairs in its map header than it carries — a
  G8 sends 55 bytes headed `0xac`, promising 12 pairs and containing 11 — and the
  decoder discarded the whole frame, so every state entity read `unknown` while the
  lamp still responded to commands. The pairs that did arrive are now kept. Thanks
  to [@pentafive](https://github.com/pentafive), who diagnosed this on a G8 and sent
  the fix ([#5](https://github.com/kugaevsky/glowrium-ha/pull/5)).
- **A command that the lamp actually carried out is no longer reported as failed.**
  Writes ask for an acknowledgement, and on a weak signal it is the acknowledgement
  that goes missing — so the lamp switched, told Home Assistant its new state, and
  the user got an error toast anyway while watching the light change. A failed write
  now waits up to 2 s for the device to report the state it was asked for, and stays
  quiet if it does — the report has to arrive *after* the write, so a stale reading
  that happens to match cannot vouch for a command that never landed. A command that
  never reached the lamp at all, because it is out of range, still fails at once;
  only one that got as far as the wire waits to see whether it worked.
- **Commands are no longer slowed down by fetching state.** A command now connects
  and writes; the property fetch that used to run first — a device-info read, a
  state read, the batched request and a wait for the activation flag, all inside the
  command's own time budget — happens afterwards in the background. On a lamp with a
  marginal signal that fetch was the difference between a command working and being
  reported as failed.
- **Reloading the integration is quick again.** Unloading waited for whatever connect
  currently held the connection lock, which on an unreachable lamp meant the best
  part of ten seconds.
- **The config entry no longer hangs in "setup in progress" when the lamp is out of
  range.** The initial connect and the background reconnect share a lock, and
  neither had a deadline: with the lamp unreachable, the reconnect could hold the
  lock while setup waited behind it forever, so the integration never finished
  loading and never reached a retry either. Background connects are now capped
  (including the wait for the lock), and setup no longer waits for the connect at
  all — it comes up immediately and connects in the background, so a reload can no
  longer cancel a setup mid-connect and leave the entry in `setup_error`. The cost
  is that entities read `unknown` for a moment after a restart until the first
  state arrives.
- **State is primed by reading `facebd02` before asking the lamp to report.** One
  read fills most of the property map in a single cheap round trip. It does not
  carry everything, though — measured on a G7 and reported for a G8, it returns a
  complete twenty-pair map that stops at `0x15`, so the
  indicator, lighting mode, ramp and DST still come from the request, which is sent
  unless the read already covered every key. A model that refuses the request is
  left alone after three consecutive refusals — and only a lamp that answered the
  read is counted as refusing, since one that fails both is simply out of range.
  Telling those apart matters: counting a weak signal as a refusal cost a healthy
  lamp four of its properties forty seconds after start-up, while a model that
  really does refuse drops its connection every time it is asked.
- **Changing one schedule field no longer overwrites the other four.** The `0x11`
  slot packs the enabled flag, both times, brightness and fade into a single write,
  so substituting a default to change one of them silently rewrote the rest and
  forced the schedule on. Setting the ramp likewise no longer resets the lighting
  mode to preset 1. Both now refuse with an explanation until the device has
  reported the field.
- **The light reports `unknown` rather than a confident `off`** before its state has
  been read.
- A command sent to an out-of-range device no longer appears to hang: `_async_write`
  is capped at 15 s (covering the wait for the connection lock, which a background
  reconnect may hold) and `establish_connection` is limited to 2 attempts instead of
  bleak-retry-connector's default 4. Previously bleak's own retries could stack up for
  over a minute before the button in the UI reported anything.
- Command failures are now raised as a translated `HomeAssistantError` ("out of range
  or the Bluetooth adapter is busy; a Bluetooth proxy near the device usually fixes
  this") instead of surfacing a raw `BleakError` stack trace.
- The refusal shown when a field has not been reported yet is translated too, in all
  six supported languages — it was the last hard-coded English message.

### Added

- The **G8** is no longer listed as untested: two of them run on real hardware,
  with on/off, brightness and state reporting working. Its circadian presets are
  still unconfirmed — `models.py` has no G8 entry, so lighting mode and ramp report
  an error there rather than writing a preset index that may be wrong.
- The rest of the vendor GATT service and the state keys the lamp reports but the
  integration does not decode are now written down — two characteristics
  (`facebd03`, `facebd81`) and eleven property keys, contributed by
  [@pentafive](https://github.com/pentafive) from a G8
  ([#3](https://github.com/kugaevsky/glowrium-ha/issues/3)). Nothing reads them;
  they are recorded so the next person does not have to rediscover them.

### Changed

- A frame dropped for carrying trailing bytes is now logged as itself, with the
  model, firmware, byte count and frame hex, once per session as a warning rather
  than buried at debug level among ordinary undecodable frames. Rejecting such
  frames is new, and on a model that never produced them this is where a regression
  would surface.

## [0.1.1] - 2026-07-20

Internal robustness and maintainability release — no change to entities or behaviour.

### Fixed

- BLE commands are now serialized on the connection lock and retried once across a
  reconnect, so a command sent during the device's periodic (~30–60 min) GATT
  reconnect no longer surfaces an error to the caller.

### Changed

- Extracted the semantic byte-layout codec into `protocol.py` — the `0x11` schedule
  slot and `0x2f` ramp conversions previously lived in three places. The coordinator
  now exposes typed read accessors, and entities read those instead of the raw
  device-state dict.

### Added

- `ARCHITECTURE.md` — a public protocol/architecture reference and a step-by-step
  guide to adding another Glowrium model; linked from the README and CONTRIBUTING.

## [0.1.0] - 2026-07-19

Initial public release.

### Added

- Local **Bluetooth** control of the INLEDCO Glowrium G7 grow light — no cloud, no vendor app.
- `light` — on/off and brightness (0–100 %).
- `select` — operating mode (Manual / Circadian / Schedule) and lighting mode (8 circadian presets).
- `number` — ramp time, schedule gradual and schedule brightness.
- `time` — schedule start / end.
- `switch` — indicator LED and DST.
- `button` — Sync location (pushes Home Assistant's home coordinates; the device recomputes its
  own circadian curve).
- Diagnostic `sensor` (latitude / longitude) and `binary_sensor` (activation status).
- Local activation handshake that brings up a factory-reset lamp without the vendor app.
- Multi-model registry (`models.py`) keyed by the device-info `pkey`, with per-model name, icon
  and circadian presets.
- Automatic Bluetooth discovery of `Glowrium-*` devices.
- Translations: en, ru, zh-Hans, es, de, fr.

[0.2.1]: https://github.com/kugaevsky/glowrium-ha/releases/tag/v0.2.1
[0.2.0]: https://github.com/kugaevsky/glowrium-ha/releases/tag/v0.2.0
[0.1.1]: https://github.com/kugaevsky/glowrium-ha/releases/tag/v0.1.1
[0.1.0]: https://github.com/kugaevsky/glowrium-ha/releases/tag/v0.1.0
