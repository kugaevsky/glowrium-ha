# Contributing

Thanks for your interest in improving the Glowrium integration!

## Development setup

Home Assistant **2026.7+** and Python **3.14** (the target HA runtime) are required.

```bash
python3.14 -m venv .venv
.venv/bin/python tools/test_stack.py oldest   # or: newest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest
```

`tools/test_stack.py` installs one of the two stacks CI runs every check on:

- **`oldest`** - the oldest Home Assistant this project supports, held to one release by
  `constraints-oldest.txt`. That file changes only together with the minimum named above.
- **`newest`** - whatever the test plugin tracks today, pre-releases included. Run it again
  in the same environment to move on to today's.

Keep one virtualenv for each if you want both (`.venv` and, say, `.venv-newest`). In either,
the script then installs what Home Assistant's own Bluetooth and USB integrations require -
`bleak`, `habluetooth`, `dbus-fast` and the rest - at the versions that Home Assistant
dictates, read from the Home Assistant it has just installed. None of those versions is
written in this repository, and `pip install -r requirements-test.txt` alone does not give
an environment the tests can run in.

## Conventions

- **Lint / format:** `ruff` (line length 88). Rules and per-file ignores live in `pyproject.toml`;
  the selection includes bandit's checks (`S`), and a broad `except` or a naive `datetime` has
  to say why it is one.
- **Types:** `mypy --strict` over the integration, configured in `pyproject.toml`.
- **Tests:** `pytest` (`asyncio_mode = "auto"`). Tests never touch real Bluetooth — the CBOR
  codec and command encoding are verified against bytes captured from a real device. CI holds
  coverage of the integration to 95 %.
- **Translations:** `strings.json` is the source of truth; `translations/{en,ru,zh-Hans,es,de,fr}.json`
  must stay key-for-key in sync (hassfest checks this).
- **Manifest:** key order follows hassfest; bump `version` when cutting a release.
- **No blocking I/O in the event loop.**

## Adding a device model

The BLE protocol is shared across the Glowrium family; per-model differences (name, circadian
presets) live in [`custom_components/glowrium/models.py`](custom_components/glowrium/models.py),
keyed by the device-info `pkey`. To add a model:

1. Add one `GlowriumModel` entry to `MODELS`. A preset is a key (`sunrise_sync`) mapped to the
   index the lamp takes.
2. Confirm the circadian preset indices against a btsnoop capture from the vendor app.
3. Give every new preset key a name in `strings.json` and in each file under `translations/`
   (`entity.select.lighting_mode.state`). The vendor's own name is the right one; a test fails
   for a key without a name.
4. Update the "Supported devices" table in the README.

See **[ARCHITECTURE.md — How to add a new model](ARCHITECTURE.md#how-to-add-a-new-model)**
for the detailed capture → decode → `models.py` walkthrough, plus the full GATT/CBOR
protocol reference behind it.

## Sending protocol data for a device

Raw protocol data from a lamp nobody here owns is worth more than a bug report,
and it does not oblige anyone to write code — post it in an issue and it gets
written down, whether or not anything comes of it immediately.

**Before you post bytes: they can say where you live.** The lamp stores the
coordinates it was given for its circadian curve, and both a notify frame and
a read of `facebd02` can carry them. In hex they are the ids `0a` and `0b`,
each followed by `fb` and eight more bytes — blank those eight. The sunrise
and sunset times the lamp works out from them (`1834`, then a byte string)
give the place away as well. Where the integration's own log lines print a
frame, both are replaced by `xx` - as far as they can be found, which is by
their bytes: a frame that begins part-way through one of them has nothing to
find it by, so look such a line over all the same. In a capture, in a read
taken with another tool, and in what the Bluetooth libraries write into a
debug log, nothing is replaced. The device-info string carries the serial
number (`devid`) and the address (`mac`); leave both out. A debug log names
the lamp by its address, and the Bluetooth libraries name the other devices
they hear. What the integration writes into the diagnostics download (the
first item below) has none of this in it; Home Assistant adds a header of
its own to that file — version, time zone, the names of your custom
integrations — so look it over all the same.

What is worth sending, roughly in order of usefulness:

1. **The diagnostics download**, from the integration's page (⋮ → *Download
   diagnostics*). Model id, firmware, what the lamp reported and where the
   link stands, in one file.
2. **A notify frame the decoder mishandled**, as hex. Turn on debug logging for
   `custom_components.glowrium`; frames that cannot be used are printed with
   their bytes. A frame with trailing bytes, or with an item nothing here can
   read, is named in a warning without it. One such frame from a G8 turned
   into a fix and a regression test — the decoder had been throwing away
   eleven valid properties because the frame promised twelve pairs and
   carried eleven.
3. **The debug log of the first few minutes after a restart**, from any model
   other than a G7. Since 0.3.0 a lamp is asked for its state before anything
   is read, and that has only been run on a G7: the log shows whether your
   lamp answered, refused or stayed silent, and how long each link then
   lasted. Say what the host is — Home Assistant OS, a Linux box with its own
   adapter, an ESPHome proxy — because the Bluetooth stack turned out to
   matter as much as the lamp.
4. **A read of `facebd02`**, as hex or decoded. This is the lamp's whole
   property map. It is how we learned that the read stops short of the
   indicator, lighting mode, ramp and DST keys, which changed how state is
   primed. Take it last: through BlueZ a read of this lamp ends the link about
   two seconds later, so anything else you wanted from that connection has to
   come first.
5. **The device-info string from `facebd80`** — `brand`, `pkey`, `version`,
   and the names of anything else your lamp puts there. Leave out the values
   of `devid` and `mac`; the `pkey` is what selects the model profile.
6. **The GATT table** as your stack reports it (`bluetoothctl`, nRF Connect,
   BlueZ). Two characteristics were missing from `const.py` until a G8 owner
   listed them.
7. **A btsnoop capture of the vendor app** switching circadian presets. This is
   the one thing that cannot be substituted: it pins the preset indices, and
   without it a model's entry in `models.py` would be a guess. See below.

Please say which model and firmware the data came from, and whether it is one
lamp or several — a value seen on one device is a datapoint, not a protocol
guarantee, and the docs mark it accordingly.

## Testing on hardware

A macOS/Linux machine with a Bluetooth adapter can drive the coordinator against a real lamp
(keep the vendor app disconnected — the lamp allows a single BLE connection):
`tools/bench.py` connects, primes and reports, and never writes a setting of its own accord.
What it prints can be shown to others as it is: the coordinates, the sunrise and sunset times
and the serial number are left out, and the lamp's address is printed by its last characters
only, in the integration's own log lines and in the Bluetooth stack's errors too. The name the
lamp advertises is printed as it is. `--show-private` prints everything.
Docker on macOS has **no** access to the host's Bluetooth; use an ESPHome Bluetooth Proxy or
a native BT host for live testing.

The two stacks do not behave alike. Through BlueZ a GATT read of this lamp ends the link two
seconds later; through macOS it does not — which is how 0.2.0 and 0.2.1 came to end their own
link on every connect without it ever showing on a laptop. A change to how the link is made,
kept or let go of needs a Linux host before it is believed, and its pull request should say
which stack it was run on.

## Pull requests

Run `ruff` and `pytest` before opening a PR, keep changes focused, and note whether the change
was tested on hardware.
