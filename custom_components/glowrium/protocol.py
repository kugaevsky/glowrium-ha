"""Semantic codec for the Glowrium property protocol.

``cbor.py`` owns the *wire* format (bytes <-> Python primitives); this module
owns the *meaning* of those values: it reads the device's raw property map
(int key -> value, as mirrored in ``GlowriumCoordinator.state``) as what each
property means, and writes the layouts back.

There is one reader for each property, and whatever wants its value goes
through it: the entities, by way of the coordinator's accessors; the commands
that build on what the lamp reported; the diagnostics; the log. A reader has
three answers - the value; ``None``, while the lamp has reported nothing
under the id; ``UNUSABLE``, when what it reported is not what the id means.
What a value is, for each property, is said here and nowhere else, and
``PROPERTIES`` has the readers by id.

Keeping every byte offset and minute/second conversion here means the layout of
the 0x11 schedule slot, the 0x2f ramp, the 0x35 daylight-saving slot and the
0x05 clock is read and written in one place, instead of being re-derived in
each platform, in the coordinator and again in ``diagnostics.py`` - which
keeps only how the file words a value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import datetime
import enum
from typing import Any, Final

from .const import (
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
)


class Unusable(enum.Enum):
    """What a reader answers for a value that is there and cannot be used.

    A reader is handed the state and has one of three answers. ``None``: the
    lamp has reported nothing under the id. A value: what the id means. Or
    this: something was reported there, and it is not what the id means - a
    text where a flag belongs, a slot of the wrong length, an hour of 25.

    The two that are no value are kept apart because what follows from them
    differs. A setting nobody has reported may show what it showed before a
    restart, and a write that builds on it can be told to wait. One that was
    reported and cannot be used has been heard from: it is unknown, and
    waiting will not mend it.

    It cannot be taken for a value by leaving the check out. It is no
    number and no bytes, and it refuses to be truth-tested - an object is
    true, and "true" is how a flag reads as on.
    """

    UNUSABLE = "unusable"

    def __bool__(self) -> bool:
        """Refuse: what cannot be used is neither true nor false."""
        raise TypeError("a value that cannot be used is neither true nor false")


UNUSABLE: Final = Unusable.UNUSABLE


def usable[T](reading: T | Unusable | None) -> T | None:
    """Return ``reading`` when it is a value, and nothing when it is not.

    For whoever has the same thing to show for both answers that are no
    value - the light and the diagnostic entities, which say unknown.
    """
    return None if reading is None or reading is UNUSABLE else reading


def _of[T, R](
    reading: T | Unusable | None, part: Callable[[T], R]
) -> R | Unusable | None:
    """Return a part of a reading that is a value; hand on either that is none.

    For a reader of one field of what another reader has read: the start of
    a schedule is not reported, or cannot be used, exactly when the slot is.
    """
    if reading is None or reading is UNUSABLE:
        return reading
    return part(reading)


# --- flags: power (0x06), the two modes (0x09, 0x0d), activated (0x14), ---
# --- the indicator (0x17) ---


def _flag(state: Mapping[int, Any], key: int) -> bool | Unusable | None:
    """Read a flag: a boolean, and nothing that only reads as one."""
    if key not in state:
        return None
    value = state[key]
    return value if isinstance(value, bool) else UNUSABLE


def power(state: Mapping[int, Any]) -> bool | Unusable | None:
    """Read whether the lamp is on (0x06)."""
    return _flag(state, KEY_POWER)


def circadian_mode(state: Mapping[int, Any]) -> bool | Unusable | None:
    """Read the flag of the Circadian mode (0x09)."""
    return _flag(state, KEY_CIRCADIAN)


def schedule_mode(state: Mapping[int, Any]) -> bool | Unusable | None:
    """Read the flag of the Schedule mode (0x0d)."""
    return _flag(state, KEY_SCHEDULE)


def activated(state: Mapping[int, Any]) -> bool | Unusable | None:
    """Read whether the lamp has been brought up (0x14)."""
    return _flag(state, KEY_ACTIVATED)


def indicator(state: Mapping[int, Any]) -> bool | Unusable | None:
    """Read whether the indicator light is on (0x17)."""
    return _flag(state, KEY_INDICATOR)


# The ramp (0x2f) and the schedule gradual field are 2-byte big-endian seconds.
_MAX_MINUTES = 0xFFFF // 60


def be2_minutes_to_bytes(minutes: int) -> bytes:
    """Encode minutes as the device's 2-byte big-endian seconds field (clamped)."""
    return (max(0, min(minutes, _MAX_MINUTES)) * 60).to_bytes(2, "big")


