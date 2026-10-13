# Contributing

Thanks for your interest in improving the Glowrium integration!

## Development setup

Home Assistant **2026.7+** and Python **3.14** (the target HA runtime) are required.

```bash
python3.14 -m venv .venv
.venv/bin/python tools/stack.py oldest   # or: newest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest
```

`tools/stack.py` installs one of the two stacks CI runs every check on:

- **`oldest`** - the oldest Home Assistant this project supports, held to one release by
  `constraints-oldest.txt`. That file changes only together with the minimum named above.
- **`newest`** - whatever the test plugin tracks today, pre-releases included. Run it again
  in the same environment to move on to today's.

Keep one virtualenv for each if you want both (`.venv` and, say, `.venv-newest`). In either,
the script then installs what Home Assistant's own Bluetooth and USB integrations require -
`bleak`, `habluetooth`, `dbus-fast` and the rest - the way Home Assistant installs them at
run time: each at the exact version the integration's manifest names, and whatever comes
with it within Home Assistant's own constraints. Both are read from the Home Assistant just
installed; none of those versions is written in this repository. `pip install -r
requirements-test.txt` alone does not give an environment the tests can run in, and a test
fails, naming the library, in one where a library is not at the version this Home Assistant
requires.

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
- **Manifest:** key order follows hassfest; bump `version` when
  [cutting a release](#cutting-a-release) - its tag is `v` and that version.
- **Quality scale:** `custom_components/glowrium/quality_scale.yaml` records where the
  integration stands on each rule of Home Assistant's
  [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/),
  in the form core integrations use. It is a self-assessment - neither Home Assistant nor
  hassfest reads it for a custom integration, and a test holds only its form - so a change that
  alters where a rule stands updates the file with it.
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

**Sent here by the notice about unconfirmed presets?** Home Assistant shows it
for a lamp whose model has no profile here. What settles it is the last item
of the list below, a capture of the vendor app switching presets. Short of
that, open an issue saying what each preset under *Lighting mode* does on your
lamp - which of them change its light over the day, and how - and attach the
diagnostics download (the first item). The notice goes once the model has a
profile; until then it can be ignored.

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
integrations — so look it over all the same. The journal of the host's
Bluetooth service can name any device the host hears or talks to, by its
address and often by its name: that is why a count is asked for below, and
not the journal. And a picture taken of the vendor app should show its list
of presets and nothing else: the app holds an account, and the place the
lamp was set to.

What is worth sending, roughly in order of usefulness:

1. **The diagnostics download**, from the integration's page (⋮ → *Download
   diagnostics*). Model id, firmware, what the lamp reported and where the
   link stands, in one file.
2. **A notify frame the decoder mishandled**, as hex. Turn on debug logging for
   `custom_components.glowrium`; frames that cannot be used are printed with
   their bytes. A frame with trailing bytes, one with an item nothing here
   can read, one that could not be decoded and one that decoded to nothing of
   use are each named in a warning without it. One such frame from a G8 turned
   into a fix and a regression test — the decoder had been throwing away
   eleven valid properties because the frame promised twelve pairs and
   carried eleven.
3. **The debug log of the first few minutes after a restart**, from any model
   other than a G7 — and from a G7 as well where the lamp is reached through
   an ESPHome Bluetooth proxy: then an hour of it, with the lamp reachable
   through the proxy only, and the diagnostics download taken while the lamp
   is connected, which says what kind of client the link runs on. Since
   0.3.0 a lamp is asked for its state before anything is read, and that has
   only been run on a G7, and never through a proxy: the log shows whether
   your lamp answered, refused or stayed silent, and how long each link then
   lasted. Say what the host is — Home Assistant OS, a Linux box with its own
   adapter, an ESPHome proxy — because the Bluetooth stack turned out to
   matter as much as the lamp.
4. **What the journal of the Bluetooth service shows on a host with BlueZ
   5.84 or later** — a Linux host whose own adapter talks to the lamp; Home
   Assistant OS has had 5.85 since 17.0. Three things, and not the journal:
   the version (`bluetoothctl --version`); how many times
   `Failed to disconnect device: Disconnected (0x0e)` stands in a week of the
   journal with the integration running
   (`journalctl -u bluetooth --since "1 week ago" | grep -cF "Disconnected (0x0e)"`);
   and whether Home Assistant raised its repair about a Bluetooth stack
   holding on to a dead link in that week. Without a shell on the host the
   last of the three is still worth sending, with the version of the
   operating system.
5. **A read of `facebd02`**, as hex or decoded. This is the lamp's whole
   property map. It is how we learned that the read stops short of the
   indicator, lighting mode, ramp and DST keys, which changed how state is
   primed. Take it last: through BlueZ a read of this lamp ends the link about
   two seconds later, so anything else you wanted from that connection has to
   come first.
6. **The device-info string from `facebd80`** — `brand`, `pkey`, `version`,
   and the names of anything else your lamp puts there. Leave out the values
   of `devid` and `mac`; the `pkey` is what selects the model profile.
7. **The GATT table** as your stack reports it (`bluetoothctl`, nRF Connect,
   BlueZ). Two characteristics were missing from `const.py` until a G8 owner
   listed them.
8. **The names of the eight presets as the vendor app shows them in German,
   Spanish, French, Russian or Chinese** — a picture of its list of presets,
   or word that the app has no such language. Here the names are the app's
   English ones in every language, on purpose: a translation made up here
   would be harder to match with the app than the English name is. What Home
   Assistant stores is a preset's key, so a name can be replaced at any time.
