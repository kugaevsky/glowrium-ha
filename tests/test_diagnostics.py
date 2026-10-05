"""Tests for the diagnostics download of a Glowrium config entry.

The download is a file people attach to public issues, and nearly everything
that could go into it is chosen by the lamp. Most of these tests are about
what must not come out of it, whatever the lamp reports.
"""

from datetime import datetime
import json
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_ADDRESS, CONF_MODEL_ID
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.glowrium import cbor
from custom_components.glowrium.const import (
    DOMAIN,
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
from custom_components.glowrium.coordinator import _parse_device_info
from custom_components.glowrium.diagnostics import async_get_config_entry_diagnostics

ADDRESS = "AA:BB:CC:DD:EE:FF"
TITLE = "Glowrium-G7_DDEEFF"
LATITUDE, LONGITUDE = 41.3166, 69.2906
SERIAL = "CST-0001"
# The host's clock while the file is made, and the lamp's a minute behind it.
HOST_NOW = datetime(2026, 10, 5, 8, 16, 30, tzinfo=dt_util.UTC)
CLOCK = bytes.fromhex("07ea0a05080f1e")  # 2026-10-05 08:15:30
SCHEDULE = bytes.fromhex("0100000006001200640000")  # 06:00-18:00, 100 %
CURVE = bytes.fromhex("000041dc0000489400004948")  # 04:41, 05:10, 05:13 ...
UNKNOWN_TEXT = "HomeNetwork-5G"
PRIVATE = (ADDRESS, ADDRESS.lower(), "AABBCCDDEEFF", TITLE, "DDEEFF", SERIAL)

NAMES = {
    KEY_TIME: "0x05 clock",
    KEY_POWER: "0x06 power",
    KEY_BRIGHTNESS: "0x08 brightness",
    KEY_CIRCADIAN: "0x09 circadian",
    KEY_SCHEDULE: "0x0d schedule mode",
    KEY_TIMER: "0x11 schedule",
    KEY_ACTIVATED: "0x14 activated",
    KEY_INDICATOR: "0x17 indicator",
    KEY_LIGHTING_MODE: "0x2b lighting mode",
    KEY_RAMP: "0x2f ramp",
    KEY_DST: "0x35 daylight saving",
}


async def _a_lamp_that_has_reported(hass: HomeAssistant) -> MockConfigEntry:
    """Set the entry up with the radio stubbed out and a state in the mirror."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TITLE,
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_MODEL_ID: "Glowrium-C051"},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.glowrium.coordinator.GlowriumCoordinator.async_start",
        new_callable=AsyncMock,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data
    coordinator.device_info = {
        "brand": "INLEDCO",
        "pkey": "Glowrium-C051",
        "devid": SERIAL,
        "mac": ADDRESS,
        "version": "4",
    }
    coordinator._ingest(
        cbor.encode(
            {
                KEY_TIME: CLOCK,
                KEY_POWER: True,
                KEY_BRIGHTNESS: 70,
                KEY_LATITUDE: LATITUDE,
                KEY_LONGITUDE: LONGITUDE,
                KEY_TIMER: SCHEDULE,
                KEY_LIGHTING_MODE: 5,
                KEY_RAMP: bytes.fromhex("0e10"),
                KEY_DST: bytes.fromhex("0100000e10"),
            }
        )
    )
    await hass.async_block_till_done()
    return entry


async def _downloaded(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Return the diagnostics as the file a user would attach.

    Through plain JSON and back: the download is a JSON file, and bytes or a
    set left in it would fail there rather than here.
    """
    with patch(
        "custom_components.glowrium.diagnostics.dt_util.now", return_value=HOST_NOW
    ):
        made = await async_get_config_entry_diagnostics(hass, entry)
    return json.loads(json.dumps(made))


def _nothing_private_in(data: dict[str, Any], *more: str) -> None:
    text = json.dumps(data)
    for private in (*PRIVATE, str(LATITUDE), str(LONGITUDE), *more):
        assert private not in text, private


async def test_the_download_says_what_the_lamp_reported(hass: HomeAssistant) -> None:
    """One file answers what a bug report otherwise has to be asked for.

    The model and firmware, what the lamp last reported - read out, each
    property under its id and the name it has here - and where the link
    stands.
    """
    entry = await _a_lamp_that_has_reported(hass)

    data = await _downloaded(hass, entry)

    assert data["device"] == {
        "model": "Glowrium G7",
        "model_id": "Glowrium-C051",
        "firmware": "4",
        "device_info_fields": 5,
    }
    assert data["state"] == {
        "0x05 clock": {"ahead_of_this_host_by_seconds": -60},
        "0x06 power": True,
        "0x08 brightness": 70,
        "0x0a 0x0b coordinates": "**REDACTED**",
        "0x11 schedule": {
            "enabled": True,
            "start": "06:00",
            "end": "18:00",
            "brightness": 100,
            "fade_seconds": 0,
        },
        "0x2b lighting mode": 5,
        "0x2f ramp": {"seconds": 3600},
        "0x35 daylight saving": {"enabled": True, "offset_seconds": 3600},
        "other_properties": 0,
    }
    assert data["link"]["connected"] is False
    assert data["link"]["reports"] == 1
    assert data["link"]["unanswered_hang_ups"] == 0


async def test_the_download_does_not_say_where_the_lamp_is_or_which_one_it_is(
    hass: HomeAssistant,
) -> None:
    """A file meant to be attached to a public issue carries nothing private.

    The lamp stores the home's coordinates, and it has a serial number and an
    address that single it out. That the coordinates were reported can be
    seen; what they are cannot, and neither the serial nor the address is
    anywhere in the file. Nor is the time: the lamp's clock is given as how
    far it is from this host's, which is what a report needs and says
    nothing about the time zone the host is in.
    """
    entry = await _a_lamp_that_has_reported(hass)

    data = await _downloaded(hass, entry)

    assert data["state"]["0x0a 0x0b coordinates"] == "**REDACTED**"
    _nothing_private_in(data, CLOCK.hex(), "2026", "08:15")


async def test_of_the_entry_only_what_was_chosen_is_shown(hass: HomeAssistant) -> None:
    """The entry is described field by field, not copied with fields removed.

    Home Assistant adds to what a config entry holds from release to release,
    and several of its fields are the lamp's address under another name.
    """
    entry = await _a_lamp_that_has_reported(hass)

    data = await _downloaded(hass, entry)

    assert data["entry"] == {
        "source": "user",
        "version": 1,
        "minor_version": 1,
        "disabled_by": None,
        "remembered_model_id": "Glowrium-C051",
    }


async def test_what_the_integration_cannot_name_is_only_counted(
    hass: HomeAssistant,
) -> None:
    """Nothing the lamp chose is repeated: not a value, not a size, not an id.

    The lamp decides which properties it reports, so a list of the two ids
    that hold the coordinates is not a guard: the sunrise and sunset times it
    computes from them give the place away just as well, and a property
    nobody has decoded could hold anything. An id is a number the lamp picks
    and so is a length. What has no name here is counted, and that is all.
    """
    entry = await _a_lamp_that_has_reported(hass)
    before = (await _downloaded(hass, entry))["state"]
    entry.runtime_data.state.update(
        {
            0x34: CURVE,
            0x77: UNKNOWN_TEXT,
            0x78: [1, CURVE],
            0xAABBCCDDEEFF: 1,  # an "id" that is somebody's address
            0x1234: UNKNOWN_TEXT,
        }
    )

    data = await _downloaded(hass, entry)

    assert data["state"] == before | {"other_properties": 5}
    _nothing_private_in(data, CURVE.hex(), UNKNOWN_TEXT, "aabbccddeeff", "0x34")


async def test_the_fields_of_the_device_info_string_are_only_counted(
    hass: HomeAssistant,
) -> None:
    """What a field is called is the lamp's choice as much as what is in it.

    The model id and the firmware are looked for by name and shown once,
    above. Every field is counted and none is listed: split on a colon, a
    field written `mac=AA:BB:...` has half an address for a name.
    """
    entry = await _a_lamp_that_has_reported(hass)
    entry.runtime_data.device_info = _parse_device_info(
        f"pkey:Glowrium-C051;version:4;mac={ADDRESS};{SERIAL}:x;new:{UNKNOWN_TEXT}".encode()
    )

    data = await _downloaded(hass, entry)

    assert data["device"] == {
        "model": "Glowrium G7",
        "model_id": "Glowrium-C051",
        "firmware": "4",
        "device_info_fields": 5,
    }
    _nothing_private_in(data, UNKNOWN_TEXT, "mac=AA")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (KEY_POWER, UNKNOWN_TEXT),
        (KEY_POWER, 1),
        (KEY_BRIGHTNESS, LATITUDE),
        (KEY_BRIGHTNESS, 101),
        (KEY_BRIGHTNESS, True),
        (KEY_LIGHTING_MODE, 0xDDEEFF),
        # Six bytes of address and one more are seven bytes, like a clock.
        (KEY_TIME, bytes.fromhex("aabbccddeeff01")),
        (KEY_TIME, bytes.fromhex("07ea0d05080f1e")),  # month 13
        (KEY_TIME, bytes.fromhex("07ea0a05180f1e")),  # hour 24
        (KEY_TIME, CLOCK + b"\x00"),
        (KEY_TIMER, CURVE + bytes(28)),
        (KEY_TIMER, bytes.fromhex("0100000019001200640000")),  # starts at 25:00
        (KEY_TIMER, bytes.fromhex("0100000006001200650000")),  # 101 %
        (KEY_TIMER, bytes.fromhex("0200000006001200640000")),  # neither on nor off
        (KEY_TIMER, bytes.fromhex("010000000600120064ffff")),  # an 18-hour fade
        (KEY_RAMP, bytes.fromhex("ffff")),
        (KEY_RAMP, bytes.fromhex("0e1000")),
        (KEY_DST, True),
        (KEY_DST, bytes.fromhex("0200000e10")),
        (KEY_DST, bytes.fromhex("01ddeeff10")),
        (KEY_INDICATOR, [True]),
        (KEY_ACTIVATED, None),
    ],
)
async def test_a_known_property_is_read_out_only_when_it_is_what_its_name_means(
    hass: HomeAssistant, key: int, value: Any
) -> None:
    """Knowing a property's name is not knowing what a lamp puts under it.

    The power flag is a boolean, the clock a date and a time, the schedule
    eleven bytes whose hours are hours. A lamp - another model, another
    firmware, a faulty one - that reports something else under the same id
    is reporting something nobody has looked at. It is said to be there and
    not what was expected, and nothing of it is repeated.
    """
    entry = await _a_lamp_that_has_reported(hass)
    entry.runtime_data.state[key] = value

    data = await _downloaded(hass, entry)

    assert data["state"][NAMES[key]] == "not as expected"
    _nothing_private_in(data, UNKNOWN_TEXT, CURVE.hex(), "ddeeff", "eeff")