# --- brightness (0x08) ---

_PERCENT = 100


def brightness_percent(state: Mapping[int, Any]) -> float | Unusable | None:
    """Read the brightness (0x08) as a level from 0 to 100.

    What is no level cannot be used: a flag, a string, a number out of
    range - or ``inf`` or ``nan``, which a float can hold and which cannot
    even be rounded. A level need not be a whole number: the G7 reports
    one, and nothing says every model does.
    """
    if KEY_BRIGHTNESS not in state:
        return None
    value = state[KEY_BRIGHTNESS]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return UNUSABLE
    if not 0 <= value <= _PERCENT:  # false for nan, and for both infinities
        return UNUSABLE
    return value


# --- lighting mode (0x2b) ---

_ONE_BYTE = 0xFF


def lighting_mode(state: Mapping[int, Any]) -> int | Unusable | None:
    """Read the index of the lighting mode (0x2b): a whole number of one byte.

    Which preset it is, if any, is the model profile's to say. A flag is no
    index, though ``True`` equals 1 wherever the two are compared.
    """
    if KEY_LIGHTING_MODE not in state:
        return None
    value = state[KEY_LIGHTING_MODE]
    if isinstance(value, bool) or not isinstance(value, int):
        return UNUSABLE
    return value if 0 <= value <= _ONE_BYTE else UNUSABLE


# --- where the lamp is (0x0a, 0x0b): degrees, for its circadian curve ---


def _degrees(
    state: Mapping[int, Any], key: int, limit: float
) -> float | Unusable | None:
    """Read a coordinate: a float no further from zero than ``limit``."""
    if key not in state:
        return None
    value = state[key]
    if not isinstance(value, float):
        return UNUSABLE
    # False for nan, and for both infinities.
    return value if -limit <= value <= limit else UNUSABLE


def latitude(state: Mapping[int, Any]) -> float | Unusable | None:
    """Read the latitude the lamp holds (0x0a), in degrees."""
    return _degrees(state, KEY_LATITUDE, 90)


def longitude(state: Mapping[int, Any]) -> float | Unusable | None:
    """Read the longitude the lamp holds (0x0b), in degrees."""
    return _degrees(state, KEY_LONGITUDE, 180)


# --- ramp (0x2f): two bytes of seconds ---

# No ramp, fade or daylight-saving offset is longer than this many seconds.
_LONGEST = 2 * 60 * 60
_RAMP_LENGTH = 2


def _octets(value: Any, length: int) -> bytes | None:
    """Return ``value`` as bytes, if it is a byte string of exactly ``length``.

    A length is the first thing a layout is held to, and not the last: seven
    bytes can be a clock, or six bytes of an address and one more.
    """
    if isinstance(value, (bytes, bytearray)) and len(value) == length:
        return bytes(value)
    return None


def _seconds(raw: bytes) -> int | None:
    """Read a big-endian count of seconds, if it is no longer than any runs."""
    seconds = int.from_bytes(raw, "big")
    return seconds if seconds <= _LONGEST else None


def reported_ramp(state: Mapping[int, Any]) -> bytes | Unusable | None:
    """Return the ramp (0x2f) as the lamp reported it, when it is a ramp.

    The two bytes themselves: a command that carries the ramp sends back
    what the lamp said, and only what it could have meant.
    """
    if KEY_RAMP not in state:
        return None
    raw = _octets(state[KEY_RAMP], _RAMP_LENGTH)
    if raw is None or _seconds(raw) is None:
        return UNUSABLE
    return raw


