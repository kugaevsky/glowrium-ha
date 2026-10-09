# Glowrium

One Home Assistant integration for one family of Bluetooth grow lamps, talking
to each lamp directly over GATT with no cloud and no vendor app. This is the
vocabulary the code, ARCHITECTURE.md and the tests use; the module in
parentheses is where a term lives. Decisions are in `docs/adr/`.

## Language

### The lamp and what it says

**Lamp**:
One Glowrium grow light at one Bluetooth address: the thing the integration
talks to, and the only thing it talks to (`coordinator.py` is built for one;
`tests/lamp.py` stands a scripted one at the dial).

**Frame**:
One CBOR message on the wire: a command written to the lamp, or a notification
the lamp sends (`cbor.py`).

**Property**:
One integer-keyed value of the lamp's state - power `0x06`, brightness `0x08`
and so on; a frame is a map of them (`const.py`).

**Report**:
A frame from the lamp of which at least one property was kept: taken into
the mirror and counted. A report, however partial, is the lamp answering
(`Mirror.take`, through `coordinator._ingest`).

**State request**:
The write of `STATE_KEYS` - the ids the vendor app asks for, plus the clock -
to the notify characteristic, which makes the lamp report them. Asked, not
read: a read is the way out for a lamp that will not report, and on BlueZ it
ends the link (`coordinator._request_state`).

**Priming**:
Getting the state into the mirror on a new link: the state request, and the
read only where the lamp will not report. A link is primed once its first
exchange has been made on it (`Link.primed`).

**Device-info string**:
The one thing that has to be read - brand, `pkey`, serial, address, version -
read once per session and last on a link
(`coordinator._async_read_device_info`).

**Model profile**:
What differs per model - the marketing name and the lighting-mode presets -
looked up by the `pkey` of the device-info string; an unknown `pkey` gets the
reference presets under the family name (`models.py`, `GlowriumModel`).

**Lighting mode**:
One circadian preset of the lamp, named by a key (`sunrise_sync`) that maps to
a model-specific index under `0x2b`; what a person reads is the key's
translation (`models.py`, `strings.json`).

**Operating mode**:
Manual, Circadian or Schedule: one select over the lamp's two mutually
exclusive flags `0x09` and `0x0d` (`coordinator.operating_mode`).

**Activation**:
The lamp's gate on its light output, flag `0x14`: a factory-reset lamp
advertises and takes commands but stays dark until the local three-write
bring-up has been made on the greet (`coordinator._async_activate`).

**Identity**:
The lamp's own words about itself - advertised name, model id, firmware -
made safe to show: as text, and only in the shape each claims (`identity.py`).

### The link

**Link**:
What holds the client to one lamp and decides when the lamp is spoken to:
taking a client, holding it, letting it go, every deadline (`link.py`, `Link`).

**Dial**:
The one act of making a connected client, handed to the link as a callable
that is given the lost-link callback; by default the lamp as Home Assistant's
Bluetooth finds it, in the tests a scripted lamp (`link.py`, `Dial`,
`dial_by_bluetooth`).

**Connect**:
The link's background flow around a dial: take the lock, open (dial, subscribe,
then keep), make the first exchange (`Link.connect`, `Link.open`).

**Hang-up**:
The one way a client is let go of: `disconnect()` under its own ceiling, then
the client's bus closed whatever came of that (`Link.hang_up`).
_Avoid_: "disconnect" for the whole of it - that is the library's call, one
step of a hang-up, and calling it is not the same as the bus being closed.

**Bus**:
The D-Bus connection bleak's BlueZ backend opens for each client and closes
only at the end of a disconnect that ran to its end; one user is allowed 256
(`link._close_bus`).

**Unclosed client**:
A client that would neither hang up nor have its bus closed, kept for the lamp
- not for one coordinator - for as long as the process runs; nothing is dialled
over it (`link.py`, `Unclosed`; held per address in `__init__.py`).
_Avoid_: "unreleased" - the old name.

**Stuck hang-up**:
A hang-up BlueZ left unanswered: a timeout with the bus still open. An error
is an answer and does not count (`Link.note_stuck_hang_up`).

**Stack fault**:
The state the host's Bluetooth stack is in from the third stuck hang-up in a
row: it holds a link that no longer exists, and nothing a client does ends it
(`link.py`, `_STACK_FAULT_AFTER`; the repair is raised in `coordinator.py`).
_Avoid_: "wedged", "stuck stack" - older prose and test names.

**Episode**:
One announced stack fault, from the warning and the repair to the lamp's first
answer; ended only by the link that announced it, which remembers that it
did (`Link.note_answer`).

**Lost**:
A link that is gone, or could not be had: what the device half hears as
`LinkLostError`; `NoNewLinkError` is the link's own "no" - stopped, or an
unclosed client (`link.py`; the callback a dial is given is `Link.on_lost`).
_Avoid_: "disconnected callback" for `on_lost` - "disconnected" is what BlueZ
reports, and it reports it for clients the link does not hold.

