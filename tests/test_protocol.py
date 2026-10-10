"""Tests for the semantic protocol codec (byte layouts <-> values)."""

from collections.abc import Callable, Mapping
import datetime
from typing import Any

import pytest

from custom_components.glowrium import cbor, protocol
from custom_components.glowrium.const import (
    DST_OFF,
    DST_ON,
    KEY_ACTIVATED,
    KEY_BRIGHTNESS,
    KEY_CIRCADIAN,
    KEY_DST,
    KEY_INDICATOR,
    KEY_LATITUDE,
    KEY_LIGHTING_MODE,
    KEY_LONGITUDE,
    KEY_POWER,
    KEY_RAMP,
    KEY_SCHEDULE,
    KEY_TIME,
    KEY_TIME_SYNCED,
    KEY_TIMER,
    MODE_CIRCADIAN,
    MODE_MANUAL,
    MODE_SCHEDULE,
    TIMER_SLOT_LENGTH,
)

# A schedule slot: enabled, 06:00 to 18:00, 100 %, no fade.
_A_SLOT = bytes.fromhex("0100000006001200640000")


def test_be2_minutes_roundtrip() -> None:
    """Minutes encode to 2-byte big-endian seconds and clamp to the field."""
    assert protocol.be2_minutes_to_bytes(30) == bytes.fromhex("0708")  # 1800 s
    assert protocol.be2_minutes_to_bytes(0) == bytes.fromhex("0000")
    assert protocol.be2_minutes_to_bytes(-5) == bytes.fromhex("0000")  # clamped up
    assert protocol.be2_minutes_to_bytes(10_000) == (1092 * 60).to_bytes(2, "big")


def test_ramp_minutes() -> None:
    """The 0x2f ramp decodes to minutes; an empty one was reported, and is none."""
    assert protocol.ramp_minutes({KEY_RAMP: bytes.fromhex("0708")}) == 30
    assert protocol.ramp_minutes({}) is None
    assert protocol.ramp_minutes({KEY_RAMP: b""}) is protocol.UNUSABLE


def test_timer_slot_guards() -> None:
    """timer_slot returns bytes only for a slot of the length a slot has."""
    assert protocol.timer_slot({}) is None
    assert protocol.timer_slot({KEY_TIMER: b"\x00\x00"}) is protocol.UNUSABLE
    assert len(_A_SLOT) == TIMER_SLOT_LENGTH
    assert protocol.timer_slot({KEY_TIMER: _A_SLOT[:-1]}) is protocol.UNUSABLE
    assert protocol.timer_slot({KEY_TIMER: _A_SLOT}) == _A_SLOT


def test_editable_timer_slot_is_an_independent_copy() -> None:
    """editable_timer_slot yields a mutable copy, or None when never read.

    It deliberately does not fall back to a default: the slot is written as
    one unit, so defaulting to change a single field overwrites the rest.
    """
    assert protocol.editable_timer_slot({}) is None
    state = {KEY_TIMER: bytearray(_A_SLOT)}
    slot = protocol.editable_timer_slot(state)
    assert slot == bytearray(_A_SLOT)
    slot[4] = 7  # mutating the copy must not touch what the lamp reported
    assert state[KEY_TIMER] == _A_SLOT


def test_schedule_fields_decode() -> None:
    """Start/end/brightness/gradual decode from their 0x11 slot offsets."""
    slot = bytearray(_A_SLOT)
    slot[4], slot[5] = 7, 30  # start 07:30
    slot[6], slot[7] = 19, 45  # end 19:45
    slot[8] = 80  # brightness
    slot[9:11] = (300).to_bytes(2, "big")  # 5 min gradual
    state = {KEY_TIMER: bytes(slot)}
    assert protocol.schedule_start(state) == datetime.time(7, 30)
    assert protocol.schedule_end(state) == datetime.time(19, 45)
    assert protocol.schedule_brightness(state) == 80
    assert protocol.schedule_gradual_minutes(state) == 5


def test_schedule_fields_none_when_absent() -> None:
    """All schedule accessors return None when the slot is missing."""
    assert protocol.schedule_start({}) is None
    assert protocol.schedule_end({}) is None
    assert protocol.schedule_brightness({}) is None
    assert protocol.schedule_gradual_minutes({}) is None


def test_schedule_time_tolerates_a_malformed_slot() -> None:
    """A malformed hour/minute cannot be used, and raises nothing."""
    slot = bytearray(_A_SLOT)
    slot[4] = 25  # invalid hour
    assert protocol.schedule_start({KEY_TIMER: bytes(slot)}) is protocol.UNUSABLE


