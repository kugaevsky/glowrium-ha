# Glowrium for Home Assistant

[![Validate](https://github.com/kugaevsky/glowrium-ha/actions/workflows/validate.yml/badge.svg)](https://github.com/kugaevsky/glowrium-ha/actions/workflows/validate.yml)
[![Test](https://github.com/kugaevsky/glowrium-ha/actions/workflows/test.yml/badge.svg)](https://github.com/kugaevsky/glowrium-ha/actions/workflows/test.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Local **Bluetooth** control of the **INLEDCO Glowrium G7** BLE grow light from
Home Assistant — **no cloud, no vendor app**. The BLE protocol was
reverse-engineered from the official `com.inledco.glowrium` app and verified on
real hardware.

> Works fully offline over Bluetooth Low Energy. Once a device has been set up,
> Home Assistant owns it end to end — including provisioning a factory-reset
> lamp without ever touching the vendor app.

![Glowrium G7 device page in Home Assistant](docs/screenshot.png)

## Features

| Entity | Type | Notes |
| --- | --- | --- |
| Light | `light` | On/off + brightness (0–100 %) |
| Operating mode | `select` | **Manual** / **Circadian** / **Schedule** |
| Lighting mode | `select` | 8 circadian presets (Sun SYNC, Sunrise Sync, …) — *Circadian only* |
| Ramp time | `number` | Sunrise/sunset ramp, minutes — *Circadian only* |
| Schedule start / end | `time` | On/off times — *Schedule only* |
| Schedule gradual | `number` | Fade duration, minutes — *Schedule only* |
| Schedule brightness | `number` | Target brightness, % — *Schedule only* |
| Indicator light | `switch` | Front-panel status LED |
| Daylight saving time | `switch` | Device DST handling |
| Sync location | `button` | Pushes HA's home coordinates; the device recomputes its circadian curve itself |
| Latitude / Longitude | `sensor` | Diagnostic — the coordinates stored on the device |
| Activated | `binary_sensor` | Diagnostic — provisioning status (see [Provisioning](#provisioning)) |

**Mode-dependent availability:** *Lighting mode* and *Ramp time* are available
only in **Circadian**; the *Schedule* controls only in **Schedule**. The
integration drives the device's own native Circadian/Schedule engine rather than
reimplementing it in Home Assistant.

**Lighting modes in automations.** The select shows the vendor's names; what
it stores, and what `select.select_option` takes, is a key: `sun_sync`,
`before_sunrise`, `sunrise_sync`, `sunset_sync`, `after_sunset`, `two_phase`,
`balance`, `enhanced_two_phase`. Before 0.3.0 the names themselves were the
options — the [changelog](CHANGELOG.md) has the table if you are upgrading.

## Supported devices

The Glowrium grow-light family (INLEDCO's `com.inledco.glowrium` app) shares the
same BLE control protocol, so other models are likely compatible. The **G7** is
verified end to end; the **G8** runs on real hardware with its presets still
unconfirmed (see below). New models are added in
[`models.py`](custom_components/glowrium/models.py) once tested.

| Model | Form factor | Status |
| --- | --- | --- |
| **G7** | 48 W puck grow light | ✅ Integrated & tested |
| G2 / G2 Pro | Floor grow light (20–48 W) | ⬜ Not yet tested |
| G3 | Vertical grow light (10–36 W) | ⬜ Not yet tested |
| G4 | Desktop grow light (8–12 W) | ⬜ Not yet tested |
| G5 | Dual-head floor grow light (48 W) | ⬜ Not yet tested |
| G6 | Grow-light strip (48 W) | ⬜ Not yet tested |
| G8 | Desktop grow light (10 W) | 🟡 Runs on hardware; presets unconfirmed |
| G9 | Seed-starter kit (30 W) | ⬜ Not yet tested |
| G10 | Telescopic floor grow light | ⬜ Not yet tested |

> Glowrium also makes home/therapy lamps (H-series) and aquarium lights
> (A-series); those are out of scope for this integration.

**About the G8.** Two of them have been run on real hardware by
[@pentafive](https://github.com/pentafive), who also found and fixed the reason
every state entity read `unknown` on them ([#5](https://github.com/kugaevsky/glowrium-ha/pull/5)).
On/off, brightness and state reporting work. What is **not** confirmed is the
circadian **presets**: `models.py` has no G8 entry, so the lighting-mode select
offers the G7's presets and sends the G7's indices, which may not be what the
same names mean on a G8. Choosing one writes that index; the integration has
nothing better to go on. What it will not do is change a setting it would
have to guess at. Setting the **ramp** rewrites the lighting mode along with
it, so on a lamp that has not reported its mode — and a G8, which refuses to
be asked for its state, may never report it — the ramp control reports an
error instead of quietly resetting the mode, and so does a switch to
Circadian that would have to re-apply a ramp. Confirming the preset indices
needs one btsnoop capture from the vendor app; see
[CONTRIBUTING](CONTRIBUTING.md).

One thing changed underneath the G8 in 0.3.0. A lamp is now asked for its
state instead of being read, because on a Linux host a read turned out to end
the link (see the [changelog](CHANGELOG.md)). A G8 refuses to be asked, so it
is still read, as before — which should leave a G8 working as it did, and on
such a host still reconnecting after every read. Neither half of that has been
run on a G8 since the change; a report from one, good or bad, is very welcome.

### Tested an unverified model? Please report back 🙏

The whole Glowrium grow-light family shares the same BLE protocol, so a model
marked **⬜ Not yet tested** above will probably work as-is — but "probably" has
already been wrong once: the G8 splits its state across notifications in a way
the G7 never does, and every state entity read `unknown` until a G8 owner
diagnosed it. So a model is only marked **verified** once someone has actually
run it on real hardware.

**If you own one of these models and have used it with Home Assistant for a
while, please open a pull request with a short test report.** This is the single
most valuable contribution to the project: it's how a model moves from "likely
compatible" to "tested", and how its presets get confirmed in
[`models.py`](custom_components/glowrium/models.py). No need to be a developer —
a clear write-up is enough, and I'll help with the rest.

**What to include in your report**

1. **Model & firmware.** The model (e.g. *G3*) and, from its device page in
   Home Assistant, the model id (`pkey`, e.g. `Glowrium-C051`) and the
   firmware. Easier still: attach the diagnostics download from that page
   (⋮ → *Download diagnostics*), which carries both along with what the lamp
   reported, and leaves out the coordinates, the serial number and the address.
2. **How you used it.** Roughly how long (a few days of normal use is great) and
   how — dashboard, automations, scenes, etc.
3. **What works.** Go entity by entity and say what behaves correctly:
   - Light — on/off and brightness;
   - Operating mode — Manual / Circadian / Schedule;
   - Lighting mode — the circadian presets (do the names/effects match?);
   - Ramp time; Schedule start/end, gradual, and brightness;
   - Indicator light; Daylight saving time; Sync location;
   - Diagnostic Latitude/Longitude sensors and the Activated sensor;
   - Bringing up a **factory-reset** unit (if you tried it).
4. **What's wrong or missing.** Anything that doesn't work, is stuck
   *unavailable*, behaves oddly, or that your device can do but the integration
   doesn't expose. For any errors, enable debug logging for
   `custom_components.glowrium` (Settings → Devices & services → ⋮ → Enable debug
   logging) and paste the relevant lines.
5. **Preset differences (optional).** If the lighting-mode names or order differ
   from the G7, note them. A btsnoop capture from the vendor app pins the exact
   indices — see [CONTRIBUTING.md](CONTRIBUTING.md).

**What the pull request should change**

- Flip your model's row in the **Supported devices** table above to
  **✅ Integrated & tested** (or *⚠️ Partial* with the caveats you found).
- If any presets/names differ from the G7, add or adjust the model's entry in
  [`models.py`](custom_components/glowrium/models.py) (one `GlowriumModel` record).
- Put the test summary itself in the pull-request description.

> Short on time or not comfortable with a PR? A GitHub **issue** with the same
> summary is very welcome too — I'll fold the results in and update the table.

## Requirements

- Home Assistant **2026.7** or newer.
- A **Bluetooth adapter on the Home Assistant host**, or an
  [ESPHome Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy.html)
  within range of the lamp.
- The lamp allows a **single BLE connection** at a time — keep the vendor app
  disconnected while Home Assistant is in control.

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories**.
2. Add `https://github.com/kugaevsky/glowrium-ha` with category **Integration**.
3. Install **Glowrium**, then restart Home Assistant.

### Manual

Copy `custom_components/glowrium/` into your Home Assistant
`config/custom_components/` directory and restart Home Assistant.

## Configuration

The lamp advertises as `Glowrium-G7_XXXXXX` and is **discovered automatically** —
a notification appears under **Settings → Devices & services**. Otherwise add it
via **Add integration → Glowrium** and pick the device from the list. No
credentials are required.

## Provisioning

A factory-reset ("virgin") lamp advertises and accepts settings, but its light
output stays disabled (front-panel LEDs blink) until an activation handshake is
performed. This integration performs that handshake itself — **entirely locally,
with no cloud** — so a freshly reset device is brought up and controllable
without the vendor app. The **Activated** binary sensor reports this status; a
device already paired via the app stays activated across restarts.

## Troubleshooting

The integration says what it finds in the Home Assistant log. These are the
messages worth acting on, and two symptoms that come without one.

- **`BlueZ has left 3 requests in a row to disconnect the lamp unanswered`** —
  the host's Bluetooth stack is holding on to a link that no longer exists,
  and the lamp cannot be reached until it lets go. That is the host, not the
  lamp. Power-cycle the adapter (`bluetoothctl power off`, then
  `bluetoothctl power on`) or restart the bluetooth service; Home Assistant
  takes the adapter back by itself and the lamp follows within about five
  minutes, with `the lamp answers again` in the log. Restarting Home
  Assistant alone does not clear it. BlueZ before 5.84 can get into this
  state — `Failed to disconnect device: Disconnected (0x0e)` in the journal
  of the bluetooth service is how it shows there. The same thing is raised
  under **Settings → System → Repairs**, and goes from there by itself when
  the lamp answers again.
- **`refused the batched state request`** — the lamp will not report its state
  when asked. That is a property of the model (a G8 does it), not a fault:
  commands still work and the state is read instead, but the indicator,
  lighting mode, ramp and DST stay `unknown`. Please
  [report the model](CONTRIBUTING.md#sending-protocol-data-for-a-device).
- **`sent a frame with … trailing bytes and it was dropped`** — a frame the
  decoder would not trust. The message carries it as hex, which is exactly
  what an issue needs — after one look: a frame can hold the coordinates the
  lamp stores. They are the ids `0a` and `0b`, each followed by `fb` and eight
  more bytes; blank those eight before posting.
- **A command fails with "out of range or the Bluetooth adapter busy"** — the
  write did not get through. Check that the vendor app is not connected (the
  lamp takes one connection at a time) and that an adapter or a proxy is
  within range of the lamp.
- **Every entity is `unavailable`** — the lamp is not being heard at all: no
  power, out of range, or the adapter is down. Availability follows the
  lamp's advertisements, not the connection, so a lamp that is merely
  reconnecting does not show this. The log says since when: `is out of
  reach`, and `is back in reach` when it returns.

With debug logging on, a Linux host shows one link after every start or
reload that lasts two seconds — `device info read`, then `disconnected` — and
is rebuilt on the next 30-second tick. That one is expected: the model and
firmware can only be had by a read, and through BlueZ a read costs the link.

For anything else, open an issue with two things from the integration's page
(Settings → Devices & services → Glowrium → ⋮). **Download diagnostics** gives
one file with the model, the firmware, what the lamp reported and where the
link stands; it is made to be attached, and does not contain the coordinates,
the serial number or the address. **Enable debug logging** raises the level
for the integration and for the Bluetooth libraries under it; reproduce the
problem, switch it off again, and attach the lines around the problem from
the log it offers to download.

## Removing the integration

Delete it like any other: **Settings → Devices & services → Glowrium → ⋮ →
Delete**. If it was installed through HACS, remove it there as well and
restart Home Assistant.

Nothing is left behind on the lamp. It keeps its settings, its schedule, its
clock and its activation, and goes on running its own program; the vendor app
can connect to it again as soon as Home Assistant has let go of the link.

## How it works

Control uses a custom GATT service (`facebd00-…`, "rabbit iot ble"): commands are
CBOR maps written to `facebd01`, state arrives as CBOR notifications on
`facebd02` — asked for once on connect, then pushed by the lamp as it changes —
and a readable `facebd80` string exposes model and firmware. The
device computes its own circadian sunrise/sunset curve from the coordinates it
stores, which is why **Sync location** simply writes your Home Assistant home
coordinates and lets the lamp do the astronomy.

For a deeper tour — the GATT/CBOR protocol, the activation handshake, the
availability model, and a step-by-step guide to adding another Glowrium model —
see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest
```

## Disclaimer

This is an unofficial, community-built integration. It is **not affiliated with,
endorsed by, or supported by INLEDCO / Glowrium**. All product names and
trademarks belong to their respective owners. The protocol was reverse-engineered
for local interoperability; use at your own risk.

## License

[MIT](LICENSE) © Nick Kugaevsky
