"""Tests for the semantic protocol codec (byte layouts <-> values)."""

import datetime

from custom_components.glowrium import protocol
from custom_components.glowrium.const import KEY_RAMP, KEY_TIMER, TIMER_DEFAULT


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
    """editable_timer_slot yields a mutable copy, defaulting when unset."""
    assert protocol.editable_timer_slot({}) == bytearray(TIMER_DEFAULT)
    slot = protocol.editable_timer_slot({})
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
