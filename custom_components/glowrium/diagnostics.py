"""Diagnostics for the Glowrium integration.

One download that answers what a bug report is otherwise asked for piece by
piece: the model and firmware, what the lamp last reported, and where the link
stands.

It is meant to be attached to a public issue, and nearly all of what could go
into it was chosen by the lamp: which properties it reports and what it puts
under them, which fields its device-info string has, what they are called and
where one ends. So the file repeats nothing after the lamp. Each property the
integration knows is read by the reader the entities and the commands go
through (``protocol``), and written out from that reading - a schedule as its
times, a clock as how far it was from this host's when it came. What that
reader cannot use is said to be there and not as expected: the file and the
device page judge a value alike. What has no name here is counted. No value,
no size and no id that the lamp picked goes into the file as the lamp gave it.

That holds for what this module returns. Home Assistant wraps it in a header
of its own (version, time zone, the names of the custom integrations) and adds
the ids of the integration's open repairs - which is why the coordinator files
its repair under the config entry's id and not under the lamp's address.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Final

from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import CONF_MODEL_ID
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import GlowriumConfigEntry, identity, protocol
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
)

# Past this the clock is simply wrong, and by how much says nothing more.
_FAR_OFF: Final = timedelta(days=366)
_NOT_AS_EXPECTED: Final = "not as expected"


def _as_it_is(value: Any) -> Any:
    """Word a flag or an index as itself: one of very few things it can be."""
    return value


def _whole(level: float) -> int:
    """Word a brightness as the whole level it is nearest to.

    A level need not be a whole number, and a number with a dozen digits
    under this id may be anything: the file repeats no float of the lamp's
    choosing.
    """
    return round(level)


def _clock(shown: datetime, heard_at: datetime | None) -> dict[str, Any]:
    """Word the device clock as how far it was from this host's when it came.

    Not as the time it shows: how far off it is is what a report needs, and
    it is a reading of the bytes rather than a copy of them.

    And not against the host's clock of now. The mirror holds the clock the
    lamp last reported, which can be hours old - a link that goes on talking
    is not asked for it again, and a lamp out of reach cannot be - and a
    clock that was exactly right then would read as slow by all of that. It
    is set against the host's clock of the moment it came (``heard_at``), and
    how long ago that was is said next to it.
    """
    if heard_at is None:  # the mirror dates every clock it takes: a guard
        return {"ahead_of_this_host_by_seconds": None, "as_of_seconds_ago": None}
    # Naive, like the clock itself: the lamp keeps local wall-clock time.
    ahead = shown - heard_at.replace(tzinfo=None)
    return {
        "ahead_of_this_host_by_seconds": (
            "more than a year off"
            if abs(ahead) > _FAR_OFF
            else round(ahead.total_seconds())
        ),
        "as_of_seconds_ago": round((dt_util.now() - heard_at).total_seconds()),
    }


def _schedule(slot: protocol.Schedule) -> dict[str, Any]:
    """Word the schedule slot as its fields.

    The three bytes nobody has decoded are not among them.
    """
    return {
        "enabled": slot.enabled,
        "start": slot.start.isoformat("minutes"),
        "end": slot.end.isoformat("minutes"),
        "brightness": slot.brightness,
        "fade_seconds": slot.fade_seconds,
    }


def _ramp(seconds: int) -> dict[str, Any]:
    """Word the ramp: its seconds."""
    return {"seconds": seconds}


def _dst(slot: protocol.DaylightSaving) -> dict[str, Any]:
    """Word the daylight-saving slot: its flag and its offset in seconds."""
    return {"enabled": slot.enabled, "offset_seconds": slot.offset_seconds}


# The properties that are read out, by id, with how the file words the value
# of each. What the property is called and what its value is are not said
# here: both are ``protocol.PROPERTIES``, the table the entities and the log
# go by, so that the file and the device page cannot disagree about a value.
# (The clock is read too, and takes more than its value to word: see _state.)
_READ: Final[dict[int, Callable[[Any], Any]]] = {
    KEY_POWER: _as_it_is,
    KEY_BRIGHTNESS: _whole,
    KEY_CIRCADIAN: _as_it_is,
    KEY_SCHEDULE: _as_it_is,
    KEY_TIMER: _schedule,
    KEY_ACTIVATED: _as_it_is,
    KEY_INDICATOR: _as_it_is,
    KEY_LIGHTING_MODE: _as_it_is,
    KEY_RAMP: _ramp,
    KEY_DST: _dst,
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
    # Worded with the moment it came, which no other property needs. Each
    # wording is handed what its own reader returned, once that is known to
    # be a value: the table pairs them by id, which no type can say.
    clock: Callable[[Any], Any] = partial(_clock, heard_at=clock_heard_at)
    for key, word in ({KEY_TIME: clock} | _READ).items():
        name, read = protocol.PROPERTIES[key]
        reading = read(reported)
        if reading is None:  # nothing was reported under the id
            continue
        shown[f"0x{key:02x} {name}"] = (
            _NOT_AS_EXPECTED if reading is protocol.UNUSABLE else word(reading)
        )
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