**In doubt**:
A link that failed a call without a refusal and has not been reported lost;
given `_LOST_GRACE` to be, then let go by the tick (`Turn.in_doubt`).

**Refused**:
The lamp said no and the link stands: an ATT authorization or permission
error, as a G8 gives the state request (`link.py`, `RefusedError`).

**Present**:
The lamp is being heard advertising, as Home Assistant's scanners report it
(told to the link through `Link.begin` and `Link.advertising`).

**In reach**:
The lamp is present or a connected client is held: what every entity's
`available` is, and what the `out of reach` / `back in reach` log lines judge
by (`Link.in_reach`, `Link.log_reach`).

**Tick**:
The link looked over every thirty seconds by the coordinator's timer: unclosed
clients hung up again, a dropped link dialled, a doubted link let go, an
unprimed link given its first exchange, a silent one probed (`Link.tick`).

**Session**:
The life of one coordinator, from start to stop; "once per session" is said of
the device-info read and of each warning that asks to be reported.

### Speaking on a turn

**Device half**:
The side that knows what is said to the lamp: the mirror, the commands, the
clock, the activation, the telling of the entities (`coordinator.py`,
`GlowriumCoordinator`). "The coordinator" is the class's name; "the device
half" is its role at the seam.

**Seam**:
The split between the link and the device half (#21): no client and no library
error crosses it; the device half reaches the link by twelve names and speaks
on a turn (held by `tests/test_bus_lifetime.py`, `_OF_THE_LINK`).

**Turn**:
One go at the lamp on a client the link holds, given to the device half in
place of the client: write, read, say the link is in doubt, say the lamp
answered (`link.py`, `Turn`).

**Talk**:
Something the device half says on a turn: the greet, the probe, and a
command's `say` (`link.py`, `Talk`).

**Greet**:
The first exchange on a new link: the state request, the bring-up if needed,
the clock if drifted, `answered()`, and last the device-info read
(`coordinator._greet`, run by `Link.prime_held`).

**Probe**:
The question for a link that has said nothing for five minutes - the state
request again; a link that does not answer is let go (`coordinator._probe`,
`Link.probe_held`).

**Answered**:
The device half's verdict that the lamp answered what it was asked; an
exchange that ends without it was held on a link that answers nothing. A read
alone is not an answer (`Turn.answered`).

**Send**:
The delivery of a command by the link: under the lock, on a link it dials if
none is held, one retry on a new link, one deadline (`Link.send`).

**Vouch**:
The one question asked when a write failed: has the lamp reported what the
command set, newer than the write? A command vouched for is delivered
(`Link.send(vouch=)`, `coordinator._async_device_confirms`).

### The Home Assistant side

**State mirror**:
The last value the lamp gave for each property and the last value written to
it, never emptied when a link drops, and the moment its clock came in: a
read-only mapping with two ways in, a frame taken and a write echoed. It
keeps every known property and every echo, and a bounded number of the rest
(`mirror.py`, `Mirror`; `coordinator.state`).

**Known property**:
A property the integration has a name for: what it asks the lamp for, what
only its commands write, and the curve. The mirror always keeps these; of
the properties nobody named it keeps the first sixty-four a session brings,
and counts each time another is reported (`const.KNOWN_KEYS`,
`Mirror.not_kept`).

**Echo**:
A command's payload reflected into the mirror after the lamp acknowledged the
write. Our word, not the lamp's: no report, no count, no wake-up, no vouching
(`Mirror.echo`).

**Carried**:
The ids one frame put into the mirror; what `Mirror.take` returns. Empty: the
frame was no report. An id there was no room for was not carried.

**Reported since**:
The ids some report numbered above a mark carried. What priming compares with
the keys it asked for, and vouching with what a failed write set
(`Mirror.reported_since`, `Mirror.reports`).

**Entity listeners**:
The entities, each told on its own when the mirror or the reach changed; one
that raises keeps the news from nobody (`coordinator.async_add_listener`,
`coordinator._async_notify_listeners`).

**Control**:
The light and the operating mode: what an action aimed at a room or a device
reaches.

**Setting**:
Everything else that can be set: a `config` entity, passed over by such an
action and reached by name (`entity.py`, `GlowriumSettingEntity`).

**Remembered value**:
What a setting showed before the last restart, shown until the lamp has
reported or been written that setting, and never written from (`entity.py`,
`_restored`).

**Scripted lamp**:
The test double at the dial: hands out a link, says a frame, answers the state
request, can be read, fails or holds a write, a read, a subscription or a
hang-up, is slow to be found, loses the link, and is silent once hung up -
each only as far as a test asks of it; the tests run the real link over it
(`tests/lamp.py`, `ScriptedLamp`). `link_of` is the tests' one door to the
link a coordinator holds; `in_range` puts the lamp where the integration's
own dial finds it, for a coordinator the setup made.
