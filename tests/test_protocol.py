"""Tests for the semantic protocol codec (byte layouts <-> values)."""

import datetime

import pytest

from custom_components.glowrium import protocol
from custom_components.glowrium.const import (
    DST_OFF,
    DST_ON,
    KEY_BRIGHTNESS,
    KEY_DST,
    KEY_RAMP,
    KEY_TIME,
    KEY_TIME_SYNCED,
    KEY_TIMER,
    TIMER_DEFAULT,
)


def test_be2_minutes_roundtrip() -> None:
    """Minutes encode to 2-byte big-endian seconds and clamp to the field."""
    assert protocol.be2_minutes_to_bytes(30) == bytes.fromhex("0708")  # 1800 s
    assert protocol.be2_minutes_to_bytes(0) == bytes.fromhex("0000")
    assert protocol.be2_minutes_to_bytes(-5) == bytes.fromhex("0000")  # clamped up
    assert protocol.be2_minutes_to_bytes(10_000) == (1092 * 60).to_bytes(2, "big")


def test_ramp_minutes() -> None:
    """The 0x2f ramp decodes to minutes, None when absent or empty."""
    assert protocol.ramp_minutes({KEY_RAMP: bytes.fromhex("0708")}) == 30
    assert protocol.ramp_minutes({}) is None
    assert protocol.ramp_minutes({KEY_RAMP: b""}) is None


def test_timer_slot_guards() -> None:
    """timer_slot returns bytes only for a present, long-enough slot."""
    assert protocol.timer_slot({}) is None
    assert protocol.timer_slot({KEY_TIMER: b"\x00\x00"}) is None  # too short
    assert protocol.timer_slot({KEY_TIMER: TIMER_DEFAULT}) == TIMER_DEFAULT


def test_editable_timer_slot_is_an_independent_copy() -> None:
    """editable_timer_slot yields a mutable copy, or None when never read.

    It deliberately does not fall back to TIMER_DEFAULT: the slot is written as
    one unit, so defaulting to change a single field overwrites the rest.
    """
    assert protocol.editable_timer_slot({}) is None
    slot = protocol.editable_timer_slot({KEY_TIMER: bytes(TIMER_DEFAULT)})
    assert slot == bytearray(TIMER_DEFAULT)
    slot[4] = 7  # mutating the copy must not touch the module default
    assert TIMER_DEFAULT[4] != 7


def test_schedule_fields_decode() -> None:
    """Start/end/brightness/gradual decode from their 0x11 slot offsets."""
    slot = bytearray(TIMER_DEFAULT)
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
    """A malformed hour/minute reads as None rather than raising."""
    slot = bytearray(TIMER_DEFAULT)
    slot[4] = 25  # invalid hour
    assert protocol.schedule_start({KEY_TIMER: bytes(slot)}) is None


# A slot unlike TIMER_DEFAULT in every byte a setter could touch or spare: a
# setter that rebuilt the slot from the default, or wrote one field too many,
# shows in it.
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
    assert protocol.with_schedule_gradual({KEY_TIMER: b"\x01\x00"}, 15) is None


def test_dst_is_read_from_the_flag_byte() -> None:
    """The 0x35 slot is a flag and an offset; only the flag says on or off."""
    assert protocol.dst_enabled({KEY_DST: DST_ON}) is True
    assert protocol.dst_enabled({KEY_DST: DST_OFF}) is False
    assert protocol.dst_enabled({}) is None
    assert protocol.dst_enabled({KEY_DST: b""}) is None
    assert protocol.dst_enabled({KEY_DST: "on"}) is None
    # Only 1 is on: anything else in the flag byte is not a lamp saying yes.
    assert protocol.dst_enabled({KEY_DST: bytes.fromhex("0200000e10")}) is False


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
    """Absent or cut short reads as unknown: there is no drift to judge."""
    assert protocol.device_time({}) is None
    assert protocol.device_time({KEY_TIME: bytes.fromhex("07ea0712")}) is None
    assert protocol.device_time({KEY_TIME: "07ea0712151823"}) is None


def test_a_clock_that_is_no_date_says_so() -> None:
    """Thirteen months is a report, and a wrong one: the caller corrects it."""
    with pytest.raises(ValueError, match="month"):
        protocol.device_time({KEY_TIME: bytes.fromhex("07ea0d12151823")})


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
        assert protocol.brightness_percent({KEY_BRIGHTNESS: odd}) is None
    assert protocol.brightness_percent({}) is None
