"""Tests for the diagnostics download of a Glowrium config entry.

The download is a file people attach to public issues, and nearly everything
that could go into it is chosen by the lamp. Most of these tests are about
what must not come out of it, whatever the lamp reports.
"""

from datetime import datetime, timedelta, timezone
import inspect
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_ADDRESS, CONF_MODEL_ID
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.glowrium import (
    cbor,
    coordinator as coordinator_module,
    diagnostics,
    link as link_module,
)
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

from .lamp import ScriptedLamp, in_range, link_of, nothing_heard, turn_over

ADDRESS = "AA:BB:CC:DD:EE:FF"
TITLE = "Glowrium-G7_DDEEFF"
LATITUDE, LONGITUDE = 12.3456, 65.4321
SERIAL = "CST-0001"
# The host's clock when the lamp reported, with the lamp's a minute behind
# it; and the host's clock five minutes later, when the file is made. Not in
# UTC: the lamp keeps local wall-clock time, and it is the host's local time
# that it is set against.
HEARD_AT = datetime(2026, 10, 5, 8, 16, 30, tzinfo=timezone(timedelta(hours=3)))
HOST_NOW = HEARD_AT + timedelta(minutes=5)
CLOCK = bytes.fromhex("07ea0a05080f1e")  # 2026-10-05 08:15:30
CLOCK_RIGHT = bytes.fromhex("07ea0a0508101e")  # 2026-10-05 08:16:30
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
    with patch(
        "custom_components.glowrium.coordinator.dt_util.now", return_value=HEARD_AT
    ):
        _report(coordinator)
    await hass.async_block_till_done()
    return entry


def _report(coordinator: Any) -> None:
    """Have the lamp report a state, as it does when it is asked for one."""
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


async def _downloaded(
    hass: HomeAssistant, entry: MockConfigEntry, at: datetime = HOST_NOW
) -> dict[str, Any]:
    """Return the diagnostics as the file a user would attach, made at ``at``.

    Through plain JSON and back: the download is a JSON file, and bytes or a
    set left in it would fail there rather than here.
    """
    with patch("custom_components.glowrium.diagnostics.dt_util.now", return_value=at):
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
        "0x05 clock": {"ahead_of_this_host_by_seconds": -60, "as_of_seconds_ago": 300},
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
        "properties_not_kept": 0,
    }
    assert data["link"]["connected"] is False
    assert data["link"]["reports"] == 1
    assert data["link"]["unanswered_hang_ups"] == 0


@pytest.mark.parametrize("later", [0, 300, 3600, 9840])
async def test_the_clock_is_judged_at_the_moment_it_was_reported(
    hass: HomeAssistant, later: int
) -> None:
    """How far a clock is off is a fact about the moment it was read at.

    The mirror holds the clock the lamp last reported, and that can be hours
    old: the lamp reports it when it is asked for its state, which a link
    that goes on talking is not asked again, and a lamp that is out of reach
    cannot be. Set against the host's clock at the time of the download, a
    clock that was exactly right read as slow by however long ago it had been
    reported. It is set against the host's clock of that moment instead, and
    the file says how long ago that was.
    """
    entry = await _a_lamp_that_has_reported(hass)
    with patch(
        "custom_components.glowrium.coordinator.dt_util.now", return_value=HEARD_AT
    ):
        entry.runtime_data._ingest(cbor.encode({KEY_TIME: CLOCK_RIGHT}))

    data = await _downloaded(hass, entry, HEARD_AT + timedelta(seconds=later))

    assert data["state"]["0x05 clock"] == {
        "ahead_of_this_host_by_seconds": 0,
        "as_of_seconds_ago": later,
    }


async def test_a_report_without_a_clock_does_not_make_the_clock_newer(
    hass: HomeAssistant,
) -> None:
    """The moment kept is the clock's own, not the last time the lamp spoke."""
    entry = await _a_lamp_that_has_reported(hass)
    with patch(
        "custom_components.glowrium.coordinator.dt_util.now",
        return_value=HEARD_AT + timedelta(minutes=4),
    ):
        entry.runtime_data._ingest(cbor.encode({KEY_POWER: False}))

    data = await _downloaded(hass, entry)

    assert data["state"]["0x05 clock"] == {
        "ahead_of_this_host_by_seconds": -60,
        "as_of_seconds_ago": 300,
    }


async def test_a_clock_the_integration_set_is_judged_from_when_it_set_it(
    hass: HomeAssistant,
) -> None:
    """The clock in the mirror also changes when the integration corrects it.

    The write is echoed into the mirror, and what is there afterwards is the
    host's own time of that moment - not a clock that was reported when the
    one before it was.
    """
    entry = await _a_lamp_that_has_reported(hass)
    coordinator = entry.runtime_data
    coordinator._mirror.echo({KEY_TIME: bytes.fromhex("07e80101000000")})  # far off
    link = await ScriptedLamp().dial(MagicMock())  # a link to write the clock on
    with patch(
        "custom_components.glowrium.coordinator.dt_util.now",
        return_value=HEARD_AT + timedelta(minutes=2),
    ):
        await coordinator._async_sync_clock_if_needed(turn_over(coordinator, link))

    data = await _downloaded(hass, entry)

    assert data["state"]["0x05 clock"] == {
        "ahead_of_this_host_by_seconds": 0,
        "as_of_seconds_ago": 180,
    }


