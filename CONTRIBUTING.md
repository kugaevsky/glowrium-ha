# Contributing

Thanks for your interest in improving the Glowrium integration!

## Development setup

Home Assistant **2026.7+** and Python **3.14** (the target HA runtime) are required.

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest
```

## Conventions

- **Lint / format:** `ruff` (line length 88). Rules and per-file ignores live in `pyproject.toml`.
- **Tests:** `pytest` (`asyncio_mode = "auto"`). Tests never touch real Bluetooth — the CBOR
  codec and command encoding are verified against bytes captured from a real device.
- **Translations:** `strings.json` is the source of truth; `translations/{en,ru,zh-Hans,es,de,fr}.json`
  must stay key-for-key in sync (hassfest checks this).
- **Manifest:** key order follows hassfest; bump `version` when cutting a release.
- **No blocking I/O in the event loop.**

## Adding a device model

The BLE protocol is shared across the Glowrium family; per-model differences (name, icon,
circadian presets) live in [`custom_components/glowrium/models.py`](custom_components/glowrium/models.py),
keyed by the device-info `pkey`. To add a model:

1. Add one `GlowriumModel` entry to `MODELS`.
2. Confirm the circadian preset indices against a btsnoop capture from the vendor app.
3. Update the "Supported devices" table in the README.

## Testing on hardware

A macOS/Linux machine with a Bluetooth adapter can drive the coordinator against a real lamp
(keep the vendor app disconnected — the lamp allows a single BLE connection). Docker on macOS
has **no** access to the host's Bluetooth; use an ESPHome Bluetooth Proxy or a native BT host
for live testing.

## Pull requests

Run `ruff` and `pytest` before opening a PR, keep changes focused, and note whether the change
was tested on hardware.
