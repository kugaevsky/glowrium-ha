"""Semantic codec for the Glowrium property protocol.

``cbor.py`` owns the *wire* format (bytes <-> Python primitives); this module
owns the *meaning* of those values: it converts the device's raw property map
(int key -> value, as mirrored in ``GlowriumCoordinator.state``) to and from the
units the entities use.

Keeping every byte offset and minute/second conversion here means the layout of
the 0x11 schedule slot, the 0x2f ramp, the 0x35 daylight-saving slot and the
0x05 clock is read and written in one place, instead of being re-derived in
each platform and again in the coordinator. The coordinator exposes thin typed
accessors and setters that delegate here, and the platforms go through those.
(``diagnostics.py`` keeps readers of its own, stricter and on purpose: it
judges what the lamp sent rather than using it.)
"""

from __future__ import annotations

from collections.abc import Mapping
import datetime
from typing import Any

from .const import (
    DST_OFF,
    DST_ON,
    KEY_BRIGHTNESS,
    KEY_DST,
    KEY_RAMP,
    KEY_TIME,
    KEY_TIME_SYNCED,
    KEY_TIMER,
    TIMER_BRIGHTNESS,
    TIMER_END_H,
    TIMER_END_M,
    TIMER_GRADUAL,
    TIMER_SLOT_LENGTH,
    TIMER_START_H,
    TIMER_START_M,
)

# The ramp (0x2f) and the schedule gradual field are 2-byte big-endian seconds.
_MAX_MINUTES = 0xFFFF // 60


def be2_minutes_to_bytes(minutes: int) -> bytes:
    """Encode minutes as the device's 2-byte big-endian seconds field (clamped)."""
    return (max(0, min(minutes, _MAX_MINUTES)) * 60).to_bytes(2, "big")


def _be2_to_minutes(raw: bytes) -> int:
    return int.from_bytes(raw[:2], "big") // 60


# --- brightness (0x08) ---

_PERCENT = 100


def brightness_percent(state: Mapping[int, Any]) -> float | None:
    """Decode the brightness (0x08) as a level from 0 to 100, or None.

    None when it was never read, and when what was reported is no level: a
    flag, a string, a number out of range - or ``inf`` or ``nan``, which a
    float can hold and which cannot even be rounded. A level need not be a
    whole number: the G7 reports one, and nothing says every model does.
    """
    value = state.get(KEY_BRIGHTNESS)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= _PERCENT:  # false for nan, and for both infinities
        return None
    return value


# --- ramp (0x2f) ---


def ramp_minutes(state: Mapping[int, Any]) -> int | None:
    """Decode the circadian ramp (0x2f) to minutes, or None if not yet known."""
    value = state.get(KEY_RAMP)
    if isinstance(value, (bytes, bytearray)) and value:
        return _be2_to_minutes(bytes(value))
    return None


# --- schedule slot (0x11) ---


def timer_slot(state: Mapping[int, Any]) -> bytes | None:
    """Return the raw 0x11 schedule slot if present and well-formed, else None."""
    value = state.get(KEY_TIMER)
    if isinstance(value, (bytes, bytearray)) and len(value) >= TIMER_SLOT_LENGTH:
        return bytes(value)
    return None


def editable_timer_slot(state: Mapping[int, Any]) -> bytearray | None:
    """Return a mutable copy of the 0x11 slot, or None if it was never read.

    Deliberately no default fallback. The slot packs the enabled flag, both
    times, brightness and the fade into a single write, so substituting a
    default to change one field silently overwrites the other four with values
    the user never chose (and forces enabled=1). Callers must refuse instead.
    """
    slot = timer_slot(state)
    return bytearray(slot) if slot is not None else None


def _slot_time(
    state: Mapping[int, Any], hour_i: int, minute_i: int
) -> datetime.time | None:
    slot = timer_slot(state)
    if slot is None:
        return None
    try:
        return datetime.time(slot[hour_i], slot[minute_i])
    except ValueError:  # a malformed slot should read as unknown, not crash
        return None


def schedule_start(state: Mapping[int, Any]) -> datetime.time | None:
    """Decode the schedule start (on) time from the 0x11 slot."""
    return _slot_time(state, TIMER_START_H, TIMER_START_M)


def schedule_end(state: Mapping[int, Any]) -> datetime.time | None:
    """Decode the schedule end (off) time from the 0x11 slot."""
    return _slot_time(state, TIMER_END_H, TIMER_END_M)


def schedule_brightness(state: Mapping[int, Any]) -> int | None:
    """Decode the schedule target brightness (%) from the 0x11 slot."""
    slot = timer_slot(state)
    return slot[TIMER_BRIGHTNESS] if slot is not None else None