def ramp_seconds(state: Mapping[int, Any]) -> int | Unusable | None:
    """Read the circadian ramp (0x2f) in seconds."""
    return _of(reported_ramp(state), lambda raw: int.from_bytes(raw, "big"))


def ramp_minutes(state: Mapping[int, Any]) -> int | Unusable | None:
    """Read the circadian ramp (0x2f) in minutes."""
    return _of(ramp_seconds(state), lambda seconds: seconds // 60)


# --- schedule slot (0x11) ---

# The slot is eleven bytes: [enabled, 00, 00, 00, start_h, start_m, end_h,
# end_m, brightness, fade_seconds_be(2)]. A value of any other length under
# the id is no slot.
_TIMER_START_H: Final = 4
_TIMER_START_M: Final = 5
_TIMER_END_H: Final = 6
_TIMER_END_M: Final = 7
_TIMER_BRIGHTNESS: Final = 8
_TIMER_GRADUAL: Final = 9  # two bytes, big-endian seconds
_TIMER_SLOT_LENGTH: Final = 11


@dataclass(frozen=True)
class Schedule:
    """The schedule slot (0x11), as what its bytes mean.

    Without the three bytes nobody has decoded: a G7 leaves them at zero, a
    G8 was seen to set them, and they are carried in a write as they came.
    """

    enabled: bool
    start: datetime.time
    end: datetime.time
    brightness: int
    fade_seconds: int


def _schedule_in(raw: bytes) -> Schedule | None:
    """Read eleven bytes as a schedule, if each is what its place means."""
    if raw[0] not in (0, 1):
        return None
    try:
        start = datetime.time(raw[_TIMER_START_H], raw[_TIMER_START_M])
        end = datetime.time(raw[_TIMER_END_H], raw[_TIMER_END_M])
    except ValueError:  # an hour of 25 is no time of day
        return None
    fade = _seconds(raw[_TIMER_GRADUAL : _TIMER_GRADUAL + 2])
    if raw[_TIMER_BRIGHTNESS] > _PERCENT or fade is None:
        return None
    return Schedule(bool(raw[0]), start, end, raw[_TIMER_BRIGHTNESS], fade)


def _slot(state: Mapping[int, Any]) -> tuple[bytes, Schedule] | Unusable | None:
    """Read the slot both ways at once: its bytes, and what they mean."""
    if KEY_TIMER not in state:
        return None
    raw = _octets(state[KEY_TIMER], _TIMER_SLOT_LENGTH)
    read = None if raw is None else _schedule_in(raw)
    if raw is None or read is None:
        return UNUSABLE
    return raw, read


def timer_slot(state: Mapping[int, Any]) -> bytes | Unusable | None:
    """Return the schedule slot (0x11) as the lamp reported it, when it is one.

    Eleven bytes and no more: a longer value read by its first eleven would
    be edited in place and written back with the rest attached.
    """
    return _of(_slot(state), lambda slot: slot[0])


def schedule(state: Mapping[int, Any]) -> Schedule | Unusable | None:
    """Read the schedule slot (0x11) into its fields."""
    return _of(_slot(state), lambda slot: slot[1])


def editable_timer_slot(state: Mapping[int, Any]) -> bytearray | Unusable | None:
    """Return a mutable copy of the 0x11 slot, when the lamp reported one.

    Deliberately no default fallback. The slot packs the enabled flag, both
    times, brightness and the fade into a single write, so substituting a
    default to change one field silently overwrites the other four with values
    the user never chose (and forces enabled=1). Callers must refuse instead -
    for a slot never reported, and for one that cannot be used.
    """
    return _of(timer_slot(state), bytearray)


def schedule_start(state: Mapping[int, Any]) -> datetime.time | Unusable | None:
    """Read the schedule start (on) time from the 0x11 slot."""
    return _of(schedule(state), lambda slot: slot.start)


def schedule_end(state: Mapping[int, Any]) -> datetime.time | Unusable | None:
    """Read the schedule end (off) time from the 0x11 slot."""
    return _of(schedule(state), lambda slot: slot.end)


def schedule_brightness(state: Mapping[int, Any]) -> int | Unusable | None:
    """Read the schedule target brightness (%) from the 0x11 slot."""
    return _of(schedule(state), lambda slot: slot.brightness)


def schedule_gradual_minutes(state: Mapping[int, Any]) -> int | Unusable | None:
    """Read the schedule gradual-fade duration (minutes) from the 0x11 slot."""
    return _of(schedule(state), lambda slot: slot.fade_seconds // 60)


def _slot_with(
    state: Mapping[int, Any], at: int, field: bytes
) -> bytes | Unusable | None:
    """Return the 0x11 slot with ``field`` written at ``at``, the rest as it came.

    Nothing for a slot never reported, and that it cannot be used for one
    that is no slot: either way there is nothing to carry the rest over from.
    """
    slot = editable_timer_slot(state)
    if slot is None or slot is UNUSABLE:
        return slot
    slot[at : at + len(field)] = field
    return bytes(slot)


def with_schedule_start(
    state: Mapping[int, Any], hour: int, minute: int
) -> bytes | Unusable | None:
    """Return the 0x11 slot with a new start (on) time, the rest carried over."""
    return _slot_with(state, _TIMER_START_H, bytes([hour, minute]))


def with_schedule_end(
    state: Mapping[int, Any], hour: int, minute: int
) -> bytes | Unusable | None:
    """Return the 0x11 slot with a new end (off) time, the rest carried over."""
    return _slot_with(state, _TIMER_END_H, bytes([hour, minute]))


def with_schedule_brightness(
    state: Mapping[int, Any], percent: int
) -> bytes | Unusable | None:
    """Return the 0x11 slot with a new target brightness, kept within 0..100."""
    return _slot_with(state, _TIMER_BRIGHTNESS, bytes([max(0, min(100, percent))]))


def with_schedule_gradual(
    state: Mapping[int, Any], minutes: int
) -> bytes | Unusable | None:
    """Return the 0x11 slot with a new gradual-fade duration in minutes."""
    return _slot_with(state, _TIMER_GRADUAL, be2_minutes_to_bytes(minutes))


# --- daylight saving (0x35): a flag, then the offset to apply ---

# Five bytes: the flag, then the offset in seconds - an hour.
_DST_ON: Final = bytes.fromhex("0100000e10")
_DST_OFF: Final = bytes.fromhex("0000000e10")


@dataclass(frozen=True)
class DaylightSaving:
    """The daylight-saving slot (0x35): whether it is on, and by how much."""

    enabled: bool
    offset_seconds: int


def daylight_saving(state: Mapping[int, Any]) -> DaylightSaving | Unusable | None:
    """Read the daylight-saving slot (0x35): a flag byte, then seconds."""
    if KEY_DST not in state:
        return None
    raw = _octets(state[KEY_DST], len(_DST_ON))
    if raw is None or raw[0] not in (0, 1):
        return UNUSABLE
    offset = _seconds(raw[1:])
    if offset is None:
        return UNUSABLE
    return DaylightSaving(bool(raw[0]), offset)


def dst_enabled(state: Mapping[int, Any]) -> bool | Unusable | None:
    """Read whether daylight saving is on."""
    return _of(daylight_saving(state), lambda slot: slot.enabled)


def with_dst(state: Mapping[int, Any], enabled: bool) -> bytes:
    """Return the 0x35 slot with daylight saving switched on or off.

    The flag and the offset are written together, so only the flag is ours to
    change: a fixed hour would turn a half-hour region into a full one the
    moment the switch is touched. Unlike the schedule slot this is one field
    with a near-universal default, so a lamp that has not reported a slot
    gets the hour rather than a refusal - and so does one whose slot cannot
    be used: its offset is not one to carry over.
    """
    slot = daylight_saving(state)
    if slot is None or slot is UNUSABLE:
        return _DST_ON if enabled else _DST_OFF
    return bytes([int(enabled)]) + bytes(state[KEY_DST][1:])


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


def _clock_in(raw: bytes) -> datetime.datetime | None:
    """Read seven bytes as a date and a time, if they are one."""
    try:
        # Naive on purpose: the lamp keeps local wall-clock time and has no
        # notion of a zone.
        return datetime.datetime(  # noqa: DTZ001
            (raw[0] << 8) | raw[1], raw[2], raw[3], raw[4], raw[5], raw[6]
        )
    except ValueError:
        return None


def device_time(state: Mapping[int, Any]) -> datetime.datetime | Unusable | None:
    """Read the clock the lamp reported (0x05): naive, as the lamp keeps it.

    Seven bytes, and a date and a time in them. A clock that shows the
    thirteenth month cannot be used either - what sets it apart from a clock
    kept in some other shape is ``clock_is_no_date``.
    """
    if KEY_TIME not in state:
        return None
    raw = _octets(state[KEY_TIME], _CLOCK_LENGTH)
    shown = None if raw is None else _clock_in(raw)
    return UNUSABLE if shown is None else shown


def clock_is_no_date(state: Mapping[int, Any]) -> bool:
    """Tell whether the lamp reported a clock that shows no date.

    Seven bytes are a clock, whatever they show, and one that shows no date
    is a clock that is wrong: writing the time puts it right. Anything else
    under the id is a clock kept in a way nobody has seen, and seven bytes
    written over it would be a guess.
    """
    raw = _octets(state.get(KEY_TIME), _CLOCK_LENGTH)
    return raw is not None and _clock_in(raw) is None


# --- operating mode: two flags that exclude each other (0x09, 0x0d) ---


def operating_mode(state: Mapping[int, Any]) -> str | Unusable | None:
    """Read the operating mode from the two mode flags.

    A mode whose flag says yes is the mode, whatever stands under the other.
    Manual takes both flags, and both saying no: one flag alone leaves the
    mode not reported, so that a lamp not yet read does not look as if it
    were in Manual. Where no flag says yes and one of them cannot be used,
    neither can the mode.
    """
    circadian = circadian_mode(state)
    scheduled = schedule_mode(state)
    if circadian is True:
        return MODE_CIRCADIAN
    if scheduled is True:
        return MODE_SCHEDULE
    if circadian is False and scheduled is False:
        return MODE_MANUAL
    if circadian is UNUSABLE or scheduled is UNUSABLE:
        return UNUSABLE
    return None


# --- every property the integration reads, by id ---

# The name each goes by here - in the log and in the diagnostics download -
# and the reader that says what its value is. One table, so that the warning
# about a value that cannot be used, the file and the entities all mean the
# same thing by it.
PROPERTIES: Final[Mapping[int, tuple[str, Callable[[Mapping[int, Any]], object]]]] = {
    KEY_TIME: ("clock", device_time),
    KEY_POWER: ("power", power),
    KEY_BRIGHTNESS: ("brightness", brightness_percent),
    KEY_CIRCADIAN: ("circadian", circadian_mode),
    KEY_LATITUDE: ("latitude", latitude),
    KEY_LONGITUDE: ("longitude", longitude),
    KEY_SCHEDULE: ("schedule mode", schedule_mode),
    KEY_TIMER: ("schedule", schedule),
    KEY_ACTIVATED: ("activated", activated),
    KEY_INDICATOR: ("indicator", indicator),
    KEY_LIGHTING_MODE: ("lighting mode", lighting_mode),
    KEY_RAMP: ("ramp", ramp_seconds),
    KEY_DST: ("daylight saving", daylight_saving),
}