# A slot unlike _A_SLOT in every byte a setter could touch or spare: a setter
# that rebuilt the slot from a fixed one, or wrote one field too many, shows
# in it.
_SLOT = bytes.fromhex("01aabbcc0615122d40012c")


def test_each_schedule_setter_changes_its_field_and_nothing_else() -> None:
    """The slot is written whole, so a setter has to carry the other fields over."""
    state = {KEY_TIMER: _SLOT}

    assert protocol.with_schedule_start(state, 7, 30) == bytes.fromhex(
        "01aabbcc071e122d40012c"
    )
    assert protocol.with_schedule_end(state, 19, 45) == bytes.fromhex(
        "01aabbcc0615132d40012c"
    )
    assert protocol.with_schedule_brightness(state, 37) == bytes.fromhex(
        "01aabbcc0615122d25012c"
    )
    assert protocol.with_schedule_gradual(state, 15) == bytes.fromhex(
        "01aabbcc0615122d400384"
    )
    assert state[KEY_TIMER] == _SLOT  # what the lamp reported is not written on


def test_schedule_brightness_is_kept_within_a_percentage() -> None:
    """The field is one byte; what goes into it is 0..100."""
    state = {KEY_TIMER: _SLOT}
    assert protocol.with_schedule_brightness(state, 150) == bytes.fromhex(
        "01aabbcc0615122d64012c"
    )
    assert protocol.with_schedule_brightness(state, -3) == bytes.fromhex(
        "01aabbcc0615122d00012c"
    )


def test_no_schedule_setter_invents_a_slot_that_was_never_read() -> None:
    """A default would overwrite four fields the user never chose; refuse instead."""
    assert protocol.with_schedule_start({}, 7, 30) is None
    assert protocol.with_schedule_end({}, 19, 45) is None
    assert protocol.with_schedule_brightness({}, 37) is None
    # Nor one from a slot that was reported and is no slot.
    short = {KEY_TIMER: b"\x01\x00"}
    assert protocol.with_schedule_gradual(short, 15) is protocol.UNUSABLE


def test_dst_is_read_from_the_flag_byte() -> None:
    """The 0x35 slot is a flag and an offset; only the flag says on or off."""
    assert protocol.dst_enabled({KEY_DST: DST_ON}) is True
    assert protocol.dst_enabled({KEY_DST: DST_OFF}) is False
    assert protocol.dst_enabled({}) is None
    assert protocol.dst_enabled({KEY_DST: b""}) is protocol.UNUSABLE
    assert protocol.dst_enabled({KEY_DST: "on"}) is protocol.UNUSABLE
    # Only 1 is on and only 0 is off: anything else in the flag byte is not a
    # lamp saying either.
    unknown_flag = {KEY_DST: bytes.fromhex("0200000e10")}
    assert protocol.dst_enabled(unknown_flag) is protocol.UNUSABLE


def test_setting_dst_keeps_the_offset_the_lamp_reported() -> None:
    """A fixed hour would turn a half-hour region into a full one at a touch."""
    half_hour = bytes.fromhex("0000000708")  # off, 1800 s
    assert protocol.with_dst({KEY_DST: half_hour}, True) == bytes.fromhex("0100000708")
    assert protocol.with_dst({KEY_DST: DST_ON}, False) == DST_OFF


def test_setting_dst_on_a_lamp_that_has_not_reported_uses_the_hour() -> None:
    """One field with a near-universal default: the switch works before priming."""
    assert protocol.with_dst({}, True) == DST_ON
    assert protocol.with_dst({}, False) == DST_OFF
    assert protocol.with_dst({KEY_DST: b"\x01"}, False) == DST_OFF  # not a whole slot


def test_the_device_clock_is_year_month_day_hour_minute_second() -> None:
    """Local wall-clock time: a two-byte year, then five bytes."""
    stamp = datetime.datetime(2026, 7, 18, 21, 24, 35)
    assert protocol.encode_device_time(stamp).hex() == "07ea0712151823"
    assert protocol.device_time({KEY_TIME: bytes.fromhex("07ea0712151823")}) == stamp


def test_the_clock_is_set_together_with_the_flag_that_says_so() -> None:
    """One write carries the time and 0x31, as the vendor app sends them."""
    stamp = datetime.datetime(2026, 7, 18, 21, 24, 35)
    assert protocol.clock_command(stamp) == {
        KEY_TIME: bytes.fromhex("07ea0712151823"),
        KEY_TIME_SYNCED: 1,
    }


