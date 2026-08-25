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

See **[ARCHITECTURE.md — How to add a new model](ARCHITECTURE.md#how-to-add-a-new-model)**
for the detailed capture → decode → `models.py` walkthrough, plus the full GATT/CBOR
protocol reference behind it.

## Sending protocol data for a device

Raw protocol data from a lamp nobody here owns is worth more than a bug report,
and it does not oblige anyone to write code — post it in an issue and it gets
written down, whether or not anything comes of it immediately.

What is worth sending, roughly in order of usefulness:

1. **A notify frame the decoder mishandled**, as hex. Turn on debug logging for
   `custom_components.glowrium`; frames that cannot be used are printed with
   their bytes. One such frame from a G8 turned into a fix and a regression
   test — the decoder had been throwing away eleven valid properties because
   the frame promised twelve pairs and carried eleven.
2. **A read of `facebd02`**, as hex or decoded. This is the lamp's whole
   property map. It is how we learned that the read stops short of the
   indicator, lighting mode, ramp and DST keys, which changed how state is
   primed.
3. **The device-info string from `facebd80`** — `brand`, `pkey`, `devid`,
   `version`, and anything else your lamp puts there. Redact `devid` and `mac`
   if you like; the `pkey` is what selects the model profile.
4. **The GATT table** as your stack reports it (`bluetoothctl`, nRF Connect,
   BlueZ). Two characteristics were missing from `const.py` until a G8 owner
   listed them.
5. **A btsnoop capture of the vendor app** switching circadian presets. This is
   the one thing that cannot be substituted: it pins the preset indices, and
   without it a model's entry in `models.py` would be a guess. See below.

Please say which model and firmware the data came from, and whether it is one
lamp or several — a value seen on one device is a datapoint, not a protocol
guarantee, and the docs mark it accordingly.

## Testing on hardware

A macOS/Linux machine with a Bluetooth adapter can drive the coordinator against a real lamp
(keep the vendor app disconnected — the lamp allows a single BLE connection). Docker on macOS
has **no** access to the host's Bluetooth; use an ESPHome Bluetooth Proxy or a native BT host
for live testing.

## Pull requests

Run `ruff` and `pytest` before opening a PR, keep changes focused, and note whether the change
was tested on hardware.
