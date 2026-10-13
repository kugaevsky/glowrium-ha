"""Tests for the Glowrium coordinator's command encoding."""

import ast
import asyncio
from collections.abc import Callable
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from bleak.exc import BleakError, BleakGATTProtocolError, BleakGATTProtocolErrorCode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import pytest

from custom_components.glowrium import (
    cbor,
    const,
    coordinator as coordinator_module,
    diagnostics,
    keeper as keeper_module,
    link as link_module,
    protocol,
)
from custom_components.glowrium.const import (
    INFO_UUID,
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
    KNOWN_KEYS,
    NOTIFY_UUID,
    STATE_KEYS,
    WRITE_UUID,
)
from custom_components.glowrium.coordinator import (
    GlowriumCoordinator,
    _parse_device_info,
)

from .conftest import DST_OFF, DST_ON
from .lamp import ScriptedLamp, flood, keeper_of, lamp_of, link_of, turn_over
from .shared import _an_entry, _at_a_lamp, _holding_a_link, _info_lines, _kept_for


def _refusing(lamp: ScriptedLamp) -> None:
    """Make ``lamp`` one that refuses the state request and can be read instead.

    That is the shape of a model that refuses: issue #3 shows a G8 serving a
    twenty-key read while answering the request with an ATT error and dropping
    the link.
    """
    lamp.fails_writes(BleakError("Insufficient authorization (8)"))
    lamp.readable(NOTIFY_UUID, cbor.encode({KEY_POWER: True}))


@pytest.mark.parametrize(
    ("switch", "value", "frame"),
    [
        pytest.param("async_set_power", True, bytes.fromhex("a106f5"), id="power on"),
        pytest.param("async_set_power", False, bytes.fromhex("a106f4"), id="power off"),
        pytest.param(
            "async_set_indicator", True, bytes.fromhex("a117f5"), id="indicator on"
        ),
        pytest.param(
            "async_set_indicator", False, bytes.fromhex("a117f4"), id="indicator off"
        ),
        pytest.param(
            "async_set_dst", False, cbor.encode({KEY_DST: DST_OFF}), id="dst off"
        ),
    ],
)
async def test_switches_send_the_state_they_were_given(
    hass: HomeAssistant, switch: str, value: bool, frame: bytes
) -> None:
    """Each switch writes {key: value} as the captures show, in both polarities.

    Power is {6: bool} on the command characteristic and the indicator
    {0x17: bool}; daylight saving is the 0x35 slot, whose "on" is held with
    the offset it carries (test_the_dst_offset_the_lamp_reports_is_preserved).
    Each of these was once only ever exercised in one direction, so a
    command that ignored its argument and always turned the thing on looked
    correct. What was written is echoed into the mirror, as it was written.
    """
    coordinator, lamp = _at_a_lamp(hass)
    await getattr(coordinator, switch)(value)
    assert lamp.written == [(WRITE_UUID, frame)]
    ((key, sent),) = cbor.decode(frame).items()
    echoed = coordinator.state[key]
    assert echoed == sent
    assert type(echoed) is type(sent)


@pytest.mark.parametrize(
    ("asked", "sent", "frame"),
    [
        pytest.param(150, 100, bytes.fromhex("a1081864"), id="above the top"),
        pytest.param(-20, 0, cbor.encode({KEY_BRIGHTNESS: 0}), id="below the bottom"),
    ],
)
async def test_brightness_is_clamped_at_both_ends(
    hass: HomeAssistant, asked: int, sent: int, frame: bytes
) -> None:
    """Brightness is clamped to 0..100 and encoded as {8: n}.

    Only the upper clamp was pinned at first; a missing lower one sends a
    negative.
    """
    coordinator, lamp = _at_a_lamp(hass)
    await coordinator.async_set_brightness(asked)
    assert lamp.written == [(WRITE_UUID, frame)]
    assert coordinator.state[KEY_BRIGHTNESS] == sent


async def test_set_light_state_batches(hass: HomeAssistant) -> None:
    """Power + brightness go out as a single CBOR map ({6: bool, 8: n})."""
    coordinator, lamp = _at_a_lamp(hass)
    await coordinator.async_set_light_state(True, 25)
    assert lamp.written == [(WRITE_UUID, bytes.fromhex("a206f5081819"))]
    assert coordinator.state[KEY_POWER] is True
    assert coordinator.state[KEY_BRIGHTNESS] == 25
    # Turning off carries no brightness key.
    lamp.written.clear()
    await coordinator.async_set_light_state(False)
    assert lamp.written == [(WRITE_UUID, bytes.fromhex("a106f4"))]


async def test_set_lighting_mode_matches_capture(hass: HomeAssistant) -> None:
    """Lighting-mode selection matches the captured command frame."""
    coordinator, lamp = _at_a_lamp(hass)
    await coordinator.async_set_lighting_mode(5)
    assert lamp.written == [
        (WRITE_UUID, bytes.fromhex("a4182b05182c4202d0182f420e10183242001e"))
    ]
    assert coordinator.state[KEY_LIGHTING_MODE] == 5


async def test_set_ramp_preserves_mode(hass: HomeAssistant) -> None:
    """Ramp re-sends the current lighting mode with a new 0x2f (30 min)."""
    coordinator, lamp = _at_a_lamp(hass)
    coordinator._mirror.echo({KEY_LIGHTING_MODE: 1})
    await coordinator.async_set_ramp(30)
    assert lamp.written == [
        (WRITE_UUID, bytes.fromhex("a4182b01182c4202d0182f420708183242001e"))
    ]


@pytest.mark.parametrize(
    ("mode", "circadian", "schedule", "frames"),
    [
        pytest.param(
            "circadian",
            True,
            False,
            (bytes.fromhex("a10df4"), bytes.fromhex("a109f5")),
            id="circadian",
        ),
        pytest.param(
            "schedule",
            False,
            True,
            (cbor.encode({KEY_CIRCADIAN: False}), cbor.encode({KEY_SCHEDULE: True})),
            id="schedule",
        ),
        pytest.param(
            "manual",
            False,
            False,
            (cbor.encode({KEY_CIRCADIAN: False}), cbor.encode({KEY_SCHEDULE: False})),
            id="manual",
        ),
    ],
)
async def test_every_operating_mode_sets_both_flags(
    hass: HomeAssistant,
    mode: str,
    circadian: bool,
    schedule: bool,
    frames: tuple[bytes, bytes],
) -> None:
    """The two flags are mutually exclusive, so each mode writes both, one frame each.

    Circadian clears schedule (0x0d) and sets circadian (0x09): the captured
    frames. Only Circadian was covered at first; a Manual that wrote
    nothing, or a Schedule that set circadian, would have left the lamp in
    the wrong mode silently. Both flags are echoed into the mirror.
    """
    coordinator, lamp = _at_a_lamp(hass)
    await coordinator.async_set_operating_mode(mode)
    assert len(lamp.written) == 2
    for frame in frames:
        assert (WRITE_UUID, frame) in lamp.written
    assert coordinator.state[KEY_CIRCADIAN] is circadian
    assert coordinator.state[KEY_SCHEDULE] is schedule


async def test_circadian_reapplies_ramp(hass: HomeAssistant) -> None:
    """Entering Circadian re-applies the user's ramp (the device resets it)."""
    coordinator, lamp = _at_a_lamp(hass)
    coordinator._mirror.echo({KEY_LIGHTING_MODE: 1})
    await coordinator.async_set_ramp(90)  # 90 min = 5400 s = 0x1518
    lamp.written.clear()
    await coordinator.async_set_operating_mode("circadian")
    # {0x0d: False}, {0x09: True}, then the mode payload re-applying the ramp.
    assert len(lamp.written) == 3
    payload = cbor.decode(lamp.written[-1][1])
    assert payload[0x2F] == bytes.fromhex("1518")


async def test_operating_mode_property(hass: HomeAssistant) -> None:
    """operating_mode is None until read, then reflects circadian/schedule keys."""
    coordinator, _ = _at_a_lamp(hass)
    assert coordinator.operating_mode is None  # state not read yet -> unknown
    coordinator._mirror.echo({KEY_CIRCADIAN: False})
    coordinator._mirror.echo({KEY_SCHEDULE: False})
    assert coordinator.operating_mode == "manual"  # both flags read as off
    coordinator._mirror.echo({KEY_CIRCADIAN: True})
    assert coordinator.operating_mode == "circadian"
    coordinator._mirror.echo({KEY_CIRCADIAN: False})
    coordinator._mirror.echo({KEY_SCHEDULE: True})
    assert coordinator.operating_mode == "schedule"


async def test_mode_allows_when_mode_unknown(hass: HomeAssistant) -> None:
    """mode_allows keeps mode entities available while the mode is unknown."""
    coordinator, _ = _at_a_lamp(hass)
    # Unknown mode (state not read) -> allowed for every mode, so nothing hides.
    assert coordinator.mode_allows("circadian") is True
    assert coordinator.mode_allows("schedule") is True
    # Once known, only the matching mode is allowed.
    coordinator._mirror.echo({KEY_CIRCADIAN: True})
    coordinator._mirror.echo({KEY_SCHEDULE: False})
    assert coordinator.mode_allows("circadian") is True
    assert coordinator.mode_allows("schedule") is False


@pytest.mark.parametrize(
    ("reported", "slot", "frame"),
    [
        pytest.param(
            None,
            DST_ON,
            bytes.fromhex("a11835450100000e10"),
            id="nothing reported yet",
        ),
        pytest.param(
            bytes.fromhex("0000000708"),  # flag off, offset 1800 s
            bytes.fromhex("0100000708"),
            cbor.encode({KEY_DST: bytes.fromhex("0100000708")}),
            id="a half-hour offset reported",
        ),
    ],
)
async def test_the_dst_offset_the_lamp_reports_is_preserved(
    hass: HomeAssistant, reported: bytes | None, slot: bytes, frame: bytes
) -> None:
    """DST writes {0x35: [enabled, offset]}: the flag asked for, the lamp's own offset.

    The slot is a flag plus an offset, written together. Sending a fixed
    3600 seconds turns half-hour daylight-saving regions - Lord Howe Island,
    and historically others - into a full hour the moment the switch is
    touched, while the lamp had been reporting the right value all along
    (issue #4). With nothing reported yet the near-universal hour is the
    default, and the frame is the captured one: enabled byte 01, offset
    3600 s. Unlike the schedule slot this carries one field rather than
    five, and refusing would leave the switch unusable until the lamp
    reports - so a default is the better trade here.
    """
    coordinator, lamp = _at_a_lamp(hass)
    if reported is None:
        assert KEY_DST not in coordinator.state
    else:
        coordinator._mirror.echo({KEY_DST: reported})

    await coordinator.async_set_dst(True)

    assert lamp.written == [(WRITE_UUID, frame)]
    assert cbor.decode(frame)[KEY_DST] == slot
    assert coordinator.state[KEY_DST] == slot


async def test_dst_enabled_property(hass: HomeAssistant) -> None:
    """The switch asks the coordinator, which knows nothing until the lamp says."""
    coordinator, _ = _at_a_lamp(hass)
    assert coordinator.dst_enabled is None
    await coordinator.async_set_dst(True)
    assert coordinator.dst_enabled is True
    await coordinator.async_set_dst(False)
    assert coordinator.dst_enabled is False


@pytest.mark.parametrize("zero", [0, 0.0], ids=["whole numbers", "floats"])
async def test_sync_location_refuses_a_home_that_was_never_set(
    hass: HomeAssistant, zero: float
) -> None:
    """Zero and zero is no position: nothing is written, and the user is told.

    Home Assistant holds a latitude and a longitude always, and both are zero
    when it was given neither - the whole number it starts out with, or the
    float a configuration that says zero is read as. Written, they are a
    place - where the equator meets the prime meridian - and the lamp works
    its sunrise and sunset out for it.
    """
    coordinator, lamp = _at_a_lamp(hass)
    keeper_of(coordinator).home = (zero, zero)
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_sync_location()
    assert err.value.translation_key == "home_location_not_set"
    assert err.value.translation_placeholders == {"name": "Glowrium-G7"}
    assert lamp.written == []
    assert KEY_LATITUDE not in coordinator.state
    assert KEY_LONGITUDE not in coordinator.state


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(12.3456, 65.4321), (0, 65.4321), (12.3456, 0), (0.25, -0.25)],
    ids=[
        "a home",
        "on the equator",
        "on the prime meridian",
        "a quarter of a degree off both",
    ],
)
async def test_sync_location_writes_any_home_but_zero_and_zero(
    hass: HomeAssistant, latitude: float, longitude: float
) -> None:
    """Sync writes the home's coordinates as float64 to keys 0x0a/0x0b.

    Only zero and zero is no position; anything else is a place, and
    written. One zero is on the equator or on the prime meridian, and a home
    close to where the two cross is a home. Each goes out as a float64,
    whole number or not: the zero here is the whole number Home Assistant
    starts out with.
    """
    coordinator, lamp = _at_a_lamp(hass)
    keeper_of(coordinator).home = (latitude, longitude)
    await coordinator.async_sync_location()
    assert lamp.written == [
        (WRITE_UUID, cbor.encode({0x0A: float(latitude), 0x0B: float(longitude)}))
    ]