def schedule_gradual_minutes(state: Mapping[int, Any]) -> int | None:
    """Decode the schedule gradual-fade duration (minutes) from the 0x11 slot."""
    slot = timer_slot(state)
    if slot is None:
        return None
    return _be2_to_minutes(slot[TIMER_GRADUAL : TIMER_GRADUAL + 2])


def _slot_with(state: Mapping[int, Any], at: int, field: bytes) -> bytes | None:
    """Return the 0x11 slot with ``field`` written at ``at``; None if never read."""
    slot = editable_timer_slot(state)
    if slot is None:
        return None
    slot[at : at + len(field)] = field
    return bytes(slot)


def _slot_with_time(
    state: Mapping[int, Any], hour_i: int, minute_i: int, hour: int, minute: int
) -> bytes | None:
    """Return the 0x11 slot with a time at its two offsets; None if never read."""
    slot = editable_timer_slot(state)
    if slot is None:
        return None
    slot[hour_i], slot[minute_i] = hour, minute
    return bytes(slot)


def with_schedule_start(
    state: Mapping[int, Any], hour: int, minute: int
) -> bytes | None:
    """Return the 0x11 slot with a new start (on) time, the rest carried over."""
    return _slot_with_time(state, TIMER_START_H, TIMER_START_M, hour, minute)


def with_schedule_end(state: Mapping[int, Any], hour: int, minute: int) -> bytes | None:
    """Return the 0x11 slot with a new end (off) time, the rest carried over."""
    return _slot_with_time(state, TIMER_END_H, TIMER_END_M, hour, minute)


def with_schedule_brightness(state: Mapping[int, Any], percent: int) -> bytes | None:
    """Return the 0x11 slot with a new target brightness, kept within 0..100."""
    return _slot_with(state, TIMER_BRIGHTNESS, bytes([max(0, min(100, percent))]))


def with_schedule_gradual(state: Mapping[int, Any], minutes: int) -> bytes | None:
    """Return the 0x11 slot with a new gradual-fade duration in minutes."""
    return _slot_with(state, TIMER_GRADUAL, be2_minutes_to_bytes(minutes))


# --- daylight saving (0x35): a flag, then the offset to apply ---


def dst_enabled(state: Mapping[int, Any]) -> bool | None:
    """Decode whether daylight saving is on, or None if not yet known."""
    value = state.get(KEY_DST)
    if isinstance(value, (bytes, bytearray)) and value:
        return value[0] == 1
    return None


def with_dst(state: Mapping[int, Any], enabled: bool) -> bytes:
    """Return the 0x35 slot with daylight saving switched on or off.

    The flag and the offset are written together, so only the flag is ours to
    change: a fixed hour would turn a half-hour region into a full one the
    moment the switch is touched. Unlike the schedule slot this is one field
    with a near-universal default, so a lamp that has not reported a whole
    slot gets the hour rather than a refusal.
    """
    default = DST_ON if enabled else DST_OFF
    reported = state.get(KEY_DST)
    if isinstance(reported, (bytes, bytearray)) and len(reported) == len(default):
        return bytes([int(enabled)]) + bytes(reported[1:])
    return default


# --- clock (0x05): year_be(2), month, day, hour, minute, second ---

_CLOCK_LENGTH = 7


def encode_device_time(when: datetime.datetime) -> bytes:
    """Encode a local wall-clock time as the device keeps it."""
    return bytes(
        [
            when.year >> 8,
            when.year & 0xFF,
            when.month,
            when.day,
            when.hour,
            when.minute,
            when.second,
        ]
    )


def clock_command(when: datetime.datetime) -> dict[int, Any]:
    """Return the command that sets the lamp's clock to ``when``.

    The time goes with the flag that says it has been set, in one write, as
    the vendor app sends them.
    """
    return {KEY_TIME: encode_device_time(when), KEY_TIME_SYNCED: 1}


def device_time(state: Mapping[int, Any]) -> datetime.datetime | None:
    """Decode the clock the lamp reported, or None if it has reported none.

    Naive on purpose: the lamp keeps local wall-clock time and has no notion
    of a zone. Raises ``ValueError`` for bytes that are no date - that is a
    report too, and a wrong one, which the caller may want to correct.
    """
    raw = state.get(KEY_TIME)
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < _CLOCK_LENGTH:
        return None
    return datetime.datetime(  # noqa: DTZ001 - see the docstring
        (raw[0] << 8) | raw[1], raw[2], raw[3], raw[4], raw[5], raw[6]
    )
