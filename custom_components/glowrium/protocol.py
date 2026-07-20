"""Semantic codec for the Glowrium property protocol.

``cbor.py`` owns the *wire* format (bytes <-> Python primitives); this module
owns the *meaning* of those values: it converts the device's raw property map
(int key -> value, as mirrored in ``GlowriumCoordinator.state``) to and from the
units the entities use.

Keeping every byte offset and minute/second conversion here means the layout of
the 0x11 schedule slot and the 0x2f ramp lives in exactly one place instead of
being re-derived in each platform. The coordinator exposes thin typed accessors
that delegate here, so entities never touch raw bytes or the state dict.
"""

from __future__ import annotations

import datetime
from typing import Any

from .const import (
    KEY_RAMP,
    KEY_TIMER,
    TIMER_BRIGHTNESS,
    TIMER_DEFAULT,
    TIMER_END_H,
    TIMER_END_M,
    TIMER_GRADUAL,
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


# --- ramp (0x2f) ---


def ramp_minutes(state: dict[int, Any]) -> int | None:
    """Decode the circadian ramp (0x2f) to minutes, or None if not yet known."""
    value = state.get(KEY_RAMP)
    if isinstance(value, (bytes, bytearray)) and value:
        return _be2_to_minutes(bytes(value))
    return None


# --- schedule slot (0x11) ---


def timer_slot(state: dict[int, Any]) -> bytes | None:
    """Return the raw 0x11 schedule slot if present and well-formed, else None."""
    value = state.get(KEY_TIMER)
    if isinstance(value, (bytes, bytearray)) and len(value) >= len(TIMER_DEFAULT):
        return bytes(value)
    return None


def editable_timer_slot(state: dict[int, Any]) -> bytearray:
    """Return a mutable copy of the 0x11 slot, falling back to the default."""
    slot = timer_slot(state)
    return bytearray(slot if slot is not None else TIMER_DEFAULT)


def _slot_time(
    state: dict[int, Any], hour_i: int, minute_i: int
) -> datetime.time | None:
    slot = timer_slot(state)
    if slot is None:
        return None
    try:
        return datetime.time(slot[hour_i], slot[minute_i])
    except ValueError:  # a malformed slot should read as unknown, not crash
        return None


def schedule_start(state: dict[int, Any]) -> datetime.time | None:
    """Decode the schedule start (on) time from the 0x11 slot."""
    return _slot_time(state, TIMER_START_H, TIMER_START_M)


def schedule_end(state: dict[int, Any]) -> datetime.time | None:
    """Decode the schedule end (off) time from the 0x11 slot."""
    return _slot_time(state, TIMER_END_H, TIMER_END_M)


def schedule_brightness(state: dict[int, Any]) -> int | None:
    """Decode the schedule target brightness (%) from the 0x11 slot."""
    slot = timer_slot(state)
    return slot[TIMER_BRIGHTNESS] if slot is not None else None


def schedule_gradual_minutes(state: dict[int, Any]) -> int | None:
    """Decode the schedule gradual-fade duration (minutes) from the 0x11 slot."""
    slot = timer_slot(state)
    if slot is None:
        return None
    return _be2_to_minutes(slot[TIMER_GRADUAL : TIMER_GRADUAL + 2])