@pytest.mark.parametrize(
    ("setter", "args", "changed"),
    [
        pytest.param(
            "async_set_timer_start",
            (7, 30),
            {4: 7, 5: 30},  # the start: bytes 4 and 5 of the slot
            id="start",
        ),
        pytest.param(
            "async_set_timer_end",
            (19, 45),
            {6: 19, 7: 45},  # the end: bytes 6 and 7
            id="end",
        ),
        pytest.param("async_set_timer_brightness", (37,), {8: 37}, id="brightness"),
        pytest.param(
            "async_set_timer_gradual",
            (5,),
            {9: 0x01, 10: 0x2C},  # bytes 9 and 10: 5 min = 300 s = 0x012c
            id="gradual",
        ),
    ],
)
async def test_schedule_setters_work_once_slot_is_known(
    hass: HomeAssistant, setter: str, args: tuple[int, ...], changed: dict[int, int]
) -> None:
    """With the slot read, a setter changes only its own field of the 0x11 slot.

    The start and the end are an hour and a minute each, the brightness a
    byte, and the gradual is kept as two-byte big-endian seconds. The slot
    below shares no byte a setter could clobber with what any setter here is
    asked to write: a fixture that did could not tell "preserved the user's
    value" from "wrote a slot of its own", which is the regression this
    exists to catch. And a setter that wrote nothing at all would pass a
    test of its neighbour, so each is held to the whole frame it writes: its
    own bytes changed, every other byte the user's, untouched.
    """
    coordinator, lamp = _at_a_lamp(hass)
    slot = bytes.fromhex("000300fe091111115a0102")
    coordinator._mirror.echo({KEY_TIMER: slot})

    await getattr(coordinator, setter)(*args)

    expected = bytearray(slot)
    for index, byte in changed.items():
        expected[index] = byte
    assert bytes(expected) != slot
    assert lamp.written == [(WRITE_UUID, cbor.encode({KEY_TIMER: bytes(expected)}))]


async def test_a_device_reporting_unactivated_is_brought_up(
    hass: HomeAssistant,
) -> None:
    """A lamp whose 0x14 reads False is activated with the app's sequence.

    This is the whole point of the bring-up: a factory-reset lamp advertises
    and accepts config writes, but gates its light output on 0x14, so
    without this it stays dark however many commands it is sent. The
    sequence replays the app's: {0x53}, {time, 0x31}, then {0x14} - the flag
    that ungates the light. The lamp keeps wall-clock time, so the clock
    written is the local hour: UTC would run a new lamp's schedule and its
    circadian curve hours off.
    """
    await hass.config.async_set_time_zone("Asia/Kolkata")  # 5 h 30 min from UTC
    coordinator, lamp, link = await _holding_a_link(hass)
    coordinator._mirror.echo({KEY_ACTIVATED: False})

    await coordinator._async_activate_if_needed(turn_over(coordinator, link))

    assert len(lamp.written) == 3
    payloads = [cbor.decode(frame) for _uuid, frame in lamp.written]
    assert payloads[0] == {0x53: 300}
    assert payloads[1].keys() == {KEY_TIME, KEY_TIME_SYNCED}
    assert payloads[1][KEY_TIME_SYNCED] == 1
    assert payloads[2] == {KEY_ACTIVATED: True}
    clock = protocol.device_time(payloads[1])
    assert clock is not None
    local = dt_util.now().replace(tzinfo=None)
    assert abs((clock - local).total_seconds()) < 5
    assert coordinator.state[KEY_ACTIVATED] is True
    assert coordinator._activation_checked is True


@pytest.mark.parametrize("bring_up", [None, False], ids=["as built", "told not to"])
async def test_a_coordinator_told_not_to_brings_no_lamp_up(
    hass: HomeAssistant, bring_up: bool | None
) -> None:
    """Whoever only looks at a lamp says so where the coordinator is built.

    A lamp that reports itself not activated is brought up on the first
    exchange: that is what a factory-reset lamp needs of Home Assistant. The
    bench checks somebody's lamp and must never activate one behind their
    back, so it builds the coordinator told not to - and nothing of the
    sequence is written, however the flag reads.
    """
    lamp = ScriptedLamp()
    lamp.answers({KEY_ACTIVATED: False})
    coordinator = lamp.coordinator(hass, bring_up=bring_up)

    await link_of(coordinator).connect()

    commands = [
        cbor.decode(frame) for uuid, frame in lamp.written if uuid == WRITE_UUID
    ]
    if bring_up is None:  # told nothing, as the setup of an entry tells it nothing
        assert commands[0] == {0x53: 300}
        assert commands[-1] == {KEY_ACTIVATED: True}
    else:
        assert commands == []
        assert coordinator.activated is False


async def test_activated_property(hass: HomeAssistant) -> None:
    """Activated reflects the device's 0x14 flag."""
    coordinator, _ = _at_a_lamp(hass)
    assert coordinator.activated is None
    coordinator._mirror.echo({0x14: False})
    assert coordinator.activated is False
    coordinator._mirror.echo({0x14: True})
    assert coordinator.activated is True


def test_parse_device_info() -> None:
    """The facebd80 device-info string parses into a key/value map."""
    raw = (
        b"brand:Glowrium;pkey:Glowrium-C051;subid:3;"
        b"devid:CST-AABBCCDDEEFF;mac:AABBCCDDEEFF;version:4;;"
    )
    info = _parse_device_info(raw)
    assert info["pkey"] == "Glowrium-C051"
    assert info["version"] == "4"
    assert info["devid"] == "CST-AABBCCDDEEFF"


async def test_device_info_properties(hass: HomeAssistant) -> None:
    """model_id/sw_version/serial_number derive from the parsed device-info."""
    coordinator, _ = _at_a_lamp(hass)
    assert coordinator.sw_version is None
    coordinator.device_info = {
        "pkey": "Glowrium-C051",
        "version": "4",
        "devid": "CST-AABBCCDDEEFF",
    }
    assert coordinator.model_id == "Glowrium-C051"
    assert coordinator.sw_version == "4"
    assert coordinator.serial_number == "CST-AABBCCDDEEFF"


async def test_model_resolution(hass: HomeAssistant) -> None:
    """coordinator.model resolves the pkey, with a generic (not G7) fallback."""
    coordinator, _ = _at_a_lamp(hass)
    # Not read yet -> generic profile (reference presets, no false model name).
    assert coordinator.model.name == "Glowrium"
    assert "sun_sync" in coordinator.model.lighting_modes
    # Known pkey -> full G7 profile.
    coordinator.device_info = {"pkey": "Glowrium-C051"}
    assert coordinator.model.name == "Glowrium G7"
    # Unknown pkey -> generic, not masquerading as a G7.
    coordinator.device_info = {"pkey": "Glowrium-XXXX"}
    assert coordinator.model.name == "Glowrium"
    assert "sun_sync" in coordinator.model.lighting_modes


def _said_of_a_refusal(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return each warning that the lamp refused the state request, in order."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "refused the batched state request" in record.getMessage()
    ]


_REFUSAL = BleakError("Insufficient authorization (8)")
_ASKED_THEN_READ = [("write", NOTIFY_UUID), ("read", NOTIFY_UUID)]


