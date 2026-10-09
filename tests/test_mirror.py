"""Tests of the state mirror, on a mirror built alone.

What the lamp says comes in as frames; what the mirror makes of a frame is
read off its return value and off the mapping. No coordinator, no link, no
Home Assistant: a frame bug is found here by its bytes.
"""

import asyncio
from datetime import UTC, datetime, timedelta
import logging
import random

import pytest

from custom_components.glowrium import cbor, mirror as mirror_module
from custom_components.glowrium.const import (
    KEY_BRIGHTNESS,
    KEY_LATITUDE,
    KEY_LONGITUDE,
    KEY_POWER,
    KEY_TIME,
)
from custom_components.glowrium.mirror import Mirror, _for_the_log

_ADDRESS = "AA:BB:CC:DD:EE:FF"
_DESCRIBED = "model Glowrium-C051, firmware 4"
_LOG = mirror_module._LOGGER.name


def _a_mirror(
    described: str = _DESCRIBED,
    moments: list[datetime] | None = None,
    known: frozenset[int] = frozenset(),
) -> Mirror:
    """Return a mirror of a lamp whose clock, when asked, reads from ``moments``.

    ``known`` is what the integration has a name for; left out, the mirror
    knows no id, and every id a frame brings is one nobody named.
    """
    clock = list(moments or [datetime(2026, 10, 9, 12, 0, tzinfo=UTC)])
    return Mirror(
        _ADDRESS,
        known=known,
        described=lambda: described,
        now=lambda: clock.pop(0) if len(clock) > 1 else clock[0],
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# --- a report, an echo, and what was reported since ---------------------------


def test_a_report_is_counted_and_an_echo_is_not() -> None:
    """A frame that carries properties is a report; a write's echo is not.

    Both get into the mapping. Only the report moves the number: the lamp
    reports what changed of its own accord, and that is what vouches for a
    write whose acknowledgement went missing - the echo is our word.
    """
    mirror = _a_mirror()
    assert mirror.reports == 0
    assert mirror.take(cbor.encode({KEY_POWER: True})) == frozenset({KEY_POWER})
    assert mirror.reports == 1
    mirror.echo({KEY_BRIGHTNESS: 70})
    assert mirror.reports == 1
    assert dict(mirror) == {KEY_POWER: True, KEY_BRIGHTNESS: 70}


def test_what_was_reported_since_a_mark() -> None:
    """The ids the reports numbered above a mark carried, and nothing else."""
    mirror = _a_mirror()
    mirror.take(cbor.encode({KEY_POWER: True, KEY_BRIGHTNESS: 10}))
    mark = mirror.reports
    assert mirror.reported_since(mark) == frozenset()
    mirror.take(cbor.encode({KEY_BRIGHTNESS: 20}))
    mirror.echo({KEY_TIME: b"\x07\xea\x0a\x09\x0c\x00\x00"})  # an echo adds nothing
    mirror.take(cbor.encode({KEY_POWER: False}))
    assert mirror.reported_since(mark) == frozenset({KEY_POWER, KEY_BRIGHTNESS})
    assert mirror.reported_since(0) == frozenset({KEY_POWER, KEY_BRIGHTNESS})
    assert mirror.reported_since(mirror.reports) == frozenset()
    # An id reported before the mark and not since is not "since".
    mark = mirror.reports
    mirror.take(cbor.encode({KEY_BRIGHTNESS: 30}))
    assert mirror.reported_since(mark) == frozenset({KEY_BRIGHTNESS})


async def test_next_report_wakes_on_a_report_and_on_nothing_else() -> None:
    """Waiting for the next report ends with a report: not an echo, not noise.

    A waiter that gives up - its deadline is its own - is forgotten, so a
    mirror that outlives a thousand commands holds no thousand futures.
    """
    mirror = _a_mirror()
    waiting = asyncio.create_task(mirror.next_report())
    await asyncio.sleep(0)
    mirror.echo({KEY_POWER: True})
    mirror.take(b"\xc0\x00")  # undecodable: no report
    mirror.take(b"\xa0")  # a map with nothing in it: no report
    await asyncio.sleep(0)
    assert not waiting.done()
    mirror.take(cbor.encode({KEY_POWER: False}))
    await asyncio.sleep(0)
    assert waiting.done()
    await waiting

    # A report nobody waited for wakes nobody later: the wait is for the NEXT.
    later = asyncio.create_task(mirror.next_report())
    await asyncio.sleep(0)
    assert not later.done()
    later.cancel()
    with pytest.raises(asyncio.CancelledError):
        await later
    assert mirror._waiters == []


def test_the_clock_is_dated_by_a_report_and_by_an_echo() -> None:
    """The lamp's clock is dated by the host's whenever it comes in, either way.

    The mirror is not emptied when a link drops, so the clock in it can be
    hours old by the time somebody asks how far off it is: what matters is
    the moment it came, not the moment it is read.
    """
    first = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    moments = [first, first + timedelta(hours=1), first + timedelta(hours=2)]
    mirror = _a_mirror(moments=moments)
    clock = b"\x07\xea\x0a\x09\x0c\x00\x00"
    assert mirror.clock_heard_at is None
    mirror.take(cbor.encode({KEY_POWER: True}))
    assert mirror.clock_heard_at is None  # no clock in it
    mirror.take(cbor.encode({KEY_TIME: clock}))
    assert mirror.clock_heard_at == first
    mirror.echo({KEY_TIME: clock})
    assert mirror.clock_heard_at == first + timedelta(hours=1)
    mirror.take(cbor.encode({KEY_BRIGHTNESS: 5}))
    assert mirror.clock_heard_at == first + timedelta(hours=1)


def test_the_mirror_is_read_only() -> None:
    """A value gets in through a frame or an echo, never by assignment."""
    mirror = _a_mirror()
    mirror.take(cbor.encode({KEY_POWER: True}))
    with pytest.raises(TypeError):
        mirror[KEY_POWER] = False  # type: ignore[index]
    assert not hasattr(mirror, "update")
    assert KEY_POWER in mirror
    assert mirror.get(KEY_BRIGHTNESS) is None
    assert mirror[KEY_POWER] is True
    assert len(mirror) == 1


def test_take_never_raises_whatever_the_bytes() -> None:
    """No frame can end the notification callback in an exception.

    Noise, mutated real frames, and the shapes that once got through the
    decoder as something other than a ValueError: a map keyed by an array,
    five hundred maps each the key of the next.
    """
    rng = random.Random(20261009)
    real = [
        cbor.encode({KEY_POWER: True, KEY_BRIGHTNESS: 70}),
        cbor.encode({KEY_LATITUDE: 12.3456, KEY_LONGITUDE: 65.4321}),
        bytes.fromhex("a406f508184609c0000d00"),
    ]
    frames = [
        bytes(rng.randrange(256) for _ in range(rng.randrange(40))) for _ in range(2000)
    ]
    for _ in range(1000):
        frame = bytearray(rng.choice(real))
        for _ in range(rng.randrange(1, 4)):
            frame[rng.randrange(len(frame))] = rng.randrange(256)
        frames.append(bytes(frame))
    frames += [b"\xa1\x80\x00", b"\xa1" * 499, b"", b"\xc0\x00", b"\x81"]
    mirror = _a_mirror()
    for frame in frames:
        carried = mirror.take(frame)
        assert isinstance(carried, frozenset)
        assert all(isinstance(key, int) for key in carried)


def test_the_warning_describes_the_lamp_as_it_is_known_when_written(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning names the model and the firmware as known at that moment.

    The lamp describes itself only after its first frames, so the mirror is
    handed the describing, not a description.
    """
    said = ["model unknown, firmware unknown"]
    mirror = Mirror(
        _ADDRESS,
        known=frozenset(),
        described=lambda: said[0],
        now=lambda: datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
    )
    said[0] = _DESCRIBED
    with caplog.at_level(logging.WARNING, logger=_LOG):
        mirror.take(bytes.fromhex("a106f5deadbeef"))
    assert f"{_ADDRESS} ({_DESCRIBED}) sent a frame" in caplog.text


# --- the frames: moved from the coordinator's tests, driving take ---------------


def test_trailing_bytes_are_reported_as_themselves(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A frame with trailing bytes is warned about once, not buried in debug.

    Rejecting these is what #5 changed, so on a model whose frames were always
    fully consumed this is the regression that change risks - it has to be
    visible as itself rather than as a generic undecodable frame.
    """
    mirror = _a_mirror()
    frame = bytes.fromhex("a106f5deadbeef")  # {6: True} plus 4 stray bytes

    with caplog.at_level(logging.DEBUG, logger=_LOG):
        assert mirror.take(frame) == frozenset()
        assert not mirror  # the frame is still rejected wholesale
        assert mirror.reports == 0

        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "4 trailing bytes" in warnings[0]
        assert frame.hex() in warnings[0]
        # It asks for the frame to be posted, and a frame can hold the home's
        # coordinates: the request has to say so where it is made.
        assert "coordinates" in warnings[0]
        assert "Undecodable frame" not in caplog.text

        # A second such frame must not warn again - notifications are constant.
        caplog.clear()
        mirror.take(frame)
        assert not _warnings(caplog)
        assert "trailing bytes" in caplog.text  # still recorded, at debug


_WHERE = {KEY_LATITUDE: 12.3456, KEY_LONGITUDE: 65.4321}
_LATITUDE_HEX = cbor.encode(12.3456).hex()[2:]  # the eight bytes after fb
_LONGITUDE_HEX = cbor.encode(65.4321).hex()[2:]
_CURVE = bytes(range(0x40, 0x5C))  # 28 bytes of sunrise and sunset times


@pytest.mark.parametrize(
    ("frame", "shown"),
    [
        pytest.param(
            cbor.encode({KEY_POWER: True} | _WHERE | {KEY_BRIGHTNESS: 70}).hex(),
            "a406f5" + "0afb" + "xx" * 8 + "0bfb" + "xx" * 8 + "081846",
            id="both coordinates, among other things",
        ),
        pytest.param(
            "a2" + "1834581c" + _CURVE.hex() + "06f5",
            "a2" + "1834581c" + "xx" * 28 + "06f5",
            id="the times worked out from them",
        ),
        pytest.param(
            "a1" + "18344c" + _CURVE[:12].hex(),
            "a1" + "18344c" + "xx" * 12,
            id="those times, on a lamp that keeps fewer",
        ),
        pytest.param(
            "a2" + "0afa" + "41458794" + "06f5",
            "a2" + "0afa" + "xx" * 4 + "06f5",
            id="a coordinate kept as a shorter float",
        ),
        pytest.param(
            "a2" + "0bf9" + "5c17" + "06f5",
            "a2" + "0bf9" + "xx" * 2 + "06f5",
            id="a coordinate kept as the shortest float there is",
        ),
        pytest.param(
            "a2" + "1834590100" + "5a" * 256 + "06f5",
            "a2" + "1834590100" + "xx" * 256 + "06f5",
            id="times that take two bytes to say how long they are",
        ),
        pytest.param(
            "a206f5" + "0bfb" + _LONGITUDE_HEX[:6],
            "a206f5" + "0bfb" + "xx" * 3,
            id="a coordinate the frame ends inside",
        ),
        pytest.param(
            "a2" + "1834581c" + _CURVE[:5].hex(),
            "a2" + "1834581c" + "xx" * 5,
            id="times the frame ends inside",
        ),
        pytest.param(
            "c0" + "0afb" + _LATITUDE_HEX + "ff",
            "c0" + "0afb" + "xx" * 8 + "ff",
            id="behind something that cannot be read",
        ),
        pytest.param(
            # 18 34 41 reads as the times, one byte long - and that byte is the
            # id of the latitude that follows.
            "183441" + "0afb" + _LATITUDE_HEX,
            "183441" + "xx" + "fb" + "xx" * 8,
            id="an id swallowed by something that only looked private",
        ),
        pytest.param(
            # 0a fb, and six bytes later the real longitude: taking eight bytes
            # for a latitude takes the longitude's id with them.
            "0afb" + "00" * 6 + "0bfb" + _LONGITUDE_HEX,
            "0afb" + "xx" * 8 + "xx" * 8,
            id="an id inside what was taken for another value",
        ),
        pytest.param(
            "1834" + "57" + "00" * 13 + "0afb" + _LATITUDE_HEX,
            "1834" + "57" + "xx" * 23,
            id="a coordinate inside what was taken for the times",
        ),
        pytest.param("a206f5081846", "a206f5081846", id="nothing of the kind"),
        pytest.param("", "", id="nothing at all"),
    ],
)
def test_a_frame_goes_into_the_log_without_what_says_where_the_lamp_is(
    frame: str, shown: str
) -> None:
    """A frame is logged so that it can be posted, and a frame can say where.

    The lamp stores the coordinates it was given and works the times of
    sunrise and sunset out from them; either gives the place away. A frame
    that is being logged is one that could not be read to its end, so they
    are found by their bytes wherever they stand - an id and the head of its
    value - and the value is put down as xx. Everything else stays, byte for
    byte, at the length it had: that is what makes the dump worth posting.

    Every offset is looked at, whatever was found before it. A search that
    skipped past each value it found would skip the id of the next one
    whenever a find was a false one - and print that value whole.
    """
    assert _for_the_log(bytes.fromhex(frame)) == shown
    assert len(shown) == len(frame)


def test_wherever_it_stands_in_whatever_noise_a_coordinate_is_blanked() -> None:
    """No bytes around a coordinate, or ahead of it, keep it from being found.

    The bytes that surround it are noise leaning towards the ones the search
    looks for, so that false finds come up all the time - before the
    coordinate, across its id, inside it. What stays in the dump is the
    frame's own bytes, in place.
    """
    rng = random.Random(20261005)
    lures = bytes.fromhex("0a0bfbfaf9183440414c575859")
    private = [
        bytes.fromhex("0afb" + _LATITUDE_HEX),
        bytes.fromhex("0bfb" + _LONGITUDE_HEX),
        bytes.fromhex("1834581c") + _CURVE,
    ]

    def noise(most: int) -> bytes:
        return bytes(
            rng.choice(lures) if rng.random() < 0.6 else rng.randrange(256)
            for _ in range(rng.randrange(most))
        )

    for _ in range(3000):
        value = rng.choice(private)
        head = 4 if value[0] == 0x18 else 2
        before = noise(40)
        frame = before + value + noise(40)

        shown = _for_the_log(frame)

        assert len(shown) == 2 * len(frame)
        start = 2 * (len(before) + head)
        assert shown[start : 2 * (len(before) + len(value))] == "xx" * (
            len(value) - head
        ), frame.hex()
        assert all(
            shown[2 * i : 2 * i + 2] in ("xx", f"{byte:02x}")
            for i, byte in enumerate(frame)
        )


@pytest.mark.parametrize(
    ("frame", "said"),
    [
        pytest.param(
            cbor.encode(_WHERE).hex() + "deadbeef", "trailing bytes", id="trailing"
        ),
        pytest.param(
            "a4" + cbor.encode(_WHERE).hex()[2:] + "09c000" + "0afb" + _LATITUDE_HEX,
            "cannot read",
            id="an item that cannot be read, with the place on both sides of it",
        ),
        pytest.param(
            "82" + cbor.encode(_WHERE).hex() + "c0",
            "Undecodable frame",
            id="undecodable",
        ),
        pytest.param(
            "81" + cbor.encode(_WHERE).hex(),
            "nothing that can be used",
            id="decoded, and not a map of properties",
        ),
    ],
)
def test_no_line_of_the_mirror_carries_the_coordinates(
    caplog: pytest.LogCaptureFixture, frame: str, said: str
) -> None:
    """Every line that prints a frame prints it blanked, the second time too.

    Two of them are warnings, written without debug logging and with a
    request to post the frame. The request cannot rest on the reader blanking
    hex by hand.
    """
    mirror = _a_mirror()
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        mirror.take(bytes.fromhex(frame))
        mirror.take(bytes.fromhex(frame))

    assert said in caplog.text
    assert "xx" * 8 in caplog.text
    assert _LATITUDE_HEX not in caplog.text
    assert _LONGITUDE_HEX not in caplog.text


def test_malformed_frame_is_not_reported_as_trailing_bytes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A truncated frame keeps the generic message and raises no warning."""
    mirror = _a_mirror()
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        assert mirror.take(bytes.fromhex("81")) == frozenset()
    assert "Undecodable frame" in caplog.text
    assert not _warnings(caplog)


@pytest.mark.parametrize(
    ("frame", "said"),
    [
        pytest.param("a18000", "cannot read", id="keyed by an array"),
        pytest.param("ada200", "cannot read", id="keyed by a map that ran out"),
        pytest.param(
            "a1" * 499, "cannot read", id="maps as keys, as deep as a frame allows"
        ),
        pytest.param("c000", "Undecodable frame", id="not a map at all"),
        pytest.param("", "Undecodable frame", id="empty"),
    ],
)
def test_a_frame_the_decoder_refuses_is_dropped_and_nothing_is_raised(
    caplog: pytest.LogCaptureFixture, frame: str, said: str
) -> None:
    """No frame can end the notification callback in an exception.

    The callback runs inside the Bluetooth stack's own message handler. A map
    keyed by an array used to leave it as a TypeError, and five hundred maps
    each the key of the next as a RecursionError - a traceback per frame on a
    local adapter, and on the path that reads the state, an exception that
    took the whole connect with it. Nothing of these frames could be read,
    so nothing is merged; each is named in the log.
    """
    mirror = _a_mirror()
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        assert mirror.take(bytes.fromhex(frame)) == frozenset()
    assert not mirror
    assert mirror.reports == 0
    assert said in caplog.text
    assert "nothing that can be used" not in caplog.text  # said once is enough


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param("80", id="an empty array"),
        pytest.param("8206f5", id="an array"),
        pytest.param("a0", id="a map with nothing in it"),
        pytest.param("05", id="a number"),
        pytest.param("f6", id="null"),
    ],
)
def test_a_frame_that_decodes_to_nothing_usable_is_named_as_well(
    caplog: pytest.LogCaptureFixture, frame: str
) -> None:
    """A frame can decode without a fault and still be of no use.

    The decoder reads CBOR; what the lamp reports is a map of properties. A
    frame that is anything else, or a map with nothing in it, was dropped
    without a line - while the documents tell whoever reports a problem that
    every frame the integration could not use is in the debug log.
    """
    mirror = _a_mirror()
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        assert mirror.take(bytes.fromhex(frame)) == frozenset()
    assert not mirror
    assert mirror.reports == 0
    assert f"frame {frame} decodes to nothing that can be used" in caplog.text
    assert not _warnings(caplog)


# {power: on, brightness: 70, 0x09: <a tag, which nothing here can read> ...
_PARTLY_READABLE = bytes.fromhex("a406f508184609c0000d00")


def test_what_was_read_ahead_of_an_unreadable_item_is_kept_and_said_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One item the integration cannot read does not cost the frame around it.

    The properties ahead of it were read as from any other frame, and they
    are kept. That the rest was not is said once at a level somebody sees,
    with the bytes it takes to add the missing reading and the caution that
    goes with posting bytes - and at debug from then on, since a lamp that
    sends one such frame sends them all day.
    """
    mirror = _a_mirror()

    with caplog.at_level(logging.DEBUG, logger=_LOG):
        carried = mirror.take(_PARTLY_READABLE)

        assert carried == frozenset({KEY_POWER, KEY_BRIGHTNESS})
        assert dict(mirror) == {KEY_POWER: True, KEY_BRIGHTNESS: 70}
        assert mirror.reports == 1
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        said = warnings[0]
        assert "cannot read" in said
        assert "unsupported CBOR major type 6" in said
        assert "2 properties ahead of it were kept" in said
        assert _PARTLY_READABLE.hex() in said
        assert "coordinates" in said
        assert "Undecodable frame" not in caplog.text
        assert "split across frames" not in caplog.text  # it was not: it is whole

        caplog.clear()
        mirror.take(_PARTLY_READABLE)
        assert not _warnings(caplog)
        assert "cannot be read" in caplog.text  # still recorded, at debug


def test_a_map_split_across_frames_is_kept_as_far_as_it_came(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A frame that promises more pairs than it carries is a report of what it has.

    A G8 sends its map in parts, each headed with the whole map's count; the
    pairs that arrived are taken, and the shortfall is named at debug.
    """
    mirror = _a_mirror()
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        carried = mirror.take(bytes.fromhex("a306f5081846"))  # promises 3, has 2
    assert carried == frozenset({KEY_POWER, KEY_BRIGHTNESS})
    assert mirror[KEY_POWER] is True
    assert mirror[KEY_BRIGHTNESS] == 70
    assert mirror.reports == 1
    assert "split across frames; kept 2 of them" in caplog.text


# --- how much of what a lamp sends is kept ------------------------------------


def _ids_never_sent(first: int, count: int) -> bytes:
    """Return a frame that reports ``count`` ids from ``first`` on, each as True."""
    return cbor.encode(dict.fromkeys(range(first, first + count), True))


def test_no_more_than_the_limit_of_ids_nobody_named_is_kept() -> None:
    """A device that keeps sending new ids does not grow the mirror without end.

    What the integration has no name for is kept all the same - a first
    report of a new model has to show that it is there - but only so much of
    it: the first ids to come, and no more. The rest is counted. The protocol
    has no pairing, so whatever answers at the lamp's address fills the
    mirror; measured, a device sending new ids in every frame took a host's
    free memory in hours (2026-10-09).
    """
    mirror = _a_mirror()
    for first in range(1000, 1200, 50):  # two hundred ids, fifty a frame
        mirror.take(_ids_never_sent(first, 50))

    assert len(mirror) == 64
    assert set(mirror) == set(range(1000, 1064))  # the first to come
    assert mirror.not_kept == 136
    # What is kept stays kept, and goes on being heard.
    assert mirror.take(cbor.encode({1000: False})) == frozenset({1000})
    assert mirror[1000] is False
    assert len(mirror) == 64


def test_what_the_integration_knows_and_what_it_wrote_is_kept_whatever_else_came() -> (
    None
):
    """The room for ids nobody named is not taken from the ones somebody did.

    A lamp is asked for its power and its brightness whether or not something
    filled the mirror first, and what a command set is the integration's own
    word: neither waits for room, and neither takes any. An id that was
    echoed and is reported later is heard as well.
    """
    mirror = _a_mirror(known=frozenset({KEY_POWER, KEY_BRIGHTNESS}))
    mirror.take(cbor.encode({KEY_BRIGHTNESS: 70}))  # a known id takes no room
    mirror.take(_ids_never_sent(1000, 100))  # more than there is room for
    assert len(mirror) == 1 + 64

    taken = mirror.take(cbor.encode({KEY_POWER: True, 2000: True}))
    mirror.echo({3000: 1})
    heard = mirror.take(cbor.encode({3000: 2}))

    assert taken == frozenset({KEY_POWER})
    assert heard == frozenset({3000})
    assert mirror[KEY_POWER] is True
    assert mirror[3000] == 2
    assert 2000 not in mirror
    assert len(mirror) == 1 + 64 + 2


async def test_what_was_not_kept_was_not_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An id there was no room for is in nothing the mirror says of a report.

    Not in what a frame carried and not in what was reported since a mark:
    priming and vouching compare those with the ids they wait for, and an id
    that is not in the mirror was not reported into it. A frame of which
    nothing was kept is no report at all - it moves no number and wakes
    nobody - and is named in the debug log, like every frame that is dropped.
    """
    mirror = _a_mirror(known=frozenset({KEY_POWER}))
    mirror.take(_ids_never_sent(1000, 64))  # all the room there is, taken
    mark = mirror.reports
    waiting = asyncio.create_task(mirror.next_report())
    await asyncio.sleep(0)

    frame = _ids_never_sent(2000, 3)
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        nothing = mirror.take(frame)
    await asyncio.sleep(0)

    assert nothing == frozenset()
    assert mirror.reports == mark
    assert mirror.reported_since(mark) == frozenset()
    assert not waiting.done()
    assert mirror.not_kept == 3
    assert (
        f"frame {frame.hex()} carries 3 properties and none of them is kept"
        in caplog.text
    )

    some = mirror.take(cbor.encode({KEY_POWER: True, 2000: True}))
    await asyncio.sleep(0)

    assert some == frozenset({KEY_POWER})
    assert mirror.reports == mark + 1
    assert mirror.reported_since(mark) == frozenset({KEY_POWER})
    assert waiting.done()
    await waiting


def test_the_first_property_not_kept_is_said_once_and_without_the_frame(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """That a lamp reports more than is kept is worth one line somebody sees.

    A lamp that does it is a model nobody has met, or not a lamp: either is
    worth a report, and the line asks for one, naming the model and the
    firmware as the other warnings do. It carries nothing of the frame - the
    ids and the values are the device's to choose - and it is said once: a
    device that does this does it in every frame.
    """
    mirror = _a_mirror()
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        mirror.take(_ids_never_sent(1000, 64))
        assert not _warnings(caplog)  # room for all of it, and nothing to say

        frame = _ids_never_sent(2000, 2)
        mirror.take(frame)
        (said,) = _warnings(caplog)
        assert f"{_ADDRESS} ({_DESCRIBED}) reports more properties than" in said
        assert "64 others" in said
        assert "report this model" in said
        assert frame.hex() not in said
        assert "07d0" not in said  # nor an id out of it

        caplog.clear()
        mirror.take(_ids_never_sent(3000, 2))
        assert not _warnings(caplog)
    assert mirror.not_kept == 4
