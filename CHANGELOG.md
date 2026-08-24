# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  quiet if it does. A command that genuinely failed still reports the error, 2 s
  later than before.
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
  loading and never reached a retry either. Connects are now capped at 20 s
  (including the wait for the lock) and the reconnect poll starts only after the
  first attempt, so the entry comes up with its entities unavailable and reconnects
  in the background as it always did.
- **State is primed by reading `facebd02` before asking the lamp to report.** One
  read fills most of the property map in a single cheap round trip. It does not
  carry everything, though — measured on real hardware it stops at `0x15`, so the
  indicator, lighting mode, ramp and DST still come from the request, which is sent
  unless the read already covered every key. A model that refuses the request is
  left alone after three consecutive refusals, and only for ten minutes: on a weak
  signal a dropped link is indistinguishable from a refusal, so giving up on the
  first one — or giving up for the whole session — costs a perfectly good lamp four
  of its properties until Home Assistant is restarted.
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

[0.1.1]: https://github.com/kugaevsky/glowrium-ha/releases/tag/v0.1.1
[0.1.0]: https://github.com/kugaevsky/glowrium-ha/releases/tag/v0.1.0
