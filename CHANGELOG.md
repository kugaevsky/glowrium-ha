# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