def test_a_clock_the_lamp_has_not_reported_is_not_a_clock() -> None:
    """Absent is not reported; cut short, or text, is reported and no clock."""
    assert protocol.device_time({}) is None
    cut_short = {KEY_TIME: bytes.fromhex("07ea0712")}
    assert protocol.device_time(cut_short) is protocol.UNUSABLE
    assert protocol.device_time({KEY_TIME: "07ea0712151823"}) is protocol.UNUSABLE


def test_a_clock_that_is_no_date_says_so() -> None:
    """Thirteen months is a report, and a wrong one: the caller corrects it.

    It cannot be used, as a clock cut short cannot. What tells the two
    apart is asked apart: seven bytes are a clock, whatever it shows.
    """
    thirteen_months = {KEY_TIME: bytes.fromhex("07ea0d12151823")}
    assert protocol.device_time(thirteen_months) is protocol.UNUSABLE
    assert protocol.clock_is_no_date(thirteen_months) is True


def test_a_brightness_is_a_number_from_zero_to_a_hundred() -> None:
    """Whole or not - and nothing else is a level at all."""
    for level in (0, 70, 100, 12.5, 70.0):
        assert protocol.brightness_percent({KEY_BRIGHTNESS: level}) == level
    not_a_level = (
        float("inf"),
        float("-inf"),
        float("nan"),
        100.5,
        101,
        -1,
        True,
        False,
        "70",
        b"\x46",
        [70],
        None,
    )
    for odd in not_a_level:
        assert protocol.brightness_percent({KEY_BRIGHTNESS: odd}) is protocol.UNUSABLE
    assert protocol.brightness_percent({}) is None


# --- One reader for each property: a value, nothing reported, or unusable ------

UNUSABLE = protocol.UNUSABLE

# Every kind of value the decoder can hand over under an id: numbers of every
# size and sign, the floats that are no numbers, both booleans, null, text,
# byte strings of every length from none to forty, arrays and maps, nested and
# holding null.
_KINDS: tuple[Any, ...] = (
    0,
    1,
    5,
    70,
    100,
    101,
    255,
    256,
    -1,
    2**64 - 1,
    -(2**63),
    0.0,
    1.0,
    12.5,
    70.0,
    100.5,
    -0.5,
    float("inf"),
    float("-inf"),
    float("nan"),
    True,
    False,
    None,
    "",
    "on",
    "70",
    *(bytes(length) for length in range(41)),
    *(b"\xff" * length for length in range(1, 41)),
    [],
    [1, 2],
    [None],
    [[None]],
    {},
    {1: 2},
    {1: None},
    {1: [None]},
)

_Reader = Callable[[Mapping[int, Any]], Any]
_FLAGS: dict[_Reader, int] = {
    protocol.power: KEY_POWER,
    protocol.circadian_mode: KEY_CIRCADIAN,
    protocol.schedule_mode: KEY_SCHEDULE,
    protocol.activated: KEY_ACTIVATED,
    protocol.indicator: KEY_INDICATOR,
}
_READERS: dict[_Reader, int] = {
    **_FLAGS,
    protocol.brightness_percent: KEY_BRIGHTNESS,
    protocol.lighting_mode: KEY_LIGHTING_MODE,
    protocol.latitude: KEY_LATITUDE,
    protocol.longitude: KEY_LONGITUDE,
    protocol.ramp_seconds: KEY_RAMP,
    protocol.ramp_minutes: KEY_RAMP,
    protocol.reported_ramp: KEY_RAMP,
    protocol.daylight_saving: KEY_DST,
    protocol.dst_enabled: KEY_DST,
    protocol.schedule: KEY_TIMER,
    protocol.timer_slot: KEY_TIMER,
    protocol.schedule_start: KEY_TIMER,
    protocol.schedule_end: KEY_TIMER,
    protocol.schedule_brightness: KEY_TIMER,
    protocol.schedule_gradual_minutes: KEY_TIMER,
    protocol.device_time: KEY_TIME,
}


def _named(read: _Reader) -> str:
    return read.__name__


def test_what_cannot_be_used_is_neither_true_nor_false() -> None:
    """The answer for a value that cannot be used cannot be taken for a value.

    It is an object, and an object is true: a caller that left the check out
    would read it as "on". This one refuses to be asked.
    """
    with pytest.raises(TypeError):
        bool(UNUSABLE)