9. **A btsnoop capture of the vendor app** switching circadian presets. This is
   the one thing that cannot be substituted: it pins the preset indices, and
   without it a model's entry in `models.py` would be a guess. How to take
   one is in [ARCHITECTURE.md](ARCHITECTURE.md#how-to-add-a-new-model),
   steps 1 and 2.

Please say which model and firmware the data came from, and whether it is one
lamp or several — a value seen on one device is a datapoint, not a protocol
guarantee, and the docs mark it accordingly.

### What nobody here can find out

These are open because nobody working on this has what it takes to find
out. Each is settled by something from the list above, sent by whoever has
the lamp, the proxy, the host or the app. An issue with it is all that is
asked; if it turns into work, that issue is where the work gets described.

- **Through an ESPHome Bluetooth proxy, with any model, a G7 included.**
  What the documents say of the link was measured through an adapter of the
  host's own, under BlueZ and on macOS. Through a proxy three things are not
  known. Whether a read of the lamp ends the link, as it does under BlueZ
  two seconds later. Whether the lamp reports its state when asked, so that
  nothing has to be read. And whether the entities stay available between
  one link and the next: a G7 was seen not to advertise while it is
  connected, and a proxy's list of devices follows advertisements, so the
  lamp may count as gone by the time a link drops. *What settles it:* the
  debug log and the diagnostics download, as the list asks for them of a
  proxy. The log is read for `device info read` with `disconnected` two
  seconds after it; for anything said of the state request ahead of `primed`
  (that it was refused, that it failed, or that it was acknowledged and
  nothing reported), each of which means the lamp had to be read; and for
  `is out of reach` right after a `disconnected` while the lamp stood where
  it was.
- **A G8 since 0.3.0.** How long a link lives, and on which Bluetooth stack;
  and whether the lamp is asked three times and from then on only read, as
  the changelog says it is. *What settles it:* the debug log and the
  diagnostics download.
- **BlueZ 5.84 or later.** Before 5.84 the stack can leave a request to
  disconnect unanswered while it holds a link that no longer exists. From
  5.84 on such a request is answered - as read in BlueZ's source: no host
  that has it has been watched through that. *What settles it:* what the
  journal of the Bluetooth service shows. Whichever way it comes out is
  worth an issue: the count says whether the fault still shows in the
  journal, the repair whether the integration still met it. It bears on what
  the README's troubleshooting says of 5.84, and on the one thing
  [ADR 0004](docs/adr/0004-a-stack-that-will-not-hang-up-is-a-state.md)
  could only read from BlueZ's code.
- **The names of the presets in the vendor app in another language.**
  *What settles it:* a picture of the app's list of presets. With it the
  names in the translations can be the app's own.
- **The preset indices of any model other than the G7.** *What settles it:*
  the capture of the vendor app switching presets on that model. With it the
  model gets a profile, and the notice about unconfirmed presets goes.

## Testing on hardware

A macOS/Linux machine with a Bluetooth adapter can drive the coordinator against a real lamp
(keep the vendor app disconnected — the lamp allows a single BLE connection):
`tools/bench.py` starts the integration's coordinator as Home Assistant does, reports what the
lamp said, and never writes a setting of its own accord - nor brings up a lamp that says it is
not activated.
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

## Cutting a release

A release is one file, `glowrium.zip`: the integration's directory as it is at the tag, with
`manifest.json` at the archive's root. `hacs.json` tells HACS to take that file, and HACS - as
its source reads - does not fall back to the tag's source when the file is missing. So a
release must never be public without it, and the order below sees to that.

1. The commit to release is on `main` with CI green; its manifest has the new `version` and
   the changelog a heading for it.
2. Push the tag: `v` and the manifest's version, exactly - `v1.2.3` for `1.2.3`.
3. Wait for the **Release** workflow. It builds the archive from the tag and makes a **draft**
   of the release that carries it, titled with the tag. A tag that is not the manifest's
   version fails the run and nothing is made.
4. Write the draft's notes, see that it carries `glowrium.zip` and no other file, and publish
   it.

**None of this has been run against GitHub or a live HACS yet.** What the workflow tells `gh`
is tested against a stand-in, and what HACS does with the file was read in its source
([ADR 0017](docs/adr/0017-a-release-is-one-archive-and-hacs-installs-nothing-else.md)). Before
the first release that depends on it, rehearse it in a scratch repository: push a tag and see
the draft and its file; run the workflow again on the tag, which now has a draft; publish, then
install it with HACS, update to it from a release without a file, and go back. Take this
paragraph out once that is done.

What the order rests on:

- **Do not create the release by hand before the draft is there.** A release that exists when
  the workflow looks is given the archive, and is public without its file until then. One made
  after the workflow has looked gets nothing from that run, and stands beside the draft as a
  second entry for the tag. Neither has been tried.
- **If the run fails after the draft is made,** the draft may be there without its file: `gh`
  makes the draft first and puts the file on it second. A draft is not public; run the
  workflow again and the file is put on it.
- **The file of a published release is never replaced**, and the workflow leaves it alone.
  GitHub's count belongs to the file, and a new file starts at zero; and its bytes would
  change under whoever has installed it. What is wrong with a published release is put right
  by the next one.
- **No second file on a release.** HACS, as its source reads, reports the count of the first
  file a release has. The workflow says so when it finds one, and takes nothing off.
- The workflow publishes nothing, edits the title and notes of no release, and makes, moves
  and deletes no tag. Its one job that can write hands the token to one script, the one that
  talks to GitHub; the two actions the job uses get the job's token as in any job.

The archive is built by `python tools/release.py archive REF OUT`, out of git's objects and
never out of the working directory, and CI builds it on every push. Two builds of one commit
on one machine are the same bytes; another version of Python or zlib may compress them
differently, and the files inside are the tag's either way. The **Release check** workflow
looks every day at each published release, pre-releases too, and fails when one whose own
`hacs.json` promises the file does not carry exactly that file.