async def test_a_lamp_that_keeps_refusing_is_asked_three_times_and_then_only_read(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Three refusals in a row, and for the rest of the session the lamp is read.

    A G8 answers the request with ATT "Insufficient authorization" (0x08)
    every time. It is asked and then read - in that order, each time: a
    refusal is the lamp answering, and what it has to say comes by the read.
    After the third in a row it is not asked again, and the log says so
    once: what the lamp is, that it is read from here on, that commands
    still work, what stays unknown, and that this is worth reporting. Three
    in a row are three times the lamp said no: a link that drops under the
    request is counted as nothing.
    """
    coordinator, lamp, link = await _holding_a_link(hass, "Glowrium-G8")
    _refusing(lamp)

    with caplog.at_level(logging.WARNING, logger=coordinator_module.__name__):
        for _ in range(3):
            assert await coordinator._request_state(turn_over(coordinator, link))
        assert lamp.exchanges == _ASKED_THEN_READ * 3
        (said,) = _said_of_a_refusal(caplog)

        for _ in range(4):  # however often it connects after that
            assert await coordinator._request_state(turn_over(coordinator, link))

    assert lamp.exchanges == _ASKED_THEN_READ * 3 + [("read", NOTIFY_UUID)] * 4
    assert _said_of_a_refusal(caplog) == [said]  # said once
    assert "3 times in a row" in said
    assert "model unknown, firmware unknown" in said
    assert "Commands still work" in said
    assert "stay unknown" in said
    assert "Please report" in said
    assert "not asked again this session" in said
    assert "read on every connect instead" in said
    for word in ("paus", "minute"):
        assert word not in said.lower()
    assert coordinator.diagnostics()["link"]["state_request_paused"] is True


async def test_a_refusal_followed_by_an_accepted_request_is_not_counted(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """It takes three in a row: a request the lamp accepts starts the count again.

    A lamp that said no twice and then answered is a lamp that answers. One
    bad moment does not add up with another an hour later.
    """
    coordinator, lamp, link = await _holding_a_link(hass, "Glowrium-G8")
    lamp.readable(NOTIFY_UUID, cbor.encode({KEY_POWER: True}))
    lamp.answers()

    with caplog.at_level(logging.WARNING, logger=coordinator_module.__name__):
        for _ in range(2):  # two bad moments, with an answer after each
            lamp.fails_writes(_REFUSAL, times=2)
            for _ in range(3):
                await coordinator._request_state(turn_over(coordinator, link))
        assert len(lamp.asked) == 6  # four refusals so far, and still asked
        assert _said_of_a_refusal(caplog) == []

        lamp.fails_writes(_REFUSAL)
        for _ in range(5):
            await coordinator._request_state(turn_over(coordinator, link))

    assert len(lamp.asked) == 9  # three more, in a row, and not after
    assert len(_said_of_a_refusal(caplog)) == 1


async def test_a_link_that_drops_under_the_request_is_not_a_refusal(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A transient failure must not cost the session its unread properties.

    On a weak link the request fails routinely with the link going under it
    - a real G7 hit it 40 s after start-up - and counted as a refusal that
    left the indicator, lighting mode, ramp and DST unread until Home
    Assistant was restarted. It is a link that was lost: it counts as
    nothing, beside two refusals as much as by itself.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    dropped = BleakError("[org.bluez.Error.Failed] Not connected")

    with caplog.at_level(logging.WARNING, logger=coordinator_module.__name__):
        lamp.fails_writes(dropped, times=1)
        assert (
            await coordinator._request_state(turn_over(coordinator, link)) is False
        )  # nothing answered, and nothing to read

        lamp.fails_writes(_REFUSAL, times=2)
        for _ in range(2):
            await coordinator._request_state(turn_over(coordinator, link))
        lamp.fails_writes(dropped, times=4)
        for _ in range(4):
            await coordinator._request_state(turn_over(coordinator, link))
        lamp.answers()
        await coordinator._request_state(turn_over(coordinator, link))

    assert len(lamp.asked) == 8  # every time: two refusals, and never a third
    assert _said_of_a_refusal(caplog) == []
    assert coordinator.diagnostics()["link"]["state_request_paused"] is False


@pytest.mark.parametrize(
    ("the_request", "readable", "answered"),
    [
        pytest.param(_REFUSAL, False, True, id="refused, and nothing to read"),
        pytest.param(
            BleakError("[org.bluez.Error.Failed] Not connected"),
            True,
            True,
            id="lost under the request, and the read answers",
        ),
        pytest.param(
            BleakError("[org.bluez.Error.Failed] Not connected"),
            False,
            False,
            id="lost under the request, and nothing to read",
        ),
    ],
)
async def test_the_link_has_answered_when_the_lamp_said_anything_at_all(
    hass: HomeAssistant, the_request: BleakError, readable: bool, answered: bool
) -> None:
    """A refusal is the lamp answering, and so is a read that comes back.

    Whether the link answered is what the link goes by: one that answers
    nothing is let go of at once. A lamp that says no to the request has
    spoken, whether or not it can be read after. A request that met a link
    going down has not - but if the read after it comes back, the lamp is
    there, and whatever doubt the failed request left is settled by it.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    lamp.fails_writes(the_request)
    if readable:
        lamp.readable(NOTIFY_UUID, cbor.encode({KEY_POWER: True}))

    said = await coordinator._request_state(turn_over(coordinator, link))

    assert said is answered
    assert lamp.exchanges == _ASKED_THEN_READ
    assert (link_of(coordinator).lost is None) is answered


async def test_a_lamp_that_reports_is_asked_and_never_read(hass: HomeAssistant) -> None:
    """A lamp that answers the state request is primed by that alone.

    From 0.2.0 the state was read first, on every connect, and on BlueZ a read
    of this lamp ends the link: a hundred links an hour were lost that way and
    put down to range (ARCHITECTURE.md, "Priming state on connect").
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    lamp.readable(NOTIFY_UUID, cbor.encode({KEY_POWER: False}))
    lamp.answers({KEY_POWER: True, KEY_BRIGHTNESS: 70})

    async with asyncio.timeout(1):  # the answer is there when the write returns
        assert await coordinator._request_state(turn_over(coordinator, link)) is True

    assert lamp.asked == [bytes(STATE_KEYS)]
    assert lamp.read == []
    assert coordinator.state[KEY_POWER] is True  # what the lamp reported
    assert coordinator.state[KEY_BRIGHTNESS] == 70
    assert coordinator.diagnostics()["link"]["state_request_paused"] is False


async def test_a_refused_request_falls_back_to_the_read(hass: HomeAssistant) -> None:
    """A lamp that will not report is read instead, link or no link.

    A G8 refuses the request, and the read is the only way to its state. It
    costs the link on BlueZ, and for such a lamp that is the price of having
    a state at all.
    """
    coordinator, lamp, link = await _holding_a_link(hass, "Glowrium-G8")
    _refusing(lamp)

    assert await coordinator._request_state(turn_over(coordinator, link)) is True

    # Asked first, then read.
    assert lamp.exchanges == [("write", NOTIFY_UUID), ("read", NOTIFY_UUID)]
    assert coordinator.state[KEY_POWER] is True


@pytest.mark.parametrize(
    "before",
    [None, cbor.encode(dict.fromkeys(STATE_KEYS, 0))],
    ids=["nothing", "a complete report, a moment earlier"],
)
async def test_a_lamp_that_acknowledges_and_says_nothing_is_read(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, before: bytes | None
) -> None:
    """The request can be accepted and still bring nothing. Then the read does.

    Not an error and not a refusal, so neither of those paths sees it: the
    write is acknowledged, and no report follows. The wait for one is
    bounded, and a lamp that stays silent is not left with no state. Only
    what the lamp says after being asked counts as its answer: the lamp
    reports on its own whenever something changes, and a complete map that
    happened to arrive a moment earlier says nothing about whether this
    request was answered.
    """
    monkeypatch.setattr(coordinator_module, "_REPORT_TIMEOUT", 0.05)
    coordinator, lamp, link = await _holding_a_link(hass)
    # The request is accepted, and nothing comes of it; the read has the state.
    lamp.readable(NOTIFY_UUID, cbor.encode({KEY_POWER: True}))
    if before is not None:
        lamp.say(before)  # before the request went out

    async with asyncio.timeout(1):
        assert await coordinator._request_state(turn_over(coordinator, link)) is True

    assert lamp.read == [NOTIFY_UUID]
    assert coordinator.state[KEY_POWER] is True

    # And if the read fails as well, the acknowledgement still stands: the
    # link answered, so it is not dropped as one that answers nothing.
    lamp.readable(NOTIFY_UUID, None)
    async with asyncio.timeout(1):
        assert await coordinator._request_state(turn_over(coordinator, link)) is True


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("c0a106f5", id="a frame that cannot be decoded"),
        pytest.param("a0", id="a map with nothing in it"),
    ],
)
async def test_a_lamp_that_answers_with_a_frame_of_which_nothing_is_kept_is_read(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """A frame of which nothing is kept is no answer to the request, and is read past.

    Such a lamp is said once in the log, and nothing else is done differently
    for it. The request is acknowledged, what comes back is no report, the
    wait for one runs out, and the state is read - as with a lamp that says
    nothing at all, and the next time the same. It is not a refusal either:
    the lamp is asked again each time.
    """
    monkeypatch.setattr(coordinator_module, "_REPORT_TIMEOUT", 0.05)
    coordinator, lamp, link = await _holding_a_link(hass)
    lamp.answers(frame=bytes.fromhex(answer))
    lamp.readable(NOTIFY_UUID, cbor.encode({KEY_POWER: True}))

    for _ in range(4):  # one more than it takes of refusals to end the asking
        async with asyncio.timeout(1):
            asked = await coordinator._request_state(turn_over(coordinator, link))
        assert asked is True

    assert lamp.asked == [bytes(STATE_KEYS)] * 4
    assert lamp.read == [NOTIFY_UUID] * 4
    assert coordinator.state == {KEY_POWER: True}  # from the read, and nothing else


async def test_an_answer_in_two_notifications_is_waited_for(
    hass: HomeAssistant,
) -> None:
    """A lamp may spread its answer; the rest is waited for, not read.

    A G8 splits its property map across notifications. Taking the first one
    for the whole answer would leave the caller - the activation check, the
    clock - looking at half a state.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    first, second = STATE_KEYS[:5], STATE_KEYS[5:]
    lamp.say(cbor.encode(dict.fromkeys(STATE_KEYS, 9)))  # a complete report, before
    lamp.answers(frame=cbor.encode(dict.fromkeys(first, 1)))
    hass.loop.call_later(0.05, lamp.say, cbor.encode(dict.fromkeys(second, 2)))

    assert await coordinator._request_state(turn_over(coordinator, link)) is True

    assert coordinator.state[second[-1]] == 2  # it waited for the second one
    assert lamp.read == []


async def test_a_partial_answer_is_not_topped_up_by_a_read(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lamp that reports some of what it was asked has answered.

    The read would not add what is missing - it stops at 0x15, short of
    everything a lamp is likely not to know - and it would cost the link.
    """
    monkeypatch.setattr(coordinator_module, "_REPORT_TIMEOUT", 0.05)
    coordinator, lamp, link = await _holding_a_link(hass)
    lamp.answers({KEY_POWER: True}, only=(KEY_POWER, KEY_BRIGHTNESS))

    async with asyncio.timeout(1):
        assert await coordinator._request_state(turn_over(coordinator, link)) is True

    assert coordinator.state[KEY_POWER] is True
    assert lamp.read == []


async def test_the_intake_notes_the_answer_and_tells_of_a_report_only(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frame is the lamp answering, whatever it says; the entities hear of a report.

    The two things the coordinator does with a frame that the mirror does
    not: note that the lamp answered - a garbage frame is still the lamp
    speaking - and tell the entities, for a report only. A frame that was
    no report wakes nobody.
    """
    coordinator, _lamp = _at_a_lamp(hass)
    told: list[int] = []
    coordinator.async_add_listener(lambda: told.append(1))
    monkeypatch.setattr(link_module, "monotonic", lambda: 1000.0)
    assert coordinator._ingest(b"\xc0\x00") == frozenset()  # undecodable: no report
    assert link_of(coordinator).last_answer == 1000.0  # and still the lamp speaking
    assert told == []
    monkeypatch.setattr(link_module, "monotonic", lambda: 1001.0)
    assert coordinator._ingest(cbor.encode({KEY_POWER: True})) == frozenset({KEY_POWER})
    assert link_of(coordinator).last_answer == 1001.0
    assert told == [1]


async def test_what_the_lamp_is_asked_for_is_kept_whatever_else_it_sent(
    hass: HomeAssistant,
) -> None:
    """The coordinator tells the mirror which ids the integration knows.

    A device that fills the mirror with ids nobody named does not keep the
    lamp's own state out: everything the lamp is asked for is taken in after
    it. The mirror has room for sixty-four ids it has no name for, and
    whatever answers at the lamp's address can fill it.
    """
    coordinator, _lamp = _at_a_lamp(hass)
    flood(coordinator._ingest)  # two hundred ids nobody named

    carried = coordinator._ingest(cbor.encode(dict.fromkeys(STATE_KEYS, 1)))

    assert carried == frozenset(STATE_KEYS)
    assert set(STATE_KEYS) <= set(coordinator.state)
    assert len(coordinator.state) == 64 + len(STATE_KEYS)


def test_every_property_the_integration_names_is_known_to_the_mirror() -> None:
    """What has a name in the integration is what the mirror always keeps.

    The ids it asks the lamp for, every id it has a constant for - the ones
    only its commands write among them - and every id the diagnostics read
    out or say is hidden. A name added and left out of the set would be an
    id the mirror may have no room for.
    """
    named = {value for name, value in vars(const).items() if name.startswith("KEY_")}
    assert named <= KNOWN_KEYS
    assert set(STATE_KEYS) <= KNOWN_KEYS
    assert {*diagnostics._READ, *const.PLACE_KEYS} <= KNOWN_KEYS


async def test_every_id_a_command_writes_is_known_to_the_mirror(
    hass: HomeAssistant,
) -> None:
    """What the integration writes, it has a name for.

    The lighting-mode command carries two ids the lamp never reports back,
    and the bring-up two more; all four are the integration's own.
    """
    coordinator, lamp, link = await _holding_a_link(hass)

    await coordinator.async_set_lighting_mode(5)
    await coordinator._async_activate(turn_over(coordinator, link))

    written = {key for _uuid, frame in lamp.written for key in cbor.decode(frame)}
    assert {0x2C, 0x32, 0x31, 0x53} <= written  # the ids this is about were written
    assert written <= KNOWN_KEYS


async def test_a_frame_of_which_nothing_was_kept_is_still_the_lamp_speaking(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever came from the lamp's address has answered, kept or not.

    Whether the link is alive is the link's question, and it is noted
    before the mirror is asked anything. The entities hear of a report, and
    a frame the mirror had no room for is none.
    """
    coordinator, _lamp = _at_a_lamp(hass)
    flood(coordinator._ingest)  # two hundred ids nobody named
    told: list[int] = []
    coordinator.async_add_listener(lambda: told.append(1))
    monkeypatch.setattr(link_module, "monotonic", lambda: 1000.0)

    assert coordinator._ingest(cbor.encode({5000: True})) == frozenset()

    assert link_of(coordinator).last_answer == 1000.0
    assert told == []


async def test_split_notification_updates_state(hass: HomeAssistant) -> None:
    """A notification carrying a split map still updates the entities."""
    coordinator, lamp, _link = await _holding_a_link(hass, "Glowrium-G8")
    lamp.say(bytes.fromhex("a306f5081846"))  # promises 3, has 2
    assert coordinator.state[KEY_POWER] is True
    assert coordinator.state[KEY_BRIGHTNESS] == 70


async def test_empty_ramp_does_not_latch(hass: HomeAssistant) -> None:
    """A zero-length ramp must not block seeding the remembered ramp later."""
    coordinator, _lamp = _at_a_lamp(hass)
    coordinator._ingest(bytes.fromhex("a1182f40"))  # {0x2f: b""}
    assert coordinator._desired_ramp is None
    coordinator._ingest(bytes.fromhex("a1182f420e10"))  # {0x2f: b"\x0e\x10"}
    assert coordinator._desired_ramp == bytes.fromhex("0e10")


async def test_schedule_setters_refuse_when_slot_unread(hass: HomeAssistant) -> None:
    """Changing one schedule field must not invent the other four.

    The 0x11 slot packs enabled, both times, brightness and fade into one write,
    so falling back to a default silently overwrote settings the user chose.
    """
    coordinator, lamp = _at_a_lamp(hass)
    for call in (
        coordinator.async_set_timer_start(7, 30),
        coordinator.async_set_timer_end(19, 0),
        coordinator.async_set_timer_brightness(80),
        coordinator.async_set_timer_gradual(15),
    ):
        with pytest.raises(HomeAssistantError) as err:
            await call
        assert err.value.translation_key == "schedule_not_read"
    assert lamp.written == []


async def test_ramp_refuses_when_lighting_mode_unread(hass: HomeAssistant) -> None:
    """Setting the ramp must not silently reset the lighting mode to index 1."""
    coordinator, lamp = _at_a_lamp(hass)
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_ramp(30)
    assert err.value.translation_key == "lighting_mode_not_read"
    assert lamp.written == []


# An array that holds a null, as the bytes of it: the encoder these tests
# write frames with has no null to write, and the lamp's frames may hold one.
_A_NULL_INSIDE = object()


def _reporting(key: int, value: Any) -> bytes:
    """Return the frame in which the lamp reports ``value`` under ``key``."""
    if value is _A_NULL_INSIDE:
        return b"\xa1" + cbor.encode(key) + bytes.fromhex("81f6")
    return cbor.encode({key: value})


_NO_RAMP = [
    pytest.param(b"\xff" * 5, id="five bytes"),
    pytest.param(bytes.fromhex("ffff"), id="eighteen hours"),
    pytest.param("0708", id="text"),
    pytest.param(_A_NULL_INSIDE, id="an array holding null"),
]


@pytest.mark.parametrize("no_ramp", _NO_RAMP)
async def test_a_lighting_mode_is_not_written_with_a_ramp_that_cannot_be_used(
    hass: HomeAssistant, no_ramp: Any
) -> None:
    """The command carries a ramp, and never one the lamp could not have meant.

    Choosing a mode rewrites the ramp on the lamp, so the command sends one:
    the ramp remembered, else the one reported, else the default. What was
    reported went in as it came - five bytes of ``ff`` were written back
    to the lamp - and an array holding a null could not be written at all:
    the command ended in a TypeError. The frame is the one captured from
    the vendor app, with the default hour.
    """
    coordinator, lamp = _at_a_lamp(hass)
    coordinator._ingest(_reporting(KEY_RAMP, no_ramp))

    await coordinator.async_set_lighting_mode(5)

    assert lamp.written == [
        (WRITE_UUID, bytes.fromhex("a4182b05182c4202d0182f420e10183242001e"))
    ]


@pytest.mark.parametrize("no_ramp", _NO_RAMP)
async def test_a_ramp_that_cannot_be_used_is_not_the_one_remembered(
    hass: HomeAssistant, no_ramp: Any
) -> None:
    """A ramp is remembered from a report only when it is a ramp.

    The one remembered is re-applied after every switch to Circadian, and
    goes into every choice of a lighting mode. Reported first, what is no
    ramp is not remembered: the switch to Circadian has nothing to
    re-apply. Reported after a ramp that is one, it does not take its place.
    """
    coordinator, lamp = _at_a_lamp(hass)
    coordinator._ingest(_reporting(KEY_RAMP, no_ramp))

    await coordinator.async_set_operating_mode("circadian")

    assert [frame for _uuid, frame in lamp.written] == [
        cbor.encode({KEY_SCHEDULE: False}),
        cbor.encode({KEY_CIRCADIAN: True}),
    ]

    half_an_hour, lamp_too = _at_a_lamp(hass)
    half_an_hour._ingest(cbor.encode({KEY_RAMP: bytes.fromhex("0708")}))
    half_an_hour._ingest(_reporting(KEY_RAMP, no_ramp))

    await half_an_hour.async_set_lighting_mode(5)

    assert lamp_too.written == [
        (WRITE_UUID, bytes.fromhex("a4182b05182c4202d0182f420708183242001e"))
    ]


@pytest.mark.parametrize(
    "no_mode",
    [
        pytest.param("sun_sync", id="text"),
        pytest.param(True, id="a flag"),
        pytest.param(256, id="more than a byte"),
        pytest.param(_A_NULL_INSIDE, id="an array holding null"),
    ],
)
async def test_the_ramp_is_refused_while_the_lighting_mode_cannot_be_used(
    hass: HomeAssistant, no_mode: Any
) -> None:
    """Setting the ramp rewrites the lighting mode, and has no mode to write.

    It is refused as it is while no mode has been reported - and in other
    words, since waiting will not bring one. What was reported used to be
    written back: after a mode that was text, setting the ramp wrote the
    text. So is the ramp that a switch to Circadian re-applies: the switch
    is made, and then says what it could not do.
    """
    coordinator, lamp = _at_a_lamp(hass)
    coordinator._ingest(cbor.encode({KEY_RAMP: bytes.fromhex("0708")}))
    coordinator._ingest(_reporting(KEY_LIGHTING_MODE, no_mode))

    with pytest.raises(HomeAssistantError) as refused:
        await coordinator.async_set_ramp(30)

    assert refused.value.translation_key == "lighting_mode_unusable"
    assert lamp.written == []

    with pytest.raises(HomeAssistantError) as refused:
        await coordinator.async_set_operating_mode("circadian")

    assert refused.value.translation_key == "lighting_mode_unusable"
    assert [frame for _uuid, frame in lamp.written] == [
        cbor.encode({KEY_SCHEDULE: False}),
        cbor.encode({KEY_CIRCADIAN: True}),
    ]


@pytest.mark.parametrize(
    "no_slot",
    [
        pytest.param(bytes.fromhex("0100000006001200640000") + b"\x00", id="twelve"),
        pytest.param(b"\xff" * 40, id="forty bytes"),
        pytest.param(bytes.fromhex("0100000019001200640000"), id="25 o'clock"),
        pytest.param(_A_NULL_INSIDE, id="an array holding null"),
    ],
)
async def test_a_schedule_field_is_refused_while_the_slot_cannot_be_used(
    hass: HomeAssistant, no_slot: Any
) -> None:
    """One field is changed by writing the whole slot, and there is none to write.

    After a schedule of forty bytes, changing its start wrote forty bytes;
    a slot of twelve was edited in place and went back with the twelfth.
    Each change is refused, as it is while no slot has been reported, and
    says that what the lamp reported cannot be used.
    """
    coordinator, lamp = _at_a_lamp(hass)
    coordinator._ingest(_reporting(KEY_TIMER, no_slot))

    for change in (
        coordinator.async_set_timer_start(7, 30),
        coordinator.async_set_timer_end(19, 0),
        coordinator.async_set_timer_brightness(80),
        coordinator.async_set_timer_gradual(15),
    ):
        with pytest.raises(HomeAssistantError) as refused:
            await change
        assert refused.value.translation_key == "schedule_unusable"
    assert lamp.written == []


@pytest.mark.parametrize(
    "no_slot",
    [
        pytest.param(b"\xff" * 5, id="five bytes of ff"),
        pytest.param(bytes.fromhex("0200000e10"), id="neither on nor off"),
        pytest.param(b"\x01", id="one byte"),
        pytest.param(_A_NULL_INSIDE, id="an array holding null"),
    ],
)
async def test_the_dst_switch_writes_the_hour_where_the_slot_cannot_be_used(
    hass: HomeAssistant, no_slot: Any
) -> None:
    """The offset the lamp reported is carried into the write only when it is one.

    After five bytes of ``ff`` the switch wrote ``01 ff ff ff ff``: the flag
    it was asked for, and four of those bytes for an offset. The frame is
    the captured one, with the hour.
    """
    coordinator, lamp = _at_a_lamp(hass)
    coordinator._ingest(_reporting(KEY_DST, no_slot))

    await coordinator.async_set_dst(True)

    assert lamp.written == [(WRITE_UUID, bytes.fromhex("a11835450100000e10"))]


@pytest.mark.parametrize("why", ["refused before it was built", "failed on the wire"])
async def test_a_ramp_that_was_refused_is_not_remembered(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, why: str
) -> None:
    """A ramp the user was told had failed is not applied behind their back later.

    The ramp was noted as the user's choice before the command was built,
    and building it is what refuses while the lighting mode is unread. The
    note stayed. A later switch to Circadian then made both of its writes -
    the mode did change - and went on to re-apply the remembered ramp, which
    refused again: the user was told the switch had failed while watching it
    take effect. The same for a ramp whose write never reached the lamp.
    """
    coordinator, lamp = _at_a_lamp(hass)
    if why == "failed on the wire":
        coordinator._ingest(cbor.encode({KEY_LIGHTING_MODE: 5}))
        monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
        lamp.fails_writes(BleakError("Not connected"), times=2)  # both tries
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_ramp(30)
    assert coordinator._desired_ramp is None
    lamp.written.clear()

    await coordinator.async_set_operating_mode("circadian")

    written = [frame for _uuid, frame in lamp.written]
    assert written == [
        cbor.encode({KEY_SCHEDULE: False}),
        cbor.encode({KEY_CIRCADIAN: True}),
    ]


_WHERE = {KEY_LATITUDE: 12.3456, KEY_LONGITUDE: 65.4321}
_LATITUDE_HEX = cbor.encode(12.3456).hex()[2:]  # the eight bytes after fb
_LONGITUDE_HEX = cbor.encode(65.4321).hex()[2:]
_CURVE = bytes(range(0x40, 0x5C))  # 28 bytes of sunrise and sunset times


@pytest.mark.parametrize(
    ("frame", "said"),
    [
        pytest.param(
            cbor.encode(_WHERE).hex() + "deadbeef", "trailing bytes", id="trailing"
        ),
        pytest.param(
            "a4" + cbor.encode(_WHERE).hex()[2:] + "09c000" + "0afb" + _LATITUDE_HEX,
            "cannot read",
            id="an item that cannot be read, with the place on both sides of it",
        ),
        pytest.param(
            "82" + cbor.encode(_WHERE).hex() + "c0",
            "Undecodable frame",
            id="undecodable",
        ),
        pytest.param(
            "81" + cbor.encode(_WHERE).hex(),
            "nothing that can be used",
            id="decoded, and not a map of properties",
        ),
    ],
)
async def test_no_line_in_the_log_carries_the_coordinates(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, frame: str, said: str
) -> None:
    """Every line that prints a frame prints it blanked, the second time too.

    Each is a debug line: the warnings a frame can cause say where the frame
    is and carry none of it. Whoever posts a debug log is asked to look a
    frame over, and that cannot rest on blanking hex by hand.
    """
    coordinator, _lamp = _at_a_lamp(hass)

    # The whole package is listened to: today only the mirror prints a frame,
    # and a line of any other module that came to print one is held to this too.
    with caplog.at_level(logging.DEBUG, logger=coordinator_module.__package__):
        coordinator._ingest(bytes.fromhex(frame))
        coordinator._ingest(bytes.fromhex(frame))

    assert said in caplog.text
    assert "xx" * 8 in caplog.text
    assert _LATITUDE_HEX not in caplog.text
    assert _LONGITUDE_HEX not in caplog.text


# {power: on, brightness: 70, 0x09: <a tag, which nothing here can read> ...
_PARTLY_READABLE = bytes.fromhex("a406f508184609c0000d00")


async def test_a_report_read_only_in_part_is_still_the_answer_to_the_request(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lamp that answers with a frame read in part has answered, and is not read.

    Taken for a malformed frame and dropped, the answer counted as silence:
    the connect fell back on reading the state, and on BlueZ a read of this
    lamp ends the link two seconds later. A model whose report carries a
    single item nobody has a reading for would have lost its link on every
    connect - what 0.2.0 and 0.2.1 did to every lamp.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    monkeypatch.setattr(coordinator_module, "_REPORT_TIMEOUT", 0.05)
    lamp.answers(frame=_PARTLY_READABLE)
    lamp.readable(NOTIFY_UUID, cbor.encode({KEY_POWER: False}))

    assert await coordinator._request_state(turn_over(coordinator, link)) is True

    assert lamp.read == []
    assert coordinator.state == {KEY_POWER: True, KEY_BRIGHTNESS: 70}


async def test_lost_acknowledgement_is_not_reported_as_failure(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write the lamp acted on is not reported as having failed, nor waited out.

    Observed on a real G7 at RSSI -88: both attempts of light.turn_on raised
    "GATT Protocol Error: Unlikely Error", yet the lamp lit and notified its
    new state 32 ms BEFORE the error surfaced. The user saw a failure toast,
    a lit lamp, and an entity reading `on`. And the wait for the lamp's word
    ends with the word: the ceiling is for a lamp that says nothing, and one
    that reported what the command set before the write had even failed is
    believed at once - a wait that always ran its full length was proposed
    with the split (#21), and is not made unless it is named there first.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 30)
    # The device receives the write and reports the new state; only the
    # acknowledgement is lost, so bleak still raises.
    lamp.fails_writes(
        BleakError("GATT Protocol Error: Unlikely Error"),
        saying=lambda _frame: cbor.encode({KEY_POWER: True}),
    )

    async with asyncio.timeout(2):
        await coordinator.async_set_power(True)  # must not raise, and not wait
    assert coordinator.state[KEY_POWER] is True


def _the_mode_and_the_ramp(frame: bytes) -> bytes:
    """Report back what a lamp tracks of a mode command: the mode and the ramp.

    Never 0x2c or 0x32, which are fixed parameters.
    """
    sent = cbor.decode(frame)
    reported = {k: v for k, v in sent.items() if k in (KEY_LIGHTING_MODE, KEY_RAMP)}
    assert set(sent) - set(reported) == {0x2C, 0x32}
    return cbor.encode(reported)


def _the_mode_alone(frame: bytes) -> bytes:
    """Report back the mode alone: the ramp the command carried is what it was."""
    assert cbor.decode(frame)[KEY_RAMP] == bytes.fromhex("0e10")
    return cbor.encode({KEY_LIGHTING_MODE: 5})


@pytest.mark.parametrize(
    "reports",
    [_the_mode_and_the_ramp, _the_mode_alone],
    ids=["the mode and the ramp", "the mode alone"],
)
async def test_a_command_is_vouched_for_by_what_it_changed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    reports: Callable[[bytes], bytes],
) -> None:
    """A mode command is vouched for by 0x2b and 0x2f, never by its fixed parameters.

    0x2c and 0x32 are constants the lamp never reports back; requiring them
    to match would mean no mode command could ever be confirmed. And the
    lamp reports what changed, which is enough: a ramp that was already what
    the command carried is not reported again, and need not be - the mode
    was, after the write, and with the value asked for.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.05)
    coordinator._ingest(
        cbor.encode({KEY_LIGHTING_MODE: 1, KEY_RAMP: bytes.fromhex("0e10")})
    )
    lamp.fails_writes(BleakError("Unlikely Error"), saying=reports)

    await coordinator.async_set_lighting_mode(5)  # must not raise
    assert coordinator.state[KEY_LIGHTING_MODE] == 5


@pytest.mark.parametrize("path", ["a background connect", "the poll"])
async def test_the_first_exchange_primes_the_link_once_on_either_path(
    hass: HomeAssistant, path: str
) -> None:
    """The first exchange asks, lands the answer, reads the model, and says so.

    On a link a background connect made (``Link.connect``: dial, subscribe,
    then the greet) and on one a command took, which the poll comes to: a
    command connects without priming to stay inside its budget, so something
    has to go back for the rest. Asserting that the poll calls a method by
    name would pass with that method emptied out; what matters is that the
    lamp is asked and its answer lands - and that the device-info string is
    read only then. The link is marked primed; without the mark the poll
    would re-interrogate the lamp every 30 s forever.
    """
    if path == "the poll":
        coordinator, lamp, _link = await _holding_a_link(hass)
    else:
        coordinator, lamp = _at_a_lamp(hass)
    lamp.answers({KEY_POWER: True, KEY_ACTIVATED: True})
    lamp.readable(INFO_UUID, b"brand:Glowrium;pkey:Glowrium-C051;version:4;;")

    if path == "the poll":
        link_of(coordinator).tick()  # a link exists that nothing has primed
        await hass.async_block_till_done()
    else:
        await link_of(coordinator).connect()

    assert lamp.links[0].subscribed  # or no state ever arrives
    assert lamp.asked == [bytes(STATE_KEYS)]
    assert coordinator.state[KEY_POWER] is True  # the properties actually landed
    assert lamp.read == [INFO_UUID]  # and the model
    assert coordinator.model_id == "Glowrium-C051"
    assert link_of(coordinator).diagnostics()["primed"] is True

    # And it is not asked again on every tick from then on.
    link_of(coordinator).tick()
    await hass.async_block_till_done()
    assert lamp.asked == [bytes(STATE_KEYS)]


@pytest.mark.parametrize("reached", ["by a background connect", "by the poll"])
async def test_what_the_first_exchange_wrote_is_told_to_the_entities(
    hass: HomeAssistant, reached: str
) -> None:
    """A lamp brought up in the first exchange is shown as activated at once.

    What the exchange writes goes into the mirror as each write is
    acknowledged. A real lamp reports its new state as well, and that report
    tells the entities; one that is slow to, or does not, would leave them
    showing the lamp as it was before the exchange. So they are told once
    more when it is over - after a background connect, and after the exchange
    the poll makes on a link a command took.
    """
    if reached == "by the poll":
        coordinator, lamp, _link = await _holding_a_link(hass)
    else:
        coordinator, lamp = _at_a_lamp(hass)
    lamp.answers({KEY_ACTIVATED: False})
    lamp.readable(INFO_UUID, b"brand:x;;")
    shown: list[bool | None] = []
    coordinator.async_add_listener(lambda: shown.append(coordinator.activated))

    if reached == "by the poll":
        link_of(coordinator).tick()  # the link is one a command made
        await hass.async_block_till_done()
    else:
        await link_of(coordinator).reconnect()

    assert coordinator.activated is True
    assert False in shown  # the lamp's own report, before anything was written
    assert shown[-1] is True  # and told again once it was


async def test_a_device_report_reaches_the_entities(hass: HomeAssistant) -> None:
    """Ingesting a frame must notify listeners, not just update the mirror.

    Entities re-render from a coordinator listener. Updating `state` without
    firing them leaves every entity in Home Assistant showing stale values
    while the coordinator quietly knows better - invisible in any test that
    inspects `state` directly.
    """
    coordinator, _ = _at_a_lamp(hass)
    fired: list[int] = []
    remove = coordinator.async_add_listener(lambda: fired.append(1))

    coordinator._ingest(cbor.encode({KEY_POWER: True}))
    assert fired == [1]

    remove()
    coordinator._ingest(cbor.encode({KEY_POWER: False}))
    assert fired == [1]  # and a removed listener stops hearing about it


async def test_a_command_reaches_the_entities(hass: HomeAssistant) -> None:
    """A successful command notifies listeners too, on its optimistic echo.

    Twice here: once when the command takes the link, and once more on the
    echo - the command is the first thing said to this lamp.
    """
    coordinator, _ = _at_a_lamp(hass)
    fired: list[int] = []
    coordinator.async_add_listener(lambda: fired.append(1))

    await coordinator.async_set_power(True)
    assert fired == [1, 1]


def _listeners_that_fail(coordinator: GlowriumCoordinator, count: int) -> list[int]:
    """Give ``coordinator`` listeners that all raise, and return who was told."""
    told: list[int] = []

    def _failing(number: int) -> Callable[[], None]:
        def _listener() -> None:
            told.append(number)
            raise ValueError("this entity cannot show what it was given")

        return _listener

    for number in range(count):
        coordinator.async_add_listener(_failing(number))
    return told


async def test_a_listener_that_fails_does_not_keep_the_news_from_the_rest(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An entity that cannot show a value must not cost the others theirs.

    The listeners are the entities, told one after another. With nothing
    around each, the first to raise ended the round: whoever came after it
    went on showing what it had shown before, and the exception landed on
    whoever had brought the news - the notification, or a command that had
    in fact gone through.

    Every listener here fails, so the order they are told in does not decide
    the outcome. Each failure is logged with its trace - the one thing asked
    for that the integration did not word - and the request to report it
    says to look the trace over first.
    """
    coordinator, _ = _at_a_lamp(hass)
    told = _listeners_that_fail(coordinator, 3)

    with caplog.at_level(logging.ERROR):
        carried = coordinator._ingest(cbor.encode({KEY_POWER: True}))

    assert sorted(told) == [0, 1, 2]
    assert carried == frozenset({KEY_POWER})  # and the report still counts
    failures = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(failures) == 3
    assert all(r.exc_info for r in failures)  # with what it takes to fix it
    for failure in failures:
        assert "Please report" in failure.getMessage()
        assert "before posting" in failure.getMessage()


async def test_a_listener_that_keeps_failing_is_named_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The lamp reports all day; a trace for every report would bury the log.

    Loudly the first time, then quietly - until the listener has managed a
    round, after which a new failure is news again. Said once is said about
    the first failure; the next may be another: a listener that has not
    recovered can fail differently the second time, and with nothing kept of
    it there would be no way to learn how. So the repeat leaves its trace at
    debug.
    """
    coordinator, _ = _at_a_lamp(hass)
    healthy = [False]

    def _listener() -> None:
        if not healthy[0]:
            raise ValueError("this entity cannot show what it was given")

    coordinator.async_add_listener(_listener)

    def _told_once_more() -> list[logging.LogRecord]:
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=coordinator_module.__name__):
            coordinator._ingest(cbor.encode({KEY_POWER: True}))
        return list(caplog.records)

    def _errors(records: list[logging.LogRecord]) -> int:
        return len([r for r in records if r.levelno >= logging.ERROR])

    assert _errors(_told_once_more()) == 1
    again = _told_once_more()
    assert _errors(again) == 0  # the same fault, said once
    (repeat,) = [r for r in again if "failed again" in r.getMessage()]
    assert repeat.levelno == logging.DEBUG
    assert repeat.exc_info
    healthy[0] = True
    assert _errors(_told_once_more()) == 0
    healthy[0] = False
    assert _errors(_told_once_more()) == 1  # it had recovered: this is a new one


async def test_a_listener_that_left_while_failing_leaves_no_record_behind(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """One that leaves during a round and then raises is gone, record and all.

    The round is told from a copy of the list, so a listener removed during
    it is still called. Noted as failing after it had left, it would be
    quiet about its first failure when it came back.
    """
    coordinator, _ = _at_a_lamp(hass)
    leave: list[Callable[[], None]] = []

    def _listener() -> None:
        if leave:
            leave.pop()()
        raise ValueError("this entity cannot show what it was given")

    def _errors_while_told() -> int:
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            coordinator._ingest(cbor.encode({KEY_POWER: True}))
        return len([r for r in caplog.records if r.levelno >= logging.ERROR])

    leave.append(coordinator.async_add_listener(_listener))
    assert _errors_while_told() == 1  # it left, and then it raised

    coordinator.async_add_listener(_listener)  # back, and this time it stays
    assert _errors_while_told() == 1  # its first failure since it came back


async def test_a_listener_added_again_starts_with_a_clean_record(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """What a listener did before it was removed is not held against it."""
    coordinator, _ = _at_a_lamp(hass)

    def _listener() -> None:
        raise ValueError("this entity cannot show what it was given")

    for _round in range(2):
        remove = coordinator.async_add_listener(_listener)
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            coordinator._ingest(cbor.encode({KEY_POWER: True}))
        assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1
        remove()


async def test_a_command_that_went_through_is_not_failed_by_a_listener(
    hass: HomeAssistant,
) -> None:
    """The lamp did what it was told; an entity's trouble is not the caller's."""
    coordinator, lamp = _at_a_lamp(hass)
    told = _listeners_that_fail(coordinator, 2)

    await coordinator.async_set_power(True)

    assert len(lamp.written) == 1
    assert sorted(set(told)) == [0, 1]


async def test_confirmation_waits_for_a_report_that_arrives_late(
    hass: HomeAssistant,
) -> None:
    """The grace window is a wait, not a glance at state as it already is.

    Measured on real hardware the confirming notification beat the error by
    22-32 ms, but that is a property of one link on one evening. If the report
    lands after the write has failed, the command must still be reported as
    delivered - which means actually waiting on the device, not sampling state
    once and giving up.
    """
    coordinator, lamp = _at_a_lamp(hass)
    lamp.fails_writes(BleakError("Unlikely Error"))
    assert KEY_POWER not in coordinator.state  # nothing to match at failure time

    async def _report_after_the_failure() -> None:
        await asyncio.sleep(0.05)
        coordinator._ingest(cbor.encode({KEY_POWER: True}))

    reporter = asyncio.create_task(_report_after_the_failure())
    try:
        await coordinator.async_set_power(True)  # must not raise
    finally:
        await reporter
    assert coordinator.state[KEY_POWER] is True
    # A command asks for a bare link: nothing but the command was written.
    assert {uuid for uuid, _frame in lamp.written} == {WRITE_UUID}


async def test_a_write_with_nothing_reportable_is_never_confirmed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A payload the device never reports back cannot vouch for itself.

    Confirmation compares against what the lamp reports, so a write carrying
    only keys outside STATE_KEYS has no evidence available either way, and
    silence must not be read as success.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.05)
    lamp.fails_writes(BleakError("down"))

    with pytest.raises(HomeAssistantError):
        await coordinator._async_write({0x2C: b"\x02\xd0"})
    # A command asks for a bare link: nothing but the command was written.
    assert {uuid for uuid, _frame in lamp.written} == {WRITE_UUID}


async def test_the_device_info_is_the_last_thing_read_and_read_once(
    hass: HomeAssistant,
) -> None:
    """The one read left on a link that reports comes after everything else.

    The device-info string is only to be had by a read, and on BlueZ that
    read ends the link two seconds later. So it waits until the state has
    arrived and whatever had to be written - here a stale clock - has been.
    After that the model is known, and no later link is read at all.
    """
    coordinator, lamp = _at_a_lamp(hass)
    stale = bytes.fromhex("07e80101000000")  # 2024-01-01 00:00:00
    lamp.answers({KEY_TIME: stale})
    lamp.readable(INFO_UUID, b"brand:Glowrium;pkey:Glowrium-C051;version:4;;")

    await link_of(coordinator).connect()
    assert lamp.exchanges == [
        ("write", NOTIFY_UUID),  # state asked
        ("write", WRITE_UUID),  # clock written
        ("read", INFO_UUID),  # info read
    ]
    assert coordinator.model_id == "Glowrium-C051"

    lamp.links[0].lose()  # the read cost the link
    await hass.async_block_till_done()
    lamp.exchanges.clear()
    await link_of(coordinator).connect()

    assert ("read", INFO_UUID) not in lamp.exchanges
    assert ("read", NOTIFY_UUID) not in lamp.exchanges
    assert lamp.read == [INFO_UUID]  # once, in the whole session


async def test_the_first_exchange_does_not_wait_for_the_activation_flag(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the lamp was asked, the exchange goes on with what it has.

    The exchange waited up to three seconds for the flag after the state
    request - which had asked for it and waited for it, and read where the
    lamp did not report. That second wait ran only where the flag was not
    going to come, with the link's lock held and a command queued behind it.
    A lamp that acknowledges, says nothing and cannot be read is the one it
    ran longest for: the exchange now ends without sleeping at all - by the
    count of what was slept, not by the wall.
    """
    monkeypatch.setattr(coordinator_module, "_REPORT_TIMEOUT", 0.05)
    coordinator, lamp, _link = await _holding_a_link(hass)
    slept: list[float] = []
    unpatched = asyncio.sleep

    async def _counted(delay: float, *_rest: object) -> None:
        slept.append(delay)
        await unpatched(0)

    with monkeypatch.context() as for_the_exchange:
        for_the_exchange.setattr(asyncio, "sleep", _counted)
        await link_of(coordinator).prime_held()

    assert slept == []
    assert lamp.exchanges == [
        ("write", NOTIFY_UUID),  # asked,
        ("read", NOTIFY_UUID),  # read, to no avail,
        ("read", INFO_UUID),  # and the exchange went on to its end
    ]
    assert coordinator.activated is None  # the flag never came: nothing written blind


@pytest.mark.parametrize(
    "the_flag_came_by",
    [
        "a report",
        "a read, from a lamp that says nothing",
        "a read, from a lamp that refuses",
        "a read, from a lamp only read",
    ],
)
async def test_a_lamp_that_says_it_is_not_activated_is_brought_up_in_that_exchange(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, the_flag_came_by: str
) -> None:
    """A plain no under the flag starts the bring-up, whichever way it came.

    Judged on the flag as the mirror holds it once the state request is
    over: reported, or read from a lamp that refuses the request - and from
    one that is no longer asked at all. Such a lamp was passed over: its
    flag was taken for one that cannot come, when the read brings it.
    """
    monkeypatch.setattr(coordinator_module, "_REPORT_TIMEOUT", 0.05)
    coordinator, lamp, link = await _holding_a_link(hass)
    if the_flag_came_by == "a report":
        lamp.answers({KEY_ACTIVATED: False})
    elif the_flag_came_by == "a read, from a lamp that says nothing":
        lamp.readable(NOTIFY_UUID, cbor.encode({KEY_ACTIVATED: False}))
    else:
        lamp.fails_writes(_REFUSAL, of=NOTIFY_UUID)
        lamp.readable(NOTIFY_UUID, cbor.encode({KEY_POWER: False}))
        if the_flag_came_by == "a read, from a lamp only read":
            for _ in range(3):
                await coordinator._request_state(turn_over(coordinator, link))
            lamp.forget()
        lamp.readable(NOTIFY_UUID, cbor.encode({KEY_ACTIVATED: False}))

    await link_of(coordinator).prime_held()  # the first exchange on this link

    commands = [
        cbor.decode(frame) for uuid, frame in lamp.written if uuid == WRITE_UUID
    ]
    assert commands[0] == {0x53: 300}
    assert commands[-1] == {KEY_ACTIVATED: True}
    assert coordinator.activated is True
    if the_flag_came_by == "a read, from a lamp only read":
        assert lamp.asked == []  # and it was not asked for it


async def test_a_flag_that_comes_after_the_exchange_is_acted_on_in_the_next(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lamp whose flag comes later than its answer is brought up next time.

    Nothing waits for it within the exchange. It is in the mirror by the
    next first exchange - on the next link, after the next connect - and
    judged then: the question was not settled, since only a plain yes
    settles it.
    """
    monkeypatch.setattr(coordinator_module, "_REPORT_TIMEOUT", 0.05)
    coordinator, lamp, link = await _holding_a_link(hass)
    lamp.answers({KEY_POWER: True}, only=(KEY_POWER,))  # an answer without the flag

    await link_of(coordinator).prime_held()
    lamp.say(cbor.encode({KEY_ACTIVATED: False}))  # and the flag, after it

    assert [uuid for uuid, _frame in lamp.written if uuid == WRITE_UUID] == []

    await link_of(coordinator).hang_up(link)
    await link_of(coordinator).connect()  # the next link, and its first exchange

    commands = [
        cbor.decode(frame) for uuid, frame in lamp.written if uuid == WRITE_UUID
    ]
    assert commands[0] == {0x53: 300}
    assert commands[-1] == {KEY_ACTIVATED: True}


async def test_the_bring_up_is_attempted_once_per_session(
    hass: HomeAssistant,
) -> None:
    """A lamp reporting 0x14 True is left alone, and the question stays settled.

    Having settled the activation question with a plain yes, the lamp is not
    judged again: a flag that read no later in the session - a glitch, a
    value of another kind - does not put an activated lamp through the
    sequence a second time.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    coordinator._mirror.echo({KEY_ACTIVATED: True})
    await coordinator._async_activate_if_needed(turn_over(coordinator, link))
    assert lamp.written == []  # not put through the bring-up
    assert coordinator._activation_checked is True

    # Settled. Even a later False must not restart the bring-up.
    coordinator._mirror.echo({KEY_ACTIVATED: False})
    await coordinator._async_activate_if_needed(turn_over(coordinator, link))
    assert lamp.written == []


@pytest.mark.parametrize("reads_as_yes", [1, "yes", b"\x01", [True]])
async def test_only_a_flag_that_says_yes_settles_the_bring_up(
    hass: HomeAssistant, reads_as_yes: Any
) -> None:
    """A lamp is taken for brought up on the flag, not on what reads as true.

    The question is settled once a session. A number or a text under the
    id settled it as a yes would, and when the lamp then said no - a
    factory reset in between - the bring-up never came.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    coordinator._mirror.echo({KEY_ACTIVATED: reads_as_yes})
    await coordinator._async_activate_if_needed(turn_over(coordinator, link))
    assert lamp.written == []  # nothing there says no: no bring-up

    coordinator._mirror.echo({KEY_ACTIVATED: False})
    await coordinator._async_activate_if_needed(turn_over(coordinator, link))

    assert len(lamp.written) == 3  # the question was still open


@pytest.mark.parametrize("reads_as_no", [0, "", b"", b"\x00", [], None])
async def test_only_a_flag_that_says_no_starts_the_bring_up(
    hass: HomeAssistant, reads_as_no: Any
) -> None:
    """The bring-up rewrites the clock and the activation: only on a plain no."""
    coordinator, lamp, link = await _holding_a_link(hass)
    coordinator._mirror.echo({KEY_ACTIVATED: reads_as_no})

    await coordinator._async_activate_if_needed(turn_over(coordinator, link))

    assert lamp.written == []


@pytest.mark.parametrize("stopped_by", ["an unload", "Home Assistant stopping"])
async def test_stopping_tears_everything_down(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, stopped_by: str
) -> None:
    """Stopping cancels all three watchers, drops the link, and takes no new one.

    Leaving any of them behind means a reload leaves the old coordinator
    reacting to advertisements and polling for reconnects alongside the new
    one, both competing for the lamp's single connection. A coordinator shut
    down with Home Assistant is as finished as one unloaded: left watching,
    it would answer the next advertisement by dialling the lamp again -
    while Home Assistant is on its way out, and after the one hang-up it
    will get.
    """
    cancels = {name: MagicMock() for name in ("bluetooth", "unavailable", "poll")}
    fake = MagicMock()
    fake.async_register_callback.return_value = cancels["bluetooth"]
    fake.async_track_unavailable.return_value = cancels["unavailable"]
    fake.async_address_present.return_value = True
    monkeypatch.setattr(keeper_module, "bluetooth", fake)
    monkeypatch.setattr(
        keeper_module, "async_track_time_interval", lambda *_a: cancels["poll"]
    )
    entry = _an_entry()
    # The first connect is left out: the link here is one a command takes.
    entry.async_create_background_task = lambda _hass, coro, _name: coro.close()
    coordinator, lamp = _kept_for(hass, entry)
    await coordinator.async_start()
    await coordinator.async_set_indicator(True)
    link = lamp.links[-1]
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)

    if stopped_by == "an unload":
        await coordinator.async_stop()
    else:
        coordinator.async_shutdown()
    await hass.async_block_till_done()

    for name, cancel in cancels.items():
        assert cancel.call_count == 1, f"{name} watcher was left running"
    assert link.hung_up == 1
    assert link_of(coordinator).diagnostics()["connected"] is False
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_power(True)
    assert lamp.dials == 1  # the link it was stopped with; none since

    # An entry can be unloaded after Home Assistant has begun to stop, and
    # that is a second stop: a watcher is not ended twice.
    await coordinator.async_stop()
    for name, cancel in cancels.items():
        assert cancel.call_count == 1, f"{name} watcher was ended twice"


async def test_the_link_and_the_device_half_each_log_under_their_own_name(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A line of the log is found in the module its logger names.

    That a link was made and primed is the link's to say; that the lamp
    refused the state request is the device half's. Debug logging turned on
    for the package shows both, which is what the documents tell an owner
    to turn on.
    """
    coordinator, lamp = _at_a_lamp(hass)
    _refusing(lamp)

    with caplog.at_level(logging.DEBUG, logger="custom_components.glowrium"):
        await link_of(coordinator).connect()

    def said_by(words: str) -> set[str]:
        return {
            record.name for record in caplog.records if words in record.getMessage()
        }

    assert said_by("connected and primed") == {"custom_components.glowrium.link"}
    assert said_by("state request refused") == {
        "custom_components.glowrium.coordinator"
    }


@pytest.mark.parametrize("present", [True, False], ids=["advertising", "absent"])
async def test_a_lamp_that_is_advertising_at_start_is_in_reach_from_the_start(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    present: bool,
) -> None:
    """What Home Assistant already knows of the lamp is taken as the watching begins.

    The entities are built right after, and on a weak link the lamp may not
    be heard again for seconds or connected to for minutes. Until then they
    go by what the scanners knew at the start - and there is nothing for the
    log to say. Starting with the lamp unplugged is the first time it is out
    of reach, and that is said: by its name, and with its address once - a
    lamp picked from the list has its address in its title already.
    """
    entry = _an_entry()
    entry.async_create_background_task = lambda _hass, coro, _name: coro.close()
    coordinator, _lamp = _kept_for(
        hass, entry, "Glowrium-G7_DDEEFF (AA:BB:CC:DD:EE:FF)"
    )
    fake = MagicMock()
    fake.async_register_callback.return_value = lambda: None
    fake.async_track_unavailable.return_value = lambda: None
    fake.async_address_present.return_value = present
    monkeypatch.setattr(keeper_module, "bluetooth", fake)

    with caplog.at_level(logging.INFO, logger=link_module.__name__):
        await coordinator.async_start()
    try:
        assert coordinator.available is present
        if present:
            assert _info_lines(caplog) == []
        else:
            (said,) = _info_lines(caplog)
            assert said.startswith(
                "Glowrium-G7_DDEEFF (AA:BB:CC:DD:EE:FF) is out of reach"
            )
    finally:
        await coordinator.async_stop()


async def test_the_remembered_ramp_survives_the_device_reporting(
    hass: HomeAssistant,
) -> None:
    """The user's ramp is remembered, and later reports must not overwrite it.

    The device resets its ramp when circadian is re-enabled, which is why it is
    remembered at all - so re-seeding it from every report would hand back
    exactly the value the memory exists to override.
    """
    coordinator, lamp = _at_a_lamp(hass)
    coordinator._mirror.echo({KEY_LIGHTING_MODE: 1})
    await coordinator.async_set_ramp(90)  # 5400 s = 0x1518
    assert coordinator._desired_ramp == bytes.fromhex("1518")

    coordinator._ingest(
        cbor.encode({KEY_RAMP: bytes.fromhex("0e10")})
    )  # device default
    assert coordinator._desired_ramp == bytes.fromhex("1518")  # still the user's

    lamp.written.clear()
    await coordinator.async_set_lighting_mode(5)
    sent = cbor.decode(lamp.written[-1][1])
    assert sent[KEY_RAMP] == bytes.fromhex("1518")  # and it is what gets re-applied


@pytest.mark.parametrize("filled", ["by hand", "by a report, some time ago"])
async def test_a_stale_mirror_does_not_vouch_for_a_failed_write(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, filled: str
) -> None:
    """Confirmation needs a fresh report, not a matching one.

    The mirror is never invalidated - a lost link clears the client, not the
    state - so it can be hours old. Asking a lamp to turn off while the stale
    mirror already says `off` would otherwise report success for a write
    that failed, leaving the lamp on and removing the only signal the user
    had that it is unreachable. The same mirror filled the way it is in
    life: by the lamp, which reported "off" some time ago. That report is
    the only one there is of what the command sets, and it is older than the
    command.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.05)
    if filled == "by hand":
        # what the lamp said, some time ago
        coordinator._mirror.echo({KEY_POWER: False})
    else:
        coordinator._ingest(cbor.encode({KEY_POWER: False}))  # some time ago
    lamp.fails_writes(BleakError("Not connected"))

    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_power(False)
    # A command asks for a bare link: nothing but the command was written.
    assert {uuid for uuid, _frame in lamp.written} == {WRITE_UUID}


async def test_what_else_was_written_meanwhile_vouches_for_no_command(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command that never reached the lamp has nothing to be vouched for.

    While it waited for the lock, a first exchange wrote the clock, and the
    lamp reported the very state the command asks for. Then the link was
    lost and the command got none. It used to be asked after all the same,
    because a write had been made since it was taken up - somebody else's -
    and was called delivered on the strength of that report: a command the
    lamp never received. It fails, and at once.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 30)
    stale = bytes.fromhex("07e80101000000")  # the exchange has a clock to write
    lamp.answers({KEY_TIME: stale, KEY_ACTIVATED: True}, after=0.01)

    def _reports_then_loses_the_link(_frame: bytes) -> bytes:
        hass.loop.call_soon(lamp.lose)  # the link goes, right after the report
        lamp.out_of_range()  # and the lamp is not heard again
        return cbor.encode({KEY_POWER: True})  # the very state the command asks for

    lamp.fails_writes(
        BleakError("Not connected"), of=WRITE_UUID, saying=_reports_then_loses_the_link
    )

    exchange = asyncio.create_task(link_of(coordinator).reconnect())
    await asyncio.sleep(0)  # a first exchange is under way
    command = asyncio.create_task(coordinator.async_set_power(True))
    await asyncio.sleep(0)  # taken up, and waiting for the lock
    await exchange

    async with asyncio.timeout(2):
        with pytest.raises(HomeAssistantError):
            await command
    # The exchange's writes - the request, the clock - and no other.
    assert [uuid for uuid, _frame in lamp.written] == [NOTIFY_UUID, WRITE_UUID]


async def test_a_command_that_failed_is_not_echoed_into_the_mirror(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror hears of a write once the lamp has acknowledged it.

    The echo is optimistic about one thing: that a write the lamp took was
    acted on. A write that failed is echoed nowhere - the entities would
    show a lamp switched on that never heard the command, under an error
    saying it could not be reached.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    coordinator._ingest(cbor.encode({KEY_POWER: False}))
    lamp.fails_writes(BleakError("down"))

    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_power(True)

    assert coordinator.state[KEY_POWER] is False


async def test_a_write_is_counted_whether_or_not_the_lamp_took_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The diagnostics count the writes put to the lamp, not those it took.

    Beside the count of its reports, that is what tells a lamp that hears
    nothing from one that is never spoken to. Counted before the write: a
    command whose two tries both failed was put to the lamp twice.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)

    await coordinator.async_set_power(True)
    assert coordinator.diagnostics()["link"]["writes_sent"] == 1

    lamp.fails_writes(BleakError("down"))
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_power(False)
    assert coordinator.diagnostics()["link"]["writes_sent"] == 3


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param("a1081828", id="a report about something else"),
        pytest.param("a208182809c000", id="a report read only in part"),
    ],
)
async def test_a_report_vouches_only_for_what_it_carries(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, frame: str
) -> None:
    """A report that is newer than the write is not thereby about the write.

    The mirror says the lamp is off, and is stale: it is on. Asked to turn
    it off, the write fails - and a moment later the lamp reports its
    brightness, as it does of its own accord all day. That report is fresh
    and says nothing about power. The mirror goes on matching the command
    only because nothing has corrected it, and the command was reported as
    delivered to a lamp that never got it.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.05)
    coordinator._mirror.echo({KEY_POWER: False})  # what the lamp said, some time ago
    # The write fails, and the lamp says something else.
    lamp.fails_writes(
        BleakError("Not connected"), saying=lambda _frame: bytes.fromhex(frame)
    )

    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_power(False)
    assert coordinator.state[KEY_BRIGHTNESS] == 40  # the report itself was taken
    # A command asks for a bare link: nothing but the command was written.
    assert {uuid for uuid, _frame in lamp.written} == {WRITE_UUID}


# What the device half keeps of Home Assistant: its two exceptions, which the
# entities and the service calls understand; the mark of a callback; the type
# of the event the stop comes as; and the clock, for the lamp's local time.
# A name added here is one more thing the device half knows of what runs it.
_OF_HOME_ASSISTANT = {
    ("homeassistant.core", "Event"),
    ("homeassistant.core", "callback"),
    ("homeassistant.exceptions", "HomeAssistantError"),
    ("homeassistant.exceptions", "ServiceValidationError"),
    ("homeassistant.util", "dt"),
}


def test_the_device_half_names_five_things_of_home_assistant_and_no_more() -> None:
    """The coordinator's module knows its keeper, and not what the keeper is.

    No instance of Home Assistant, no config entry, no Bluetooth integration,
    no registry of issues or of devices: each is behind the keeper (#54), and
    a coordinator cannot be built without one, so nothing in it asks whether
    Home Assistant is there. This reads the source, as the test of the seam
    with the link does: the seam erodes one import at a time, and a branch on
    an absent Home Assistant is a line nobody would write a test against.
    """
    tree = ast.parse(Path(coordinator_module.__file__).read_text(encoding="utf-8"))
    imported = {
        (node.module, one.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("homeassistant")
        for one in node.names
    }
    whole = [
        one.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for one in node.names
        if one.name.startswith("homeassistant")
    ]
    assert imported == _OF_HOME_ASSISTANT
    assert whole == []
    # Nor does it reach round its keeper for the one Home Assistant has: the
    # adapter is the setup's to build and hand in.
    siblings = {
        one.name if node.module is None else node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level
        for one in node.names
    }
    assert "keeper" not in siblings

    # Nor is anything kept by a name that was Home Assistant's or stood in
    # for it: the instance, the entry, the tasks the coordinator held itself,
    # the lookup of the lamp for a dial nobody handed in.
    gone = {"hass", "_entry", "_kept_tasks", "_run_without_hass", "_ble_device"}
    named = sorted(
        f"{node.lineno} {said}"
        for node in ast.walk(tree)
        for said in (
            getattr(node, "id", None),
            getattr(node, "attr", None),
            getattr(node, "arg", None),
            getattr(node, "name", None),
        )
        if said in gone
    )
    assert named == []

    # A coordinator is built with a keeper and a dial: neither has a default.
    (built,) = (
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "GlowriumCoordinator"
    )
    (init,) = (
        node
        for node in built.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    required = [
        arg.arg
        for arg, default in zip(
            init.args.kwonlyargs, init.args.kw_defaults, strict=True
        )
        if default is None
    ]
    assert required == ["keeper", "dial"]


async def test_work_is_started_through_the_keeper(hass: HomeAssistant) -> None:
    """The device half starts nothing itself: its keeper does, by the lifetime asked.

    A reconnect is work that has to end with whatever keeps the coordinator;
    a hang-up is work that must outlive it. Which is which is the device
    half's to say and the keeper's to honour, so each is asked for by name.
    """
    coordinator, lamp = _at_a_lamp(hass)
    keeper = keeper_of(coordinator)

    link_of(coordinator).tick()
    await hass.async_block_till_done()

    assert keeper.spawned == ["glowrium reconnect AA:BB:CC:DD:EE:FF"]
    assert keeper.lasting == []

    link_of(coordinator).hang_up(lamp.links[-1])
    await hass.async_block_till_done()

    assert keeper.lasting == ["glowrium hang up AA:BB:CC:DD:EE:FF"]
    assert keeper.spawned == ["glowrium reconnect AA:BB:CC:DD:EE:FF"]


@pytest.mark.parametrize("stopped_by", ["an unload", "Home Assistant stopping"])
async def test_the_lamp_is_watched_through_the_keeper(
    hass: HomeAssistant, stopped_by: str
) -> None:
    """The device half watches nothing itself: its keeper tells it, and the link hears.

    Whether the lamp is on the air as the watching begins, the lamp heard,
    the lamp gone, the clock come round: each is the keeper's to know, and
    what it says reaches the link as it is. Stopped either way, the device
    half asks to be told nothing more.
    """
    coordinator, lamp = _at_a_lamp(hass)
    lamp.out_of_range()  # every dial fails at once, and is counted
    keeper = keeper_of(coordinator)
    keeper.present = False

    await coordinator.async_start()
    await hass.async_block_till_done()  # the first connect, failing

    assert keeper.spawned == ["glowrium initial connect AA:BB:CC:DD:EE:FF"]
    assert not coordinator.available  # as the keeper said it was at the start

    keeper.hears(True)
    assert coordinator.available
    await hass.async_block_till_done()  # the reconnect that set off, failing

    keeper.hears(False)
    assert not coordinator.available

    dials = lamp.dials
    keeper.ticks()
    await hass.async_block_till_done()
    assert lamp.dials == dials + 1  # the tick dialled

    if stopped_by == "an unload":
        await coordinator.async_stop()
    else:
        coordinator.async_shutdown()
    assert not keeper.watching


def _a_protocol_error(
    code: BleakGATTProtocolErrorCode, wording: str
) -> BleakGATTProtocolError:
    """Return bleak's protocol error for ``code``, worded some other way."""
    error = BleakGATTProtocolError(code)
    error.args = (int(code), wording)  # the code as a number, the words reworded
    return error


_REWORDED = "GATT Protocol Error: put some other way"


@pytest.mark.parametrize(
    ("error", "readable", "is_a_refusal"),
    [
        pytest.param(
            BleakError("Insufficient authorization (8)"),
            True,
            True,
            id="authorization refused, by its words",
        ),
        pytest.param(
            BleakError("[org.bluez.Error.Failed] Not connected"),
            True,
            False,
            id="a link that died after the read",
        ),
        pytest.param(
            BleakError("Not connected"),
            False,
            False,
            id="a link that is dead to the read as well",
        ),
        pytest.param(
            BleakError("GATT Protocol Error: Unlikely Error"),
            True,
            False,
            id="an unlikely error, by its words",
        ),
        pytest.param(
            BleakError("something nobody has seen before"),
            True,
            False,
            id="something nobody has seen before",
        ),
        pytest.param(
            _a_protocol_error(
                BleakGATTProtocolErrorCode.INSUFFICIENT_AUTHORIZATION, _REWORDED
            ),
            True,
            True,
            id="authorization refused, by its code",
        ),
        pytest.param(
            _a_protocol_error(
                BleakGATTProtocolErrorCode.INSUFFICIENT_AUTHENTICATION, _REWORDED
            ),
            True,
            True,
            id="authentication refused, by its code",
        ),
        pytest.param(
            _a_protocol_error(
                BleakGATTProtocolErrorCode.WRITE_NOT_PERMITTED, _REWORDED
            ),
            True,
            True,
            id="write not permitted, by its code",
        ),
        pytest.param(
            _a_protocol_error(BleakGATTProtocolErrorCode.READ_NOT_PERMITTED, _REWORDED),
            True,
            True,
            id="read not permitted, by its code",
        ),
        pytest.param(
            _a_protocol_error(
                BleakGATTProtocolErrorCode.UNLIKELY_ERROR,
                "GATT Protocol Error: nothing to do with authorization",
            ),
            True,
            False,
            id="an unlikely error, by its code",
        ),
    ],
)
async def test_only_an_application_level_refusal_ends_the_asking(
    hass: HomeAssistant, error: BleakError, readable: bool, is_a_refusal: bool
) -> None:
    """End the asking on a refusal, never on anything merely unrecognised.

    Two attempts to tell the cases apart failed on real hardware. A
    successful read does not prove the device is there: seen on a real G7 in
    0.2.0, the read answered - the low property block arrived and six
    entities came alive - and the request then failed with "Not connected"
    three times running, because the link dropped in between (because of
    the read, as it turned out: see _request_state). Nor does
    ``is_connected``: measured on a G7, it still reported True at the moment
    the write failed with "not connected", and the disconnect callback
    arrived two seconds later. A weak link fails the read and the request
    alike, and counting that ended the asking on a perfectly good lamp that
    merely sat far from the adapter - seen forty seconds after start-up.

    So the test is inverted. Only an error that positively looks like the
    device answering "no" - an authorization or ATT protocol error - counts,
    and which ATT error it was is in the code bleak gives, not in the words
    that render it: told by the text, a refusal lasts as long as the wording
    does, and an error that only mentions authorization in passing is taken
    for one. The text is still what there is to go by where the error is
    not bleak's own - a Bluetooth proxy's, say. Everything else is treated
    as the link, and is not even counted, however often it happens: a
    working lamp no longer asked loses four properties silently, while asking an exotic
    device once too often costs a reconnect.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    if readable:
        lamp.readable(NOTIFY_UUID, cbor.encode({KEY_POWER: True}))
    lamp.fails_writes(error)

    for _ in range(9):
        await coordinator._request_state(turn_over(coordinator, link))

    # A lamp that refuses is asked three times and no more; anything else
    # is asked every time, however often it happens.
    assert len(lamp.asked) == (3 if is_a_refusal else 9)
    paused = coordinator.diagnostics()["link"]["state_request_paused"]
    assert paused is is_a_refusal


async def _the_warnings_that_name_the_lamp(
    coordinator: GlowriumCoordinator, caplog: pytest.LogCaptureFixture
) -> list[str]:
    """Draw each warning that names the lamp's model, and return what was said.

    Seven of them: a frame with trailing bytes, one with an item that cannot
    be read, one that cannot be decoded, one that decodes to nothing of use,
    more properties than are kept, a value that cannot be used, and a refused
    state request.
    """
    lamp = lamp_of(coordinator)
    await coordinator.async_set_indicator(True)  # a link to be asked on
    link = lamp.links[-1]
    _refusing(lamp)
    with caplog.at_level(logging.WARNING, logger=coordinator_module.__package__):
        lamp.say(bytes.fromhex("a106f5deadbeef"))  # trailing bytes
        lamp.say(_PARTLY_READABLE)
        lamp.say(bytes.fromhex("c0a106f5"))  # a map behind a tag: not decoded
        lamp.say(bytes.fromhex("a0"))  # a map with nothing in it
        flood(lamp.say, ids=65)  # one more than there is room for
        lamp.say(cbor.encode({KEY_INDICATOR: "on"}))  # text where a flag belongs
        for _ in range(3):  # the third refusal in a row is the one that is said
            await coordinator._request_state(turn_over(coordinator, link))
    said = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(said) == 7
    assert "trailing bytes" in said[0]
    assert "cannot read" in said[1]
    assert "could not be decoded" in said[2]
    assert "decodes to nothing" in said[3]
    assert "reports more properties than" in said[4]
    assert "cannot be used" in said[5]
    assert "refused the batched state request" in said[6]
    return said


@pytest.mark.parametrize(
    ("known_by", "named", "hidden"),
    [
        pytest.param(
            b"brand:INLEDCO;pkey:Glowrium-C051;devid:CST-0001;mac:x;version:4;;",
            "(model Glowrium-C051, firmware 4)",
            (),
            id="a G7, read",
        ),
        pytest.param(b"", "(model unknown, firmware unknown)", (), id="not read yet"),
        pytest.param(
            b"pkey:Glowrium-C064;version:1.10.2;;",
            "(model Glowrium-C064, firmware 1.10.2)",
            (),
            id="a G8, read",
        ),
        pytest.param(
            b"brand:INLEDCO;pkey:Glowrium-C051,devid:CST-0001;version:4,mac:A1B2C3;;",
            "(model not as expected, firmware not as expected)",
            ("CST-0001", "A1B2C3"),
            id="the serial and the address glued to the model and the firmware",
        ),
        pytest.param(
            "Glowrium-C051;devid:CST-0001",
            "(model not as expected, firmware unknown)",
            ("CST-0001",),
            id="a remembered model id with the serial glued to it",
        ),
    ],
)
async def test_a_warning_names_a_model_and_a_firmware_that_are_what_they_claim(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    known_by: bytes | str,
    named: str,
    hidden: tuple[str, ...],
) -> None:
    """Whoever reads the report needs to know which lamp it came from - and no more.

    A model id and a firmware are said only when they are what they claim.
    Both come out of the device-info string, where the serial number and the
    address sit beside them, and where one field ends is only what the
    parser made of the string. A lamp that separates its fields differently
    hands over one long field with the others inside it. Seven warnings name
    the model and the firmware, and each of them asks to be reported: the
    log is held to the shapes the diagnostics file is held to. Until the
    lamp is read, its model is what an earlier session stored (``known_by``
    a string rather than the device-info string), held to the same shape.
    """
    if isinstance(known_by, bytes):
        coordinator = ScriptedLamp().coordinator(hass)
        coordinator.device_info = _parse_device_info(known_by)
    else:
        coordinator = ScriptedLamp().coordinator(hass, model_id=known_by)
    for glued in hidden:  # the parser took it for the model, or the firmware
        assert glued in (coordinator.model_id or "") + (coordinator.sw_version or "")

    for said in await _the_warnings_that_name_the_lamp(coordinator, caplog):
        assert named in said
        for glued in hidden:
            assert glued not in said


async def test_a_value_that_cannot_be_used_is_said_once_for_its_property(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The owner of such a lamp sees `unknown`, and the log says why - once.

    The first report that carries, under a property, something that is not
    what the property means is said at warning level: the lamp, the
    property by the name it has here, and what to send. Nothing of the
    value - it is the lamp's to choose, and this is a line people post. The
    lamp reports all day, so a second one under the same property is not
    said; another property is said for itself.
    """
    coordinator = ScriptedLamp().coordinator(hass, model_id="Glowrium-C051")
    with caplog.at_level(logging.WARNING, logger=coordinator_module.__name__):
        coordinator._ingest(cbor.encode({KEY_RAMP: b"\xab" * 5}))
        coordinator._ingest(cbor.encode({KEY_RAMP: b"\xcd" * 6}))  # the ramp again
        coordinator._ingest(cbor.encode({KEY_POWER: True}))  # a value
        coordinator._ingest(cbor.encode({KEY_TIMER: "HomeNetwork-5G"}))
        coordinator._ingest(cbor.encode({KEY_RAMP: bytes.fromhex("0708")}))

    said = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(said) == 2
    assert "ramp" in said[0]
    assert "schedule" in said[1]
    for line in said:
        assert "cannot be used" in line
        assert "(model Glowrium-C051, firmware unknown)" in line
        assert "diagnostics" in line
        for of_the_value in ("abab", "cdcd", "HomeNetwork"):
            assert of_the_value not in line


@pytest.mark.parametrize(
    ("key", "name"),
    [
        (KEY_TIME, "clock"),
        (KEY_POWER, "power"),
        (KEY_BRIGHTNESS, "brightness"),
        (KEY_CIRCADIAN, "circadian"),
        (KEY_LATITUDE, "latitude"),
        (KEY_LONGITUDE, "longitude"),
        (KEY_SCHEDULE, "schedule mode"),
        (KEY_TIMER, "schedule"),
        (KEY_ACTIVATED, "activated"),
        (KEY_INDICATOR, "indicator"),
        (KEY_LIGHTING_MODE, "lighting mode"),
        (KEY_RAMP, "ramp"),
        (KEY_DST, "daylight saving"),
    ],
)
async def test_each_property_the_integration_reads_is_named_when_it_cannot_be_used(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, key: int, name: str
) -> None:
    """Whatever an entity shows or a command builds on, the log speaks for.

    Thirteen properties are read, and each goes unknown without a word on
    the device page when what the lamp reported under it cannot be used. A
    property left out of the table the warning goes by would do so in the
    log as well. Each is named by the name it has in the diagnostics
    download, with its id, and the text that stood under it is not repeated.
    """
    coordinator = ScriptedLamp().coordinator(hass)
    with caplog.at_level(logging.WARNING, logger=coordinator_module.__name__):
        coordinator._ingest(cbor.encode({key: "HomeNetwork-5G"}))

    (said,) = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert f"its {name} (0x{key:02x}) cannot be used" in said
    assert "HomeNetwork" not in said


async def test_a_clock_that_shows_no_date_is_put_right_and_not_reported(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning asks for a report of what this integration cannot handle.

    Seven bytes with no date in them it can: that is a clock that is wrong,
    and the first exchange sets it. A lamp that has never been given the
    time may well report one, and would be asked to report itself on every
    first connect for a thing that is mended in the same breath.
    """
    coordinator = ScriptedLamp().coordinator(hass)
    with caplog.at_level(logging.WARNING, logger=coordinator_module.__name__):
        coordinator._ingest(cbor.encode({KEY_TIME: bytes(7)}))

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert protocol.clock_is_no_date(coordinator.state)


async def test_what_was_written_and_not_reported_is_not_said_to_be_unusable(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning is about what the lamp reports, and only when it reports it.

    An echo of a write is the integration's own value. And a property that
    could not be used once is not said again each time some other property
    is reported: the lamp reports its brightness all day.
    """
    coordinator = ScriptedLamp().coordinator(hass)
    with caplog.at_level(logging.WARNING, logger=coordinator_module.__name__):
        coordinator._mirror.echo({KEY_DST: b"\x01"})  # not a report
        coordinator._ingest(cbor.encode({KEY_BRIGHTNESS: 70}))

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_the_request_is_repeated_on_every_connect(hass: HomeAssistant) -> None:
    """Whether to ask is not judged by what the mirror already holds.

    The mirror accumulates. Once a request has filled it in, a check against
    it is satisfied for ever and the lamp is never asked again - so anything
    changed from the vendor app while Home Assistant was disconnected stays
    invisible until a restart, which is the opposite of what a reconnect is
    for.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    lamp.answers()

    await coordinator._request_state(turn_over(coordinator, link))
    assert len(lamp.asked) == 1
    assert all(key in coordinator.state for key in STATE_KEYS)  # all known now

    await coordinator._request_state(turn_over(coordinator, link))
    assert len(lamp.asked) == 2, "a later connect must ask again"
    assert lamp.read == []


@pytest.mark.parametrize(
    "stale",
    [
        pytest.param(bytes.fromhex("07ea02010f0e2c"), id="months out"),  # 2026-02-01
        pytest.param(bytes.fromhex("07ea0d12151823"), id="no date at all"),  # month 13
    ],
)
async def test_a_stale_device_clock_is_corrected(
    hass: HomeAssistant, stale: bytes
) -> None:
    """A lamp whose clock has drifted is put right on connect, in local time.

    The clock was only ever written during first-time bring-up, so a lamp
    set up months ago runs its schedule and its circadian curve off whatever
    date it had then - one reporter's was six months out (issue #4). Nothing
    surfaces it either, because the clock is not an entity. Thirteen months
    is a report too, and a wrong one: it is set right, not left - to a date
    the lamp can keep.
    """
    await hass.config.async_set_time_zone("Asia/Kolkata")  # 5 h 30 min from UTC
    coordinator, lamp, link = await _holding_a_link(hass)
    coordinator._mirror.echo({KEY_TIME: stale})

    await coordinator._async_sync_clock_if_needed(turn_over(coordinator, link))

    assert len(lamp.written) == 1
    written = cbor.decode(lamp.written[-1][1])
    assert written[KEY_TIME] != stale
    assert written[KEY_TIME_SYNCED] == 1
    corrected = protocol.device_time(written)
    assert corrected is not None
    local = dt_util.now().replace(tzinfo=None)  # wall-clock time, not UTC
    assert abs((corrected - local).total_seconds()) < 5


@pytest.mark.parametrize("clock", ["near enough", "never read"])
async def test_a_clock_that_is_right_or_unread_is_left_alone(
    hass: HomeAssistant, clock: str
) -> None:
    """The clock is written only on a drift that can be judged, and is too large.

    Writing on every connect would cost a write an hour for nothing: the
    lamp reconnects itself every half hour or so, and correcting a clock
    that is seconds out would mean a write each time, on a link that is the
    scarce resource here. And with nothing read back there is no drift to
    judge and nothing to fix: a blind write would be guessing at what the
    lamp currently believes.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    if clock == "near enough":
        coordinator._mirror.echo({KEY_TIME: protocol.encode_device_time(dt_util.now())})
    else:
        assert KEY_TIME not in coordinator.state

    await coordinator._async_sync_clock_if_needed(turn_over(coordinator, link))

    assert lamp.written == []


@pytest.mark.parametrize(
    "kept",
    [
        pytest.param(bytes.fromhex("07ea0712151823") + b"\x00", id="a byte more"),
        pytest.param(
            bytes.fromhex("07ea0d12151823") + b"\x00",
            id="a byte more, and no date in the first seven",
        ),
        pytest.param(bytes.fromhex("07ea0712"), id="cut short"),
        pytest.param("2026-07-18T21:24:35", id="as text"),
        pytest.param(1784409875, id="as a number"),
        pytest.param([None], id="an array holding null"),
    ],
)
async def test_a_clock_kept_in_some_other_shape_is_not_written_over(
    hass: HomeAssistant, kept: Any
) -> None:
    """Seven bytes written over a clock kept another way would be a guess.

    A clock of seven bytes that shows no date is a clock that is wrong, and
    is set right. Anything else under the id is not a clock this
    integration knows how a lamp keeps: it is left alone, as a clock never
    reported is. One of more than seven bytes used to be read by its first
    seven, found months out, and written over.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    coordinator._mirror.echo({KEY_TIME: kept})

    await coordinator._async_sync_clock_if_needed(turn_over(coordinator, link))

    assert lamp.written == []


async def test_both_priming_paths_check_the_clock(hass: HomeAssistant) -> None:
    """A background connect corrects the clock too, not only the poll's priming.

    Most connects on a healthy lamp are background reconnects; if only the poll
    checked, a lamp whose link is good enough never to need re-priming would
    keep a stale clock for ever.
    """
    for path in ("a background connect", "the poll"):
        if path == "the poll":
            coordinator, lamp, _link = await _holding_a_link(hass)
        else:
            coordinator, lamp = _at_a_lamp(hass)
        checked = 0

        async def _note(_turn: object) -> None:
            nonlocal checked
            checked += 1

        coordinator._async_sync_clock_if_needed = _note
        coordinator._request_state = AsyncMock(return_value=True)
        lamp.readable(INFO_UUID, b"brand:x;;")
        coordinator._async_activate_if_needed = AsyncMock()

        if path == "the poll":
            await link_of(coordinator).prime_held()
        else:
            await link_of(coordinator).connect()

        assert checked == 1, path


@pytest.mark.parametrize(
    ("remembered", "info", "said"),
    [
        pytest.param(
            None,
            b"brand:INLEDCO;pkey:Glowrium-C051;devid:CST-0001;version:4;;",
            ("Glowrium-C051", "4", "CST-0001"),
            id="all of it",
        ),
        pytest.param(
            None,
            b"pkey:Glowrium-C051;version:4;;",
            ("Glowrium-C051", "4", None),
            id="what it said, and no more",
        ),
        pytest.param(
            "Glowrium-C051",
            b"version:4;;",
            ("Glowrium-C051", "4", None),
            id="the model as an earlier session read it",
        ),
    ],
)
async def test_what_the_lamp_says_it_is_is_kept_and_handed_to_the_keeper(
    remembered: str | None, info: bytes, said: tuple[str | None, ...]
) -> None:
    """What the lamp says about itself is kept, and whatever keeps it is told.

    Home Assistant's keeper puts it in the device registry and remembers the
    model with the config entry; the bench has neither, and the string is
    read and kept all the same. The model is said as the device half knows
    it: read now, or as it was remembered when the lamp does not name it.
    """
    lamp = ScriptedLamp()
    coordinator = lamp.coordinator(None, "bench", model_id=remembered)
    await coordinator.async_set_indicator(True)  # a link, taken by a command
    lamp.readable(INFO_UUID, info)

    await coordinator._async_read_device_info(turn_over(coordinator, lamp.links[-1]))

    assert coordinator.model_id == "Glowrium-C051"
    assert coordinator.sw_version == "4"
    assert keeper_of(coordinator).described == [said]