@pytest.mark.parametrize(
    ("key", "value", "read_out_as"),
    [
        (KEY_TIME, CLOCK, {"ahead_of_this_host_by_seconds": -60}),
        (
            KEY_TIME,
            bytes.fromhex("07ea0a05081c1e"),  # 08:28:30
            {"ahead_of_this_host_by_seconds": 720},
        ),
        (
            KEY_TIME,
            bytes.fromhex("07e80101000000"),  # set in 2024 and never again
            {"ahead_of_this_host_by_seconds": "more than a year off"},
        ),
        (
            KEY_TIMER,
            # As a G8 reports it, with bytes the G7 leaves at zero.
            bytes.fromhex("010200ff0a121212640000"),
            {
                "enabled": True,
                "start": "10:18",
                "end": "18:18",
                "brightness": 100,
                "fade_seconds": 0,
            },
        ),
        (
            KEY_TIMER,
            bytes.fromhex("00000000173b0000000708"),
            {
                "enabled": False,
                "start": "23:59",
                "end": "00:00",
                "brightness": 0,
                "fade_seconds": 1800,
            },
        ),
        (KEY_RAMP, bytes.fromhex("0000"), {"seconds": 0}),
        (KEY_RAMP, bytes.fromhex("1c20"), {"seconds": 7200}),
        (
            KEY_DST,
            bytes.fromhex("0000000708"),
            {"enabled": False, "offset_seconds": 1800},
        ),
        (KEY_BRIGHTNESS, 0, 0),
        (KEY_BRIGHTNESS, 100, 100),
        (KEY_LIGHTING_MODE, 32, 32),
        (KEY_POWER, False, False),
        (KEY_CIRCADIAN, True, True),
        (KEY_SCHEDULE, False, False),
        (KEY_ACTIVATED, True, True),
        (KEY_INDICATOR, False, False),
    ],
)
async def test_what_checks_out_is_read_out(
    hass: HomeAssistant, key: int, value: Any, read_out_as: Any
) -> None:
    """The file is rebuilt from what was understood, not copied from the lamp.

    Each property is read the way the integration reads it and written out
    from that reading. The bytes of a schedule that nobody has decoded - a
    G8 sets three that a G7 leaves at zero - are therefore not in the file,
    and neither is anything else that merely happened to be the right length.
    """
    entry = await _a_lamp_that_has_reported(hass)
    entry.runtime_data.state[key] = value

    data = await _downloaded(hass, entry)

    assert data["state"][NAMES[key]] == read_out_as
    if isinstance(value, bytes):
        assert value.hex() not in json.dumps(data)