@pytest.mark.parametrize("read", _READERS, ids=_named)
def test_a_reader_has_one_of_three_answers_whatever_is_under_its_id(
    read: _Reader,
) -> None:
    """Nothing was reported, a value, or that what is there cannot be used.

    Never the first for something that was reported - a null is a report -
    and never an exception: whatever a frame carried, a reader has an answer.
    """
    key = _READERS[read]

    assert read({}) is None
    for value in _KINDS:
        assert read({key: value}) is not None, repr(value)


@pytest.mark.parametrize("read", _FLAGS, ids=_named)
def test_a_flag_is_a_boolean_and_nothing_that_only_reads_as_one(
    read: _Reader,
) -> None:
    """True and False, and no other thing a lamp could put there.

    Text used to read as on, and so did one zero byte and the number one:
    each was taken by its truthiness, and none of them is a lamp saying yes.
    """
    key = _FLAGS[read]

    assert read({key: True}) is True
    assert read({key: False}) is False
    for odd in (1, 0, 1.0, "on", "", b"\x01", b"\x00", b"", [True], {}, None):
        assert read({key: odd}) is UNUSABLE, repr(odd)


def test_a_lighting_mode_is_a_whole_number_that_fits_one_byte() -> None:
    """An index from 0 to 255; which preset it is, if any, the profile says.

    The presets are found by comparing the index, and ``True`` equals 1: a
    lamp that reported a flag there was shown as the preset with index 1.
    """
    for index in (0, 1, 5, 32, 255):
        read = protocol.lighting_mode({KEY_LIGHTING_MODE: index})
        assert read == index
        assert type(read) is int
    for odd in (True, False, -1, 256, 5.0, 1.5, "5", b"\x05", [5], None):
        assert protocol.lighting_mode({KEY_LIGHTING_MODE: odd}) is UNUSABLE, repr(odd)
    assert protocol.lighting_mode({}) is None


@pytest.mark.parametrize(
    ("read", "key", "limit"),
    [
        pytest.param(protocol.latitude, KEY_LATITUDE, 90, id="latitude"),
        pytest.param(protocol.longitude, KEY_LONGITUDE, 180, id="longitude"),
    ],
)
def test_a_coordinate_is_a_finite_float_within_its_range(
    read: _Reader, key: int, limit: int
) -> None:
    """Degrees, and no further than the globe goes.

    The sensors showed ``inf``, and 500 degrees: a float was all they asked
    for.
    """
    for degrees in (0.0, 12.3456, -12.3456, float(limit), float(-limit)):
        assert read({key: degrees}) == degrees
    not_a_place = (
        float("inf"),
        float("-inf"),
        float("nan"),
        limit + 0.5,
        -limit - 0.5,
        500.0,
        41,
        True,
        "41.3",
        bytes(8),
        [41.3],
        None,
    )
    for odd in not_a_place:
        assert read({key: odd}) is UNUSABLE, repr(odd)
    assert read({}) is None


def test_a_ramp_is_two_bytes_of_seconds_and_no_longer_than_two_hours() -> None:
    """Seconds in two bytes, big-endian; the longest ramp there is is two hours.

    Any byte string long enough to index used to do: five bytes of ``ff``
    read as 1092 minutes, on a number that goes to 90.
    """
    half_an_hour = {KEY_RAMP: bytes.fromhex("0708")}
    assert protocol.ramp_seconds(half_an_hour) == 1800
    assert protocol.ramp_minutes(half_an_hour) == 30
    assert protocol.ramp_seconds({KEY_RAMP: bytes.fromhex("0000")}) == 0
    assert protocol.ramp_seconds({KEY_RAMP: bytes.fromhex("1c20")}) == 7200
    # What a command sends back is the two bytes, and only when they are a ramp.
    as_reported = protocol.reported_ramp({KEY_RAMP: bytearray.fromhex("0e10")})
    assert as_reported == bytes.fromhex("0e10")
    assert type(as_reported) is bytes
    not_a_ramp = (
        b"",
        b"\x07",
        bytes.fromhex("1c21"),  # a second over two hours
        bytes.fromhex("ffff"),
        bytes.fromhex("0e1000"),  # in place, and one byte more
        b"\xff" * 5,
        1800,
        "0708",
        [7, 8],
        None,
    )
    for read in (protocol.ramp_seconds, protocol.ramp_minutes, protocol.reported_ramp):
        for odd in not_a_ramp:
            assert read({KEY_RAMP: odd}) is UNUSABLE, repr(odd)
        assert read({}) is None


