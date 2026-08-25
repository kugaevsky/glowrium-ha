# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  carry everything, though — observed on a G7 and reported for a G8, it stops at
  `0x15`, so the
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

[0.2.0]: https://github.com/kugaevsky/glowrium-ha/releases/tag/v0.2.0
[0.1.1]: https://github.com/kugaevsky/glowrium-ha/releases/tag/v0.1.1
[0.1.0]: https://github.com/kugaevsky/glowrium-ha/releases/tag/v0.1.0
