"""Diagnostics for the Glowrium integration.

One download that answers what a bug report is otherwise asked for piece by
piece: the model and firmware, what the lamp last reported, and where the link
stands.

It is meant to be attached to a public issue, and nearly all of what could go
into it was chosen by the lamp: which properties it reports and what it puts
under them, which fields its device-info string has, what they are called and
where one ends. So the file repeats nothing after the lamp. Each property the
integration knows is read the way the integration reads it, and written out
from that reading - a schedule as its times, a clock as how far it was from
this host's when it came. What does not read as what its name means is said
to be there and not as expected. What has no name here is counted. No value,
no size and no id that the lamp picked goes into the file as the lamp gave it.

That holds for what this module returns. Home Assistant wraps it in a header
of its own (version, time zone, the names of the custom integrations) and adds
the ids of the integration's open repairs - which is why the coordinator files
its repair under the config entry's id and not under the lamp's address.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta
from functools import partial
from typing import Any, Final

from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import CONF_MODEL_ID
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import GlowriumConfigEntry, identity
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
    KEY_TIMER,
    TIMER_BRIGHTNESS,
    TIMER_END_H,
    TIMER_END_M,
    TIMER_GRADUAL,
    TIMER_START_H,
    TIMER_START_M,
)

# No ramp, fade or daylight-saving offset is longer than this many seconds.
_LONGEST: Final = 2 * 60 * 60
# Past this the clock is simply wrong, and by how much says nothing more.
_FAR_OFF: Final = timedelta(days=366)
_PERCENT: Final = 100
_ONE_BYTE: Final = 0xFF
_NOT_AS_EXPECTED: Final = "not as expected"


def _octets(value: Any, length: int) -> bytes:
    """Return ``value`` as bytes, if it is a byte string of exactly ``length``."""
    if isinstance(value, (bytes, bytearray)) and len(value) == length:
        return bytes(value)
    raise ValueError("not the bytes this property is")


def _flag(value: Any) -> bool:
    """Read a boolean."""
    if isinstance(value, bool):
        return value
    raise ValueError("not a flag")


def _up_to(limit: int) -> Callable[[Any], int]:
    """Read a whole number from zero to ``limit``."""

    def read(value: Any) -> int:
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= limit
        ):
            return value
        raise ValueError("not a number this property takes")

    return read


def _seconds(raw: bytes) -> int:
    """Read a big-endian count of seconds no longer than any of them runs."""
    seconds = int.from_bytes(raw, "big")
    if seconds > _LONGEST:
        raise ValueError("longer than a ramp, a fade or an offset is")
    return seconds


def _clock(value: Any, heard_at: datetime | None) -> dict[str, Any]:
    """Read the device clock, as how far it was from this host's when it came.

    Not as the time it shows: how far off it is is what a report needs, and
    it is a reading of the bytes rather than a copy of them.

    And not against the host's clock of now. The mirror holds the clock the
    lamp last reported, which can be hours old - a link that goes on talking
    is not asked for it again, and a lamp out of reach cannot be - and a
    clock that was exactly right then would read as slow by all of that. It
    is set against the host's clock of the moment it came (``heard_at``), and
    how long ago that was is said next to it.
    """
    raw = _octets(value, 7)
    # Naive, like the clock itself: the lamp keeps local wall-clock time.
    shown = datetime(int.from_bytes(raw[:2], "big"), *raw[2:])  # noqa: DTZ001
    if heard_at is None:  # the mirror dates every clock it takes: a guard
        return {"ahead_of_this_host_by_seconds": None, "as_of_seconds_ago": None}
    ahead = shown - heard_at.replace(tzinfo=None)
    return {
        "ahead_of_this_host_by_seconds": (
            "more than a year off"
            if abs(ahead) > _FAR_OFF
            else round(ahead.total_seconds())
        ),
        "as_of_seconds_ago": round((dt_util.now() - heard_at).total_seconds()),
    }


def _schedule(value: Any) -> dict[str, Any]:
    """Read the schedule slot into its fields.

    The three bytes nobody has decoded are not among them.
    """
    raw = _octets(value, 11)
    if raw[0] not in (0, 1):
        raise ValueError("neither on nor off")
    start = time(raw[TIMER_START_H], raw[TIMER_START_M])
    end = time(raw[TIMER_END_H], raw[TIMER_END_M])
    return {
        "enabled": bool(raw[0]),
        "start": start.isoformat("minutes"),
        "end": end.isoformat("minutes"),
        "brightness": _up_to(_PERCENT)(raw[TIMER_BRIGHTNESS]),
        "fade_seconds": _seconds(raw[TIMER_GRADUAL:]),
    }


def _ramp(value: Any) -> dict[str, Any]:
    """Read the ramp: two bytes of seconds."""
    return {"seconds": _seconds(_octets(value, 2))}


def _dst(value: Any) -> dict[str, Any]:
    """Read the daylight-saving slot: a flag and an offset in seconds."""
    raw = _octets(value, 5)
    if raw[0] not in (0, 1):
        raise ValueError("neither on nor off")
    return {"enabled": bool(raw[0]), "offset_seconds": _seconds(raw[1:])}


# The properties that are read out, under the id and the name each has here,
# with the reading its value has to survive. A length is not a reading: seven
# bytes can be a clock or six bytes of address and one more. (The clock is
# read too, and takes more than its value to read: see _state.)
_READ: Final[dict[int, tuple[str, Callable[[Any], Any]]]] = {
    KEY_POWER: ("power", _flag),
    KEY_BRIGHTNESS: ("brightness", _up_to(_PERCENT)),
    KEY_CIRCADIAN: ("circadian", _flag),
    KEY_SCHEDULE: ("schedule mode", _flag),
    KEY_TIMER: ("schedule", _schedule),
    KEY_ACTIVATED: ("activated", _flag),
    KEY_INDICATOR: ("indicator", _flag),
    KEY_LIGHTING_MODE: ("lighting mode", _up_to(_ONE_BYTE)),
    KEY_RAMP: ("ramp", _ramp),
    KEY_DST: ("daylight saving", _dst),
}
# Known by name and private: the home's coordinates, which the lamp keeps for
# its circadian curve. Said to be there, so that it can be seen the lamp has
# them, and never read out.
_WHERE: Final = (KEY_LATITUDE, KEY_LONGITUDE)
_WHERE_NAME: Final = " ".join(f"0x{key:02x}" for key in _WHERE) + " coordinates"


def _as_claimed(read: Callable[[object], str | None], value: Any) -> Any:
    """Return ``value`` if ``read`` takes it for what it claims to be.

    For the model id and the firmware, the two things taken from the
    device-info string (``identity`` has why they are checked at all).
    Nothing is nothing; anything else that is not taken is a placeholder.
    """
    if value is None:
        return None
    return read(value) or REDACTED


def _state(
    reported: dict[Any, Any], clock_heard_at: datetime | None, not_kept: int
) -> dict[str, Any]:
    """Read the state mirror out, property by property.

    What has no name here is counted, and so is every time the mirror had no
    room for a property (``not_kept``): a number each, and nothing of the
    lamp's choosing.
    """
    shown: dict[str, Any] = {}
    clock = ("clock", partial(_clock, heard_at=clock_heard_at))
    for key, (name, read) in ({KEY_TIME: clock} | _READ).items():
        if key not in reported:
            continue
        try:
            shown[f"0x{key:02x} {name}"] = read(reported[key])
        except ValueError:
            shown[f"0x{key:02x} {name}"] = _NOT_AS_EXPECTED
    named = len(shown)
    if any(key in reported for key in _WHERE):
        shown[_WHERE_NAME] = REDACTED
        named += sum(key in reported for key in _WHERE)
    shown["other_properties"] = len(reported) - named
    shown["times_a_property_was_not_kept"] = not_kept
    return dict(sorted(shown.items()))


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GlowriumConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    described = entry.runtime_data.diagnostics()
    device = described["device"]
    return {
        # Field by field. A config entry's address is in its data, its unique
        # id, its title and its discovery record, and Home Assistant adds to
        # what an entry holds from one release to the next.
        "entry": {
            "source": entry.source,
            "version": entry.version,
            "minor_version": entry.minor_version,
            "disabled_by": entry.disabled_by,
            "remembered_model_id": _as_claimed(
                identity.as_model_id, entry.data.get(CONF_MODEL_ID)
            ),
        },
        "device": {
            "model": device["model"],  # from the integration's own table
            "model_id": _as_claimed(identity.as_model_id, device["model_id"]),
            "firmware": _as_claimed(identity.as_firmware, device["firmware"]),
            # How many fields the string had. Not which: a field's name is as
            # much the lamp's choice as its value.
            "device_info_fields": len(device["info"]),
        },
        "link": described["link"],
        "state": _state(
            described["state"],
            described["clock_heard_at"],
            described["not_kept"],
        ),
    }