def test_daylight_saving_is_a_flag_byte_and_an_offset_in_five_bytes() -> None:
    """On or off in the first byte, then seconds; no offset is over two hours.

    One byte used to read as on, and a flag byte of 2 as off.
    """
    on = protocol.daylight_saving({KEY_DST: DST_ON})
    assert on == protocol.DaylightSaving(enabled=True, offset_seconds=3600)
    half_hour_off = protocol.daylight_saving({KEY_DST: bytes.fromhex("0000000708")})
    assert half_hour_off == protocol.DaylightSaving(enabled=False, offset_seconds=1800)
    assert protocol.daylight_saving({KEY_DST: bytes.fromhex("0100001c20")}) == (
        protocol.DaylightSaving(enabled=True, offset_seconds=7200)
    )
    not_a_slot = (
        b"",
        b"\x01",
        bytes.fromhex("0200000e10"),  # neither on nor off
        bytes.fromhex("0100000e1000"),  # in place, and one byte more
        bytes.fromhex("0100001c21"),  # a second over two hours
        bytes.fromhex("01ffffffff"),
        True,
        "on",
        [1, 0, 0, 14, 16],
        None,
    )
    for read in (protocol.daylight_saving, protocol.dst_enabled):
        for odd in not_a_slot:
            assert read({KEY_DST: odd}) is UNUSABLE, repr(odd)
        assert read({}) is None


def test_the_switch_writes_the_hour_over_a_slot_that_cannot_be_used() -> None:
    """Only a slot that is one has its offset carried into the write.

    A slot of the right length and the wrong content used to: after five
    bytes of ``ff`` the switch wrote the flag it was asked for and four of
    those bytes for an offset.
    """
    for odd in (b"\xff" * 5, bytes.fromhex("0200000e10"), bytes.fromhex("0100001c21")):
        assert protocol.with_dst({KEY_DST: odd}, True) == DST_ON, odd.hex()
        assert protocol.with_dst({KEY_DST: odd}, False) == DST_OFF, odd.hex()
    assert protocol.with_dst({KEY_DST: [None]}, True) == DST_ON


# The slot in the one frame of a G8 on record: three bytes a G7 leaves at
# zero are set in it, and nobody knows what they say.
_G8_SLOT = bytes.fromhex("010200ff0a121212640000")
_SCHEDULE_READERS = (
    protocol.schedule,
    protocol.timer_slot,
    protocol.schedule_start,
    protocol.schedule_end,
    protocol.schedule_brightness,
    protocol.schedule_gradual_minutes,
)


def test_a_schedule_is_eleven_bytes_and_each_is_what_its_place_means() -> None:
    """A flag, three bytes nobody has decoded, two times of day, a level, a fade.

    Any byte string of eleven or more used to do, read by its first eleven:
    forty bytes of ``ff`` showed a brightness of 255 %, and a slot of twelve
    was edited in place with the twelfth carried into the write.
    """
    assert protocol.schedule({KEY_TIMER: _G8_SLOT}) == protocol.Schedule(
        enabled=True,
        start=datetime.time(10, 18),
        end=datetime.time(18, 18),
        brightness=100,
        fade_seconds=0,
    )
    late = {KEY_TIMER: bytes.fromhex("00000000173b0000001c20")}
    assert protocol.schedule(late) == protocol.Schedule(
        enabled=False,
        start=datetime.time(23, 59),
        end=datetime.time(0, 0),
        brightness=0,
        fade_seconds=7200,
    )
    assert protocol.schedule_gradual_minutes(late) == 120
    as_reported = protocol.timer_slot({KEY_TIMER: bytearray(_G8_SLOT)})
    assert as_reported == _G8_SLOT
    assert type(as_reported) is bytes
    not_a_schedule = (
        _A_SLOT + b"\x00",  # every field in place, and one byte more
        _A_SLOT[:-1],
        bytes.fromhex("0200000006001200640000"),  # neither on nor off
        bytes.fromhex("0100000018001200640000"),  # starts at 24:00
        bytes.fromhex("010000000600123c640000"),  # ends at 18:60
        bytes.fromhex("0100000006001200650000"),  # 101 %
        bytes.fromhex("0100000006001200641c21"),  # a fade a second over two hours
        b"\xff" * 11,
        b"\xff" * 40,
        b"",
        "0100000006001200640000",
        [1, 0, 0, 0, 6, 0, 18, 0, 100, 0, 0],
        None,
    )
    for odd in not_a_schedule:
        state = {KEY_TIMER: odd}
        for read in _SCHEDULE_READERS:
            assert read(state) is UNUSABLE, (read.__name__, odd)
        assert protocol.with_schedule_start(state, 7, 30) is UNUSABLE, odd
        assert protocol.with_schedule_end(state, 19, 45) is UNUSABLE, odd
        assert protocol.with_schedule_brightness(state, 37) is UNUSABLE, odd
        assert protocol.with_schedule_gradual(state, 15) is UNUSABLE, odd