async def test_a_field_is_shown_only_if_it_is_what_it_claims_to_be(
    hass: HomeAssistant,
) -> None:
    """Where one field ends is the parser's opinion, and the lamp's to upset.

    The device-info string is split on semicolons. A lamp that separates its
    fields any other way hands over one long field with the serial number
    and the address inside it, and showing that field would show those.
    """
    entry = await _a_lamp_that_has_reported(hass)
    coordinator = entry.runtime_data
    glued = f"pkey:Glowrium-C051,devid:{SERIAL},mac:{ADDRESS};version:4 devid {SERIAL}"
    coordinator.device_info = _parse_device_info(glued.encode())
    assert SERIAL in coordinator.device_info["pkey"]  # the differential itself

    data = await _downloaded(hass, entry)

    assert data["device"]["model_id"] == "**REDACTED**"
    assert data["device"]["firmware"] == "**REDACTED**"
    _nothing_private_in(data)


@pytest.mark.parametrize(
    ("field", "shown_as", "value"),
    [
        ("pkey", "model_id", f"Glowrium-C051-{SERIAL}"),
        ("pkey", "model_id", "Glowrium-C051.AABBCCDDEEFF"),
        ("pkey", "model_id", "Glowrium-C051_AA-BB-CC-DD-EE-FF"),
        ("pkey", "model_id", "Glowrium-AABBCCDDEEFF"),
        ("pkey", "model_id", "Glowrium-C0510001"),  # a model code with more on it
        ("pkey", "model_id", "Glowrium-c051"),
        ("pkey", "model_id", SERIAL),  # a word, a dash, a code - of no family
        ("pkey", "model_id", "AABBCCDDEEFF"),
        ("version", "firmware", f"4-{SERIAL}"),
        ("version", "firmware", "4.AABBCCDDEEFF"),
        ("version", "firmware", "4_0001"),
        ("version", "firmware", "12345678"),  # a number, but no version is that long
        ("version", "firmware", "123.4"),
        ("version", "firmware", "1.2.3.4"),
    ],
)
async def test_a_field_is_held_to_its_own_shape_not_to_a_common_one(
    hass: HomeAssistant, field: str, shown_as: str, value: str
) -> None:
    """A dash or a dot can join two things as well as a comma can.

    One pattern for both fields had to let through whatever either may
    contain, and a model id contains a dash: a lamp that joined its fields
    with one would still have passed as a single word, and so would a serial
    number on its own. Each is held to what it is instead - a model id is the
    family's name, a dash, a letter and three digits; a version is one to
    three small numbers with dots between them.
    """
    entry = await _a_lamp_that_has_reported(hass)
    entry.runtime_data.device_info = {"pkey": "Glowrium-C051", "version": "4"} | {
        field: value
    }

    data = await _downloaded(hass, entry)

    assert data["device"][shown_as] == "**REDACTED**"
    assert value not in json.dumps(data)


