# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **A command the integration itself refuses says why.** A command that
  arrives while the integration is being reloaded or Home Assistant is
  stopping, or while a connection that could not be closed is still held, was
  reported like any write that failed: "the device may be out of range … a
  Bluetooth proxy near the device usually fixes this". Neither is anything
  the radio did, and a proxy mends neither. Each now has a message of its
  own, in all six languages, and is no longer tried a second time first
  ([#22](https://github.com/kugaevsky/glowrium-ha/issues/22)).

## [0.3.0] - 2026-10-06

> [!WARNING]
> **Breaking: a lighting mode is a key now.** The options of the *Lighting
> mode* select were the presets' English names; they are keys, and the names
> are translations, in all six languages. The device page looks the same, and
> the value the select remembered across a restart is converted by itself.
> What has to be updated is anything that names a mode: `select.select_option`
> with `option: Sun SYNC`, a trigger or condition on the select's state, a
> template that compares it.
>
> | Was | Is |
> | --- | --- |
> | `Sun SYNC` | `sun_sync` |
> | `Before Sunrise` | `before_sunrise` |
> | `Sunrise Sync` | `sunrise_sync` |
> | `Sunset Sync` | `sunset_sync` |
> | `After Sunset` | `after_sunset` |
> | `Two-Phase` | `two_phase` |
> | `Balance` | `balance` |
> | `Enhanced Two-Phase` | `enhanced_two_phase` |

Two things this integration did to its own link, and to the host. Since 0.2.0
it ended every link two seconds after making it, and every link it let go of
left a connection to the system bus behind: left running, 0.2.1 used the bus
up. This release fixes both, and how a link is let go of on a reload, when
Home Assistant stops and when the Bluetooth stack itself has failed; a review
of the whole integration added the rest. How each of these works, and what
was measured, is in [ARCHITECTURE.md](ARCHITECTURE.md).

### Fixed

- **The integration no longer ends its own link on every connect.** Through
  BlueZ, a read of this lamp ends the link two seconds later: on a G7, a link
  that was only written to stayed up nine runs of nine, and one that had been
  read was gone six runs of six. From 0.2.0 the state was read on every
  connect — a link made and lost on every poll tick, about a hundred an hour,
  taken for a lamp at the edge of range. The state is now asked for by a
  write, a lamp that answers is not read at all, and the device info is read
  once per session, last. A lamp that will not report — a G8 refuses the
  request — is still read, as before; that could not be re-measured without
  one.
- **Home Assistant no longer runs the system bus out of connections.** bleak
  opens a D-Bus connection for every Bluetooth client and closes it only at
  the end of a disconnect that BlueZ answers. The integration let clients go
  without one, so about two and a half hours after a start nothing running as
  Home Assistant's user could open a new connection — and every Bluetooth
  connection needs one, as do host tools such as `networkctl` where that user
  is root. Every client that is let go of is now disconnected, and its
  connection to the bus closed whether or not the disconnect went through.
- **A Bluetooth stack that holds on to a dead link no longer goes
  unnoticed.** BlueZ before 5.84 can go on reporting the lamp connected after
  the controller has lost the link (`Failed to disconnect device:
  Disconnected (0x0e)` in its journal), and then answers nothing until the
  adapter is power-cycled. The integration cannot end that state. After three
  disconnects in a row that BlueZ leaves unanswered it backs its background
  connects off, from thirty seconds to five minutes, and says once what the
  state is and what clears it, and once more when the lamp answers again. A
  command is never held back.
- **A link that has died without the stack saying so is noticed.** A link on
  which the state request fails is given ten seconds to be reported dropped
  and is then let go, and a held link that has been silent for five minutes
  is asked for its state again: an idle lamp and a dead link look the same
  until asked.
- **A reload leaves nothing behind.** Unload gave the disconnect three seconds
  and walked away from it, which leaked a connection on a slow link and left
  an unhandled exception in the log on a dead one. And the instance that had
  just been replaced could go on connecting — or finish a connect and keep
  the lamp's single connection from its successor until Home Assistant was
  restarted. The hang-up now finishes behind the unload, and an instance that
  has been stopped takes no new link.
- **The lamp's link is hung up when Home Assistant stops.** Home Assistant
  does not unload integrations on shutdown, and a stop that is cut short — a
  container restart allows ten seconds — left BlueZ holding the link: the
  lamp read as connected and answered nothing until the adapter was
  power-cycled. The link is now dropped as soon as Home Assistant says it is
  stopping. Whether that is enough on a stop that is killed has not been
  measured.
- **Errors from the bus itself are handled as the lost link they are.**
  `EOFError` and "Bad file descriptor" used to end a background connect in a
  traceback, and a command in a raw error with no retry.
- **Turning the DST switch no longer overwrites the offset the lamp
  reported.** Only the flag is changed now; sending a fixed hour with it
  turned a half-hour daylight-saving region into a full one
  ([#4](https://github.com/kugaevsky/glowrium-ha/issues/4)).
- **The device clock is kept right instead of being set once and forgotten.**
  It was only written during first-time bring-up, so a lamp set up months ago
  ran its schedule and its circadian curve off whatever date it had then
  ([#4](https://github.com/kugaevsky/glowrium-ha/issues/4)). It is now
  checked whenever state is primed, and corrected only when it has drifted.
- **A setting changed from the vendor app while Home Assistant was
  disconnected is picked up again.** The lamp is asked for its state each
  time a link is primed, where it used to be asked once per session.
- **The device page shows the lamp's model, firmware and serial number again,
  and a model's own presets and the light's icon take effect.** All of it was
  decided before anything had been read from the lamp. What is read now
  reaches the device registry when it arrives, and the model id is kept with
  the config entry for the next start.
- **A malformed frame can no longer end in an exception.** A map keyed by a
  list or by another map, or a few hundred maps each the key of the next,
  left the decoder as something its caller does not catch. A healthy lamp
  sends neither: this was found by fuzzing the decoder, not in the field.
- **A ramp that was refused, or that never reached the lamp, is not applied
  later.** It is remembered once the lamp has it.
- **A command that failed is not reported as delivered because the lamp said
  something else.** The report that vouches for it now has to be about
  something the command set: the lamp reports its brightness of its own
  accord all day.
- **Entities go unavailable when a command finds the lamp gone**, and the log
  says so.

### Changed

- **A frame with an item the integration cannot read is said to be one.** What
  came ahead of the item is still kept, and the report still counts as an
  answer, but the frame is now named in the log — once as a warning, with the
  bytes it takes to write a reading for it.
- **A frame printed in the log has the lamp's coordinates blanked**, and the
  sunrise and sunset times it works out from them. They are found by their
  bytes, so give such a line one look before posting it all the same.
- **The lamp's state is asked for, not read, and its clock with it**, in one
  request that the lamp answers in one notification. A link that has said
  nothing for five minutes is asked again.
- **A command that has to retry now reconnects for real.** It waits for the
  link it gave up on to be closed first, so a second attempt takes longer
  than it did; a command that works first time is unaffected.
- **"Enable debug logging" takes the Bluetooth libraries with it** — bleak
  and bleak-retry-connector, which is where a link problem is read from. They
  are noisy while it is on.

### Added

- **A diagnostics download** (*Settings → Devices & services → Glowrium → ⋮ →
  Download diagnostics*): the model and firmware, what the lamp last reported
  and where the link stands, in one file. It repeats nothing after the lamp —
  the coordinates are marked as redacted, the serial number and address are
  not put in, and what the integration has no name for is only counted. Home
  Assistant adds a header of its own (its version, the host's time zone, the
  names of your custom integrations), so look the file over before attaching
  it to anything public.
- **A repair for a Bluetooth stack that will not let go**, under *Settings →
  System → Repairs*, with what clears it. It goes away by itself when the
  lamp answers again.
- **The log says when the lamp goes out of reach, and when it is back** —
  once each way, at info level.

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

[0.3.0]: https://github.com/kugaevsky/glowrium-ha/releases/tag/v0.3.0
[0.2.1]: https://github.com/kugaevsky/glowrium-ha/releases/tag/v0.2.1
[0.2.0]: https://github.com/kugaevsky/glowrium-ha/releases/tag/v0.2.0
[0.1.1]: https://github.com/kugaevsky/glowrium-ha/releases/tag/v0.1.1
[0.1.0]: https://github.com/kugaevsky/glowrium-ha/releases/tag/v0.1.0