def test_a_clock_is_seven_bytes_that_are_a_date_and_a_time() -> None:
    """A year in two bytes and five more, and nothing after them.

    A clock of more than seven bytes was read by its first seven.
    """
    shown = bytes.fromhex("07ea0712151823")
    assert protocol.device_time({KEY_TIME: shown}) == datetime.datetime(
        2026, 7, 18, 21, 24, 35
    )
    no_date = (
        bytes.fromhex("07ea0d12151823"),  # month 13
        bytes.fromhex("07ea0712181823"),  # hour 24
        bytes.fromhex("00000000000000"),  # the year nought, and no month
    )
    no_clock = (shown + b"\x00", shown[:-1], b"", shown.hex(), [7, 234], 20260718, None)
    for odd in (*no_date, *no_clock):
        assert protocol.device_time({KEY_TIME: odd}) is UNUSABLE, repr(odd)
    # Seven bytes that are no date are a clock that is wrong, which a write
    # puts right. Anything else is a clock kept some other way, or no clock.
    for odd in no_date:
        assert protocol.clock_is_no_date({KEY_TIME: odd}) is True, odd.hex()
    for other in (shown, *no_clock):
        assert protocol.clock_is_no_date({KEY_TIME: other}) is False, repr(other)
    assert protocol.clock_is_no_date({}) is False


_NOT_SAID = object()


@pytest.mark.parametrize(
    ("circadian", "schedule", "mode"),
    [
        (True, False, MODE_CIRCADIAN),
        (False, True, MODE_SCHEDULE),
        (False, False, MODE_MANUAL),
        (True, True, MODE_CIRCADIAN),
        # A mode whose flag is true is the mode, whatever the other says.
        (True, _NOT_SAID, MODE_CIRCADIAN),
        (_NOT_SAID, True, MODE_SCHEDULE),
        (True, "on", MODE_CIRCADIAN),
        (b"\x00", True, MODE_SCHEDULE),
        # Manual takes both flags, and both saying no.
        (_NOT_SAID, _NOT_SAID, None),
        (False, _NOT_SAID, None),
        (_NOT_SAID, False, None),
        # A flag that cannot be used and no flag saying yes: neither can the mode.
        ("on", False, UNUSABLE),
        (False, b"\x00", UNUSABLE),
        (1, _NOT_SAID, UNUSABLE),
        (_NOT_SAID, None, UNUSABLE),
        ("on", [True], UNUSABLE),
    ],
)
def test_the_operating_mode_is_read_from_two_flags(
    circadian: Any, schedule: Any, mode: Any
) -> None:
    """Circadian or Schedule by the flag that says yes; Manual when both say no.

    Text under the Circadian flag used to read as Circadian mode, and one
    zero byte too: each flag was taken by its truthiness.
    """
    state = {
        key: value
        for key, value in ((KEY_CIRCADIAN, circadian), (KEY_SCHEDULE, schedule))
        if value is not _NOT_SAID
    }

    assert protocol.operating_mode(state) is mode


def test_what_a_g8_was_seen_to_report_reads_as_values() -> None:
    """The one frame of a G8 on record: nothing in it is turned away.

    Three bytes of its schedule slot are not a G7's, and its ramp is the
    automatic one. A rule drawn from one model has to take the other's.
    """
    frame = bytes.fromhex(
        "ac06f508184609f40afb4043747ae147ae140bfb40534147ae147ae10df411"
        "4b010200ff0a12121264000014f517f5182b01182f420000"
    )
    reported, _short = cbor.decode_frame(frame)

    assert protocol.power(reported) is True
    assert protocol.brightness_percent(reported) == 70
    assert protocol.operating_mode(reported) == MODE_MANUAL
    assert isinstance(protocol.latitude(reported), float)
    assert isinstance(protocol.longitude(reported), float)
    assert protocol.schedule(reported) == protocol.schedule({KEY_TIMER: _G8_SLOT})
    assert protocol.activated(reported) is True
    assert protocol.indicator(reported) is True
    assert protocol.lighting_mode(reported) == 1
    assert protocol.ramp_seconds(reported) == 0