async def test_the_fields_a_report_needs_are_shown_as_they_are(
    hass: HomeAssistant,
) -> None:
    """A model nobody here has seen, and its firmware, still come through."""
    entry = await _a_lamp_that_has_reported(hass)
    entry.runtime_data.device_info = {
        "brand": "INLEDCO",
        "pkey": "Glowrium-C064",
        "version": "2.10.3",
    }

    data = await _downloaded(hass, entry)

    assert data["device"] == {
        "model": "Glowrium",
        "model_id": "Glowrium-C064",
        "firmware": "2.10.3",
        "device_info_fields": 3,
    }


async def test_a_remembered_model_id_is_held_to_the_same(hass: HomeAssistant) -> None:
    """What was kept from an earlier session came from the lamp as well."""
    entry = await _a_lamp_that_has_reported(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_MODEL_ID: f"Glowrium-C051;devid:{SERIAL}"}
    )

    data = await _downloaded(hass, entry)

    assert data["entry"]["remembered_model_id"] == "**REDACTED**"
    _nothing_private_in(data)


async def test_a_lamp_that_has_reported_nothing_makes_a_file_all_the_same(
    hass: HomeAssistant,
) -> None:
    """The file is asked for when things do not work, which is often before."""
    entry = await _a_lamp_that_has_reported(hass)
    entry.runtime_data.state.clear()
    entry.runtime_data.device_info = {}

    data = await _downloaded(hass, entry)

    assert data["state"] == {"other_properties": 0}
    assert data["device"] == {
        "model": "Glowrium G7",  # remembered from an earlier session
        "model_id": "Glowrium-C051",
        "firmware": None,
        "device_info_fields": 0,
    }