def test_a_clock_with_no_record_of_when_it_came_is_not_guessed_at() -> None:
    """Without the moment it was read at, a clock is neither right nor wrong.

    No path of the integration makes one: the mirror dates every clock it
    takes or echoes. The reader is held to it all the same, on its own.
    """
    assert diagnostics._clock(CLOCK, None) == {
        "ahead_of_this_host_by_seconds": None,
        "as_of_seconds_ago": None,
    }


async def test_the_download_does_not_say_where_the_lamp_is_or_which_one_it_is(
    hass: HomeAssistant,
) -> None:
    """A file meant to be attached to a public issue carries nothing private.

    The lamp stores the home's coordinates, and it has a serial number and an
    address that single it out. That the coordinates were reported can be
    seen; what they are cannot, and neither the serial nor the address is
    anywhere in what the integration writes. Nor are the bytes of the clock:
    it is given as how far it is from this host's, which is what a report
    needs.
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
    entry.runtime_data._mirror.echo(
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
        (KEY_TIMER, SCHEDULE + b"\x00"),  # every field in place, and one byte more
        (KEY_TIMER, bytes.fromhex("0100000019001200640000")),  # starts at 25:00
        (KEY_TIMER, bytes.fromhex("0100000006001200650000")),  # 101 %
        (KEY_TIMER, bytes.fromhex("0200000006001200640000")),  # neither on nor off
        (KEY_TIMER, bytes.fromhex("010000000600120064ffff")),  # an 18-hour fade
        (KEY_RAMP, bytes.fromhex("ffff")),
        (KEY_RAMP, bytes.fromhex("0e1000")),
        (KEY_DST, True),
        (KEY_DST, bytes.fromhex("0200000e10")),
        (KEY_DST, bytes.fromhex("0100000e1000")),  # in place, and one byte more
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
    with patch(
        "custom_components.glowrium.coordinator.dt_util.now", return_value=HEARD_AT
    ):
        entry.runtime_data._mirror.echo({key: value})  # dated as the report was

    data = await _downloaded(hass, entry)

    assert data["state"][NAMES[key]] == "not as expected"
    _nothing_private_in(data, UNKNOWN_TEXT, CURVE.hex(), "ddeeff", "eeff")


@pytest.mark.parametrize(
    ("key", "value", "read_out_as"),
    [
        (
            KEY_TIME,
            CLOCK,
            {"ahead_of_this_host_by_seconds": -60, "as_of_seconds_ago": 300},
        ),
        (
            KEY_TIME,
            bytes.fromhex("07ea0a05081c1e"),  # 08:28:30
            {"ahead_of_this_host_by_seconds": 720, "as_of_seconds_ago": 300},
        ),
        (
            KEY_TIME,
            bytes.fromhex("07e80101000000"),  # set in 2024 and never again
            {
                "ahead_of_this_host_by_seconds": "more than a year off",
                "as_of_seconds_ago": 300,
            },
        ),
        (
            KEY_TIME,
            bytes.fromhex("07ec0101000000"),  # as far ahead as that one is behind
            {
                "ahead_of_this_host_by_seconds": "more than a year off",
                "as_of_seconds_ago": 300,
            },
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
    with patch(
        "custom_components.glowrium.coordinator.dt_util.now", return_value=HEARD_AT
    ):
        entry.runtime_data._mirror.echo({key: value})  # dated as the report was

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


async def test_the_file_says_how_many_properties_were_not_kept(
    hass: HomeAssistant,
) -> None:
    """A lamp that reports more than the mirror keeps shows in the file as a number.

    Not which ids and not what was under them: the file repeats nothing
    after the lamp. That there were some is what a reader of the file needs:
    it is a model nobody has met, or not a lamp. The mirror has room for
    sixty-four ids nobody named; the rest is counted.
    """
    entry = await _a_lamp_that_has_reported(hass)
    coordinator = entry.runtime_data
    for first in range(1000, 1200, 50):
        coordinator._ingest(cbor.encode(dict.fromkeys(range(first, first + 50), True)))

    data = await _downloaded(hass, entry)

    assert data["state"]["other_properties"] == 64
    assert data["state"]["properties_not_kept"] == 136


async def test_a_lamp_that_has_reported_nothing_makes_a_file_all_the_same(
    hass: HomeAssistant,
) -> None:
    """The file is asked for when things do not work, which is often before."""
    entry = await _a_lamp_that_has_reported(hass)
    nothing_heard(entry.runtime_data)
    entry.runtime_data.device_info = {}

    data = await _downloaded(hass, entry)

    assert data["state"] == {"other_properties": 0, "properties_not_kept": 0}
    assert data["device"] == {
        "model": "Glowrium G7",  # remembered from an earlier session
        "model_id": "Glowrium-C051",
        "firmware": None,
        "device_info_fields": 0,
    }


_NOW = 5000.0  # the coordinator's monotonic clock, held still
_LINK_AT_REST = {
    "available": False,
    "advertising": False,
    "connected": False,
    "primed": False,
    "client": None,
    "reports": 1,
    "writes_sent": 0,
    "seconds_since_last_answer": 0,
    "state_request_refusals": 0,
    "state_request_paused": False,
    "unanswered_hang_ups": 0,
    "dials_held_back": False,
    "clients_that_would_not_close": 0,
}


async def _hold(
    coordinator: Any, *, connected: bool = True, primed: bool = False
) -> None:
    """Have the coordinator take a link of a scripted lamp: bare, or primed.

    What is behind the lamp's link is named by its class, as the file names
    a client.
    """
    lamp = ScriptedLamp()
    lamp.answers()
    with in_range(lamp):
        if primed:
            await link_of(coordinator).connect()  # and the first exchange on it
        else:
            async with link_of(coordinator).lock:
                await link_of(coordinator).open()  # taken bare, nothing asked
    lamp.links[0].is_connected = connected


@pytest.mark.parametrize(
    ("arrange", "differs"),
    [
        pytest.param(lambda _: None, {}, id="at rest"),
        pytest.param(
            lambda c: link_of(c).begin(present=True),
            {"advertising": True, "available": True},
            id="heard advertising",
        ),
        pytest.param(
            _hold,
            {"connected": True, "available": True, "client": "NothingBehind"},
            id="a link nobody has primed",
        ),
        pytest.param(
            lambda c: _hold(c, primed=True),
            {
                "connected": True,
                "available": True,
                "client": "NothingBehind",
                "primed": True,
                "reports": 2,  # the lamp answered the first exchange
            },
            id="a primed link",
        ),
        pytest.param(
            lambda c: _hold(c, connected=False),
            {"client": "NothingBehind"},
            id="a client that says it is not connected",
        ),
        pytest.param(_report, {"reports": 2}, id="a second report"),
        pytest.param(
            lambda c: setattr(c, "_writes_sent", 3), {"writes_sent": 3}, id="writes"
        ),
        pytest.param(
            lambda c: setattr(link_of(c), "last_answer", _NOW - 42),
            {"seconds_since_last_answer": 42},
            id="silence",
        ),
        pytest.param(
            lambda c: setattr(c, "_state_request_failures", 1),
            {"state_request_refusals": 1},
            id="a refusal",
        ),
        pytest.param(
            lambda c: setattr(c, "_state_request_muted_until", _NOW + 60),
            {"state_request_paused": True},
            id="the state request paused",
        ),
        pytest.param(
            lambda c: setattr(c, "_state_request_given_up", True),
            {"state_request_paused": True},
            id="the state request given up",
        ),
        pytest.param(
            lambda c: setattr(link_of(c), "stuck_hang_ups", 2),
            {"unanswered_hang_ups": 2},
            id="hang-ups left unanswered",
        ),
        pytest.param(
            lambda c: setattr(link_of(c), "dial_not_before", _NOW + 60),
            {"dials_held_back": True},
            id="dials held back",
        ),
        pytest.param(
            lambda c: link_of(c).unclosed.add(object()),
            {"clients_that_would_not_close": 1},
            id="a client that would not close",
        ),
    ],
)
async def test_each_field_of_the_link_section_follows_its_own_source(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    arrange: Any,
    differs: dict[str, Any],
) -> None:
    """The link is described in full, and by nothing the lamp chose.

    Every field is a count or a flag the integration keeps. The section is
    passed through whole from the coordinator's own description, so it is
    held here to an exact list - a field added there arrives here, and has to
    be looked at - and each field is moved on its own: two states that
    differ in everything at once cannot tell a field from a copy of its
    neighbour. The client is named by its class, which says whether BlueZ or
    a proxy is behind the link, and is not printed: a client prints with the
    address in it.
    """
    entry = await _a_lamp_that_has_reported(hass)
    coordinator = entry.runtime_data
    monkeypatch.setattr(coordinator_module, "monotonic", lambda: _NOW)
    monkeypatch.setattr(link_module, "monotonic", lambda: _NOW)
    link_of(coordinator).last_answer = _NOW
    arranged = arrange(coordinator)
    if inspect.isawaitable(arranged):
        await arranged

    data = await _downloaded(hass, entry)
    link_of(coordinator).unclosed.clear()

    assert data["link"] == _LINK_AT_REST | differs
    _nothing_private_in(data)
