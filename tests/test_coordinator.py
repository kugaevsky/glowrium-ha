"""Tests for the Glowrium coordinator's command encoding."""

import asyncio
from collections.abc import Callable
import contextlib
from datetime import timedelta
import logging
from time import monotonic
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from bleak.exc import BleakError, BleakGATTProtocolError, BleakGATTProtocolErrorCode
from bleak_retry_connector import BLEAK_TIMEOUT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.glowrium import (
    cbor,
    const,
    coordinator as coordinator_module,
    diagnostics,
    link as link_module,
    protocol,
)
from custom_components.glowrium.const import (
    DST_OFF,
    DST_ON,
    INFO_UUID,
    KEY_ACTIVATED,
    KEY_BRIGHTNESS,
    KEY_CIRCADIAN,
    KEY_DST,
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
    TIMER_BRIGHTNESS,
    TIMER_END_H,
    TIMER_END_M,
    TIMER_GRADUAL,
    TIMER_START_H,
    TIMER_START_M,
    WRITE_UUID,
)
from custom_components.glowrium.coordinator import (
    GlowriumCoordinator,
    _parse_device_info,
)

from .lamp import (
    LampLink,
    ScriptedLamp,
    flood,
    lamp_of,
    link_of,
    nothing_heard,
    turn_over,
)


def _at_a_lamp(
    hass: HomeAssistant | None, name: str = "Glowrium-G7"
) -> tuple[GlowriumCoordinator, ScriptedLamp]:
    """Return a coordinator at a scripted lamp in range, with no link yet.

    A command dials the lamp bare - the link and the command's own write,
    nothing else - so what the lamp has been written is the command and only
    that (``lamp.written``). The link is taken on the first command, and the
    entities are told of it then.
    """
    lamp = ScriptedLamp()
    return lamp.coordinator(hass, name), lamp


async def _holding_a_link(
    hass: HomeAssistant | None, name: str = "Glowrium-G7"
) -> tuple[GlowriumCoordinator, ScriptedLamp, LampLink]:
    """Return a coordinator holding a bare link to a scripted lamp, and that link.

    Taken the way a command takes one: nothing asked of the lamp and nothing
    read, as on a link no background connect has run on. The command's own
    write and its echo are cleared away, so the mirror reads as unread and
    ``lamp.written`` starts empty - what is written next is the test's.
    """
    coordinator, lamp = _at_a_lamp(hass, name)
    await coordinator.async_set_indicator(True)
    lamp.written.clear()
    lamp.exchanges.clear()
    nothing_heard(coordinator)  # the command's echo gone; a mirror forgets nothing
    return coordinator, lamp, lamp.links[-1]


def _refusing(lamp: ScriptedLamp) -> None:
    """Make ``lamp`` one that refuses the state request and can be read instead.

    That is the shape of a model that refuses: issue #3 shows a G8 serving a
    twenty-key read while answering the request with an ATT error and dropping
    the link.
    """
    lamp.fails_writes(BleakError("Insufficient authorization (8)"))
    lamp.readable(NOTIFY_UUID, cbor.encode({KEY_POWER: True}))


# What a call receives when its client's bus is closed underneath it. Run against
# dbus-fast with a bus that is shut while a call is waiting for its reply: the
# call ends in EOFError, and with the socket gone, in "Bad file descriptor".
# Neither is a BleakError, and bleak passes both on as they are.
_BUS_CLOSED = (EOFError(), OSError(9, "Bad file descriptor"))


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
            None, DST_ON, bytes.fromhex("a11835450100000e10"), id="nothing reported yet"
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
    hass.config.latitude = zero
    hass.config.longitude = zero
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
    hass.config.latitude = latitude
    hass.config.longitude = longitude
    await coordinator.async_sync_location()
    assert lamp.written == [
        (WRITE_UUID, cbor.encode({0x0A: float(latitude), 0x0B: float(longitude)}))
    ]


async def test_sync_location_writes_nothing_without_home_assistant() -> None:
    """The bench has no home to hand over, and the command says nothing of it.

    tools/bench.py builds the real coordinator with ``hass=None``; the command
    returns there as it always did, whatever else it now refuses.
    """
    coordinator, lamp, _link = await _holding_a_link(None, "bench")
    await coordinator.async_sync_location()
    assert lamp.written == []


@pytest.mark.parametrize(
    ("setter", "args", "changed"),
    [
        pytest.param(
            "async_set_timer_start",
            (7, 30),
            {TIMER_START_H: 7, TIMER_START_M: 30},
            id="start",
        ),
        pytest.param(
            "async_set_timer_end",
            (19, 45),
            {TIMER_END_H: 19, TIMER_END_M: 45},
            id="end",
        ),
        pytest.param(
            "async_set_timer_brightness", (37,), {TIMER_BRIGHTNESS: 37}, id="brightness"
        ),
        pytest.param(
            "async_set_timer_gradual",
            (5,),
            {TIMER_GRADUAL: 0x01, TIMER_GRADUAL + 1: 0x2C},  # 5 min = 300 s = 0x012c
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


async def test_available_follows_presence_not_connection(hass: HomeAssistant) -> None:
    """Availability tracks presence or a live link, so it does not flap on reconnect."""
    coordinator, lamp = _at_a_lamp(hass)
    assert coordinator.available is False  # neither present nor connected
    link_of(coordinator).begin(present=True)
    assert coordinator.available is True  # advertising -> available
    link_of(coordinator).advertising(False)
    await coordinator.async_set_power(True)  # a link is taken
    assert coordinator.available is True  # connected -> available
    lamp.links[0].is_connected = False
    assert coordinator.available is False  # link dropped and gone -> unavailable


async def test_presence_callbacks_notify(hass: HomeAssistant) -> None:
    """Advertisement/unavailable callbacks flip presence and notify listeners."""
    coordinator, lamp = _at_a_lamp(hass)
    lamp.out_of_range()  # the reconnect an advertisement sets off comes to nothing
    updates: list[int] = []
    coordinator.async_add_listener(lambda: updates.append(1))
    link_of(coordinator).advertising(True)
    assert link_of(coordinator).diagnostics()["advertising"] is True
    link_of(coordinator).advertising(False)
    assert link_of(coordinator).diagnostics()["advertising"] is False
    assert updates == [1, 1]  # notified on the present flip and on going away
    await hass.async_block_till_done()  # the reconnect, failing at debug level


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


@pytest.mark.parametrize("behind_it", ["Home Assistant", "nothing: the bench"])
@pytest.mark.parametrize(
    "failure",
    [BleakError("dropped"), *_BUS_CLOSED],
    ids=["dropped", "eof", "bad-fd"],
)
async def test_write_retries_once_after_a_dropped_link(
    hass: HomeAssistant, behind_it: str, failure: Exception
) -> None:
    """A write that fails once hangs up its link, reconnects, retries and succeeds.

    When the lamp drops the link, the disconnected callback hangs the client
    up at once, which closes its D-Bus connection. A write still waiting for
    its reply on that connection does not get the BleakError a lost link
    usually produces: it gets whatever the bus raised - EOFError, and with
    the socket gone "Bad file descriptor". Caught as nothing in particular,
    that went straight out of the command: no retry, and a bare EOFError
    where the user should read "cannot connect". Without Home Assistant
    behind the coordinator too: a command on a link that drops under it is
    what tools/bench.py exists to exercise, and the retry waits for the
    hang-up before it dials again (0.2.1 scheduled that hang-up on hass).
    """
    with_hass = behind_it == "Home Assistant"
    coordinator, lamp, first = await _holding_a_link(
        hass if with_hass else None, "Glowrium-G7" if with_hass else "bench"
    )
    lamp.fails_writes(failure, times=1)

    await coordinator.async_set_power(True)

    assert len(lamp.written) == 2  # failed, then retried
    assert lamp.dials == 2  # a reconnect happened before the retry
    assert coordinator.state[KEY_POWER] is True
    assert first.hung_up == 1
    assert link_of(coordinator).client is lamp.links[1]


@pytest.mark.parametrize(
    ("failure", "said"),
    [
        pytest.param(BleakError("Not connected"), "Not connected", id="a lost link"),
        pytest.param(EOFError(), "EOFError()", id="eof"),
        pytest.param(
            OSError(9, "Bad file descriptor"),
            "[Errno 9] Bad file descriptor",
            id="bad-fd",
        ),
    ],
)
async def test_write_raises_after_two_failures(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, failure: Exception, said: str
) -> None:
    """A write that keeps failing is reported as a readable HA error.

    Silence is not success: with no confirmation the error still surfaces,
    in the user's words - and when the retry meets the same bus closed under
    it as the first try did, the same words. The error is kept: as what the
    link made of it, and under that as the library raised it.
    """
    coordinator, lamp = _at_a_lamp(hass)
    # Nothing will confirm this write, so do not sit out the whole grace window.
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    lamp.fails_writes(failure)

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_power(True)
    assert err.value.translation_key == "cannot_connect"
    assert isinstance(err.value.__cause__, link_module.LinkLostError)
    assert str(err.value.__cause__) == said
    assert isinstance(err.value.__cause__.__cause__, type(failure))
    assert len(lamp.written) == 2  # tried twice, then gave up


async def test_a_model_that_keeps_refusing_is_left_alone_for_the_session(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusing model is asked a run, paused, asked one more run, then left alone.

    A G8 answers the request with ATT "Insufficient authorization" (0x08)
    every time, and asking on every connect gets nothing from it - but it
    takes a run of refusals, not one (see the dropped-link test below): on a
    real G7 three consecutive failures accumulated 70 s after start-up
    purely from a bad link, and making the silence permanent cost the lamp
    four properties until Home Assistant was restarted. So the first run
    pauses the request for a cooldown, and the lamp is not asked while it
    lasts: the cooldown gives that run the benefit of the doubt. A model
    that refuses again once it expires is refusing, not unlucky, and asking
    it again gets nothing - it is not asked again this session.
    """
    monkeypatch.setattr(coordinator_module, "_STATE_REQUEST_COOLDOWN", 0.05)
    coordinator, lamp, link = await _holding_a_link(hass, "Glowrium-G8")
    # A refusal is told by what the error says. The read is what such a lamp
    # is primed by instead.
    _refusing(lamp)
    attempts = coordinator_module._STATE_REQUEST_ATTEMPTS

    for _ in range(attempts):
        await coordinator._request_state(turn_over(coordinator, link))
    assert len(lamp.asked) == attempts
    assert coordinator._state_request_muted is True

    # Later connects must not re-send it while the pause lasts.
    await coordinator._request_state(turn_over(coordinator, link))
    await coordinator._request_state(turn_over(coordinator, link))
    assert len(lamp.asked) == attempts

    await asyncio.sleep(0.06)  # the cooldown expires
    assert coordinator._state_request_muted is False
    await coordinator._request_state(turn_over(coordinator, link))
    assert len(lamp.asked) == attempts + 1  # and it asks again
    for _ in range(attempts - 1):  # a second run of refusals
        await coordinator._request_state(turn_over(coordinator, link))
    assert coordinator._state_request_muted is True

    sent = len(lamp.asked)
    await asyncio.sleep(0.06)
    assert coordinator._state_request_muted is True  # this time for good
    await coordinator._request_state(turn_over(coordinator, link))
    assert len(lamp.asked) == sent  # never again this session


async def test_one_dropped_link_does_not_abandon_the_state_request(
    hass: HomeAssistant,
) -> None:
    """A transient failure must not cost the session its unread properties.

    A dropped connection raises the same BleakError as an outright refusal, and
    on a weak link it happens routinely - a real G7 hit it 40 s after start-up.
    Abandoning the request there left the indicator, lighting mode, ramp and DST
    unread until Home Assistant was restarted.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    lamp.fails_writes(BleakError("[org.bluez.Error.Failed] Not connected"), times=1)

    assert (
        await coordinator._request_state(turn_over(coordinator, link)) is False
    )  # nothing answered, and nothing to read
    assert coordinator._state_request_muted is False  # and one failure means nothing
    assert coordinator._state_request_failures == 0  # it is not even counted

    # And an answer clears the count, so that the odd refusal on a bad link
    # never adds up to a silenced request.
    coordinator._state_request_failures = coordinator_module._STATE_REQUEST_ATTEMPTS - 1
    lamp.answers()
    await coordinator._request_state(turn_over(coordinator, link))
    assert coordinator._state_request_failures == 0


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
    assert coordinator._state_request_muted is False


async def test_activation_skipped_when_state_unreadable(hass: HomeAssistant) -> None:
    """A device whose state cannot be read is never activated, and never waits.

    0x14 can never arrive on such a device, so the 3 s wait would run on every
    connect - including the connect the command path performs.
    """
    coordinator, _lamp, link = await _holding_a_link(hass, "Glowrium-G8")
    coordinator._state_request_muted_until = monotonic() + 60
    activated = []
    coordinator._async_activate = AsyncMock(
        side_effect=lambda _turn: activated.append(1)
    )

    await coordinator._async_activate_if_needed(turn_over(coordinator, link))

    assert not activated  # must not replay the vendor bring-up blind
    assert coordinator._activation_checked is True  # and must not re-wait


@pytest.mark.parametrize(
    ("read", "carried", "exchange", "asked"),
    [
        pytest.param(
            bytes.fromhex("a206f5081846"),
            {KEY_POWER: True, KEY_BRIGHTNESS: 70},
            [("read", NOTIFY_UUID), ("write", NOTIFY_UUID)],
            1,
            id="a read covering part of the map",
        ),
        pytest.param(
            cbor.encode(dict.fromkeys(STATE_KEYS, 0)),
            dict.fromkeys(STATE_KEYS, 0),
            [("read", NOTIFY_UUID)],
            0,
            id="a read covering every key",
        ),
        pytest.param(
            None,
            {},
            [("read", NOTIFY_UUID), ("write", NOTIFY_UUID)],
            1,
            id="a read that fails",
        ),
    ],
)
async def test_a_lamp_read_first_is_asked_for_what_the_read_did_not_carry(
    hass: HomeAssistant,
    read: bytes | None,
    carried: dict[int, Any],
    exchange: list[tuple[str, str]],
    asked: int,
) -> None:
    """For a lamp that is read, a read covering part of the map is not the end.

    A lamp that has refused the request is read first from then on, as every
    lamp was before - and read before it is asked again: it was reported of
    a G8 that the refused request takes the link with it, and then there is
    nothing left to read. Measured on a real one: the read carries only the
    low property block, so the indicator (0x17), lighting mode (0x2b), ramp
    (0x2f) and DST (0x35) are absent from it and arrive solely through the
    request. Treating the read as the whole story left those four entities
    `unknown` for the entire session, so the request is repeated until it is
    silenced - judged on what each read carried, not on the mirror, which
    accumulates. A read that already has everything is not followed by the
    request, this time or the next; a read that fails still leaves it.
    """
    coordinator, lamp, link = await _holding_a_link(hass, "Glowrium-G8")
    coordinator._state_request_failures = 1  # refused once: read first
    _refusing(lamp)
    lamp.readable(NOTIFY_UUID, read)

    await coordinator._request_state(turn_over(coordinator, link))
    await coordinator._request_state(turn_over(coordinator, link))  # and next time

    assert lamp.exchanges == exchange * 2
    assert lamp.asked == [bytes(STATE_KEYS)] * (asked * 2)
    assert coordinator.state == carried


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
    assert coordinator._state_request_failures == 1


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
    out. A name added and left out of the set would be an id the mirror may
    have no room for.
    """
    named = {value for name, value in vars(const).items() if name.startswith("KEY_")}
    assert named <= KNOWN_KEYS
    assert set(STATE_KEYS) <= KNOWN_KEYS
    assert {*diagnostics._READ, *diagnostics._WHERE} <= KNOWN_KEYS


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
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G8")
    coordinator._on_notify(None, bytearray.fromhex("a306f5081846"))  # promises 3, has 2
    assert coordinator.state[KEY_POWER] is True
    assert coordinator.state[KEY_BRIGHTNESS] == 70


async def test_empty_ramp_does_not_latch(hass: HomeAssistant) -> None:
    """A zero-length ramp must not block seeding the remembered ramp later."""
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
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


@pytest.mark.parametrize(
    "held_up_by", ["a dial that never connects", "the lock a background connect holds"]
)
async def test_command_gives_up_instead_of_hanging(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, held_up_by: str
) -> None:
    """An unreachable device fails the command promptly, not after minutes.

    bleak's own retries can keep a connect attempt alive for minutes, which
    made a button in the UI look like it had hung; the command budget caps
    it. A command blocked by a background reconnect gives up too: the
    reconnect holds the link's lock while it dials, so the budget has to
    cover the wait for the lock, not just the write itself.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_COMMAND_TIMEOUT", 0.05)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    lamp.dials_when(asyncio.Event())  # a dial that never completes
    connecting = None
    if held_up_by == "the lock a background connect holds":
        connecting = asyncio.create_task(link_of(coordinator).connect())
        await asyncio.sleep(0)  # it holds the lock, dialling
    try:
        with pytest.raises(HomeAssistantError) as err:
            await coordinator.async_set_power(True)
    finally:
        if connecting is not None:
            connecting.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await connecting
    assert err.value.translation_key == "cannot_connect"


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
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")

    with caplog.at_level(logging.DEBUG, logger=coordinator_module.__name__):
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


async def test_setup_is_not_held_by_a_connect_that_never_finishes(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connect to an unreachable lamp must not hold the connection lock open.

    Setup awaited this path once. When the reconnect poll held the lock while
    grinding through attempts to a lamp that was out of range, setup waited on
    that lock with no deadline and the entry stayed in "setup in progress"
    forever - never even reaching setup_retry.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONNECT_TIMEOUT", 0.05)
    # The other holder: a command dialling a lamp that is never found.
    lamp.dials_when(asyncio.Event())
    command = asyncio.create_task(coordinator.async_set_power(True))
    await asyncio.sleep(0)
    try:
        with pytest.raises(TimeoutError):
            await link_of(coordinator).connect()
    finally:
        command.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await command


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


async def test_a_command_connect_reads_nothing(hass: HomeAssistant) -> None:
    """A command's own connect asks for a bare link: it writes, and reads nothing.

    Not even the model: it used to read the device-info string on its way to
    the write when the model was not known yet - a read between a button and
    its lamp, and on BlueZ a link with two seconds to live. Nor does it pay
    for priming first: that costs a device-info read, a state read, the
    batched request and up to 3 s waiting for the activation flag, all before
    the write and all inside the command budget, and on a lamp where the
    connect alone is marginal that is what turned a working command into a
    reported failure. A link that was primed would have had the state
    request written to it first.
    """
    coordinator, lamp = _at_a_lamp(hass)

    await coordinator.async_set_power(True)

    assert lamp.dials == 1
    assert lamp.exchanges == [("write", WRITE_UUID)]  # nothing read, nothing asked
    assert lamp.read == []
    assert len(lamp.written) == 1


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


async def test_a_first_exchange_on_a_held_link_does_not_wait_for_ever(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exchange the poll makes on a command's link has a ceiling too.

    It takes the lock. A lamp that never acknowledges the state request would
    hold it for good, and every command after that would spend its whole
    budget waiting behind a question nobody is going to answer. The link is
    left as it was - held, and without its first exchange - for the next
    tick.
    """
    monkeypatch.setattr(link_module, "_ASK_TIMEOUT", 0.05)
    coordinator, lamp, link = await _holding_a_link(hass)
    lamp.never_acknowledges_a_write()

    async with asyncio.timeout(2):
        await link_of(coordinator).prime_held()

    assert not link_of(coordinator).lock.locked()
    assert link_of(coordinator).client is link
    assert link_of(coordinator).diagnostics()["primed"] is False


async def test_two_ticks_do_not_make_the_first_exchange_twice(
    hass: HomeAssistant,
) -> None:
    """An exchange still waiting for the lock when the first is over is dropped.

    The tick does not know that the exchange the last tick began is still
    under way, and begins another. Once it has the lock, the second looks at
    whether the link has had its exchange meanwhile.
    """
    coordinator, lamp, _link = await _holding_a_link(hass)
    # Long enough for the next tick to queue up behind this one.
    lamp.answers({KEY_ACTIVATED: True}, after=0.01)
    lamp.readable(INFO_UUID, b"brand:x;;")

    link_of(coordinator).tick()
    link_of(coordinator).tick()
    await hass.async_block_till_done()

    assert len(lamp.asked) == 1


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


async def test_the_wait_for_the_activation_flag_ends_with_the_link(
    hass: HomeAssistant,
) -> None:
    """A lamp that has not said whether it is activated is waited for, briefly.

    For as long as there is a link to hear it on. A link that goes while the
    exchange waits leaves nothing to wait for - and nothing is written blind
    to a lamp whose flag was never read.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    waiting = asyncio.create_task(
        coordinator._async_activate_if_needed(turn_over(coordinator, link))
    )
    await asyncio.sleep(0)
    assert not waiting.done()  # the flag has not come, and it waits

    link.is_connected = False  # the link goes
    async with asyncio.timeout(1):  # and the wait with it, at its next look
        await waiting

    assert coordinator._activation_checked is False
    assert lamp.written == []


async def test_the_bring_up_is_attempted_once_per_session(
    hass: HomeAssistant,
) -> None:
    """A lamp reporting 0x14 True is left alone, and the question stays settled.

    The check costs up to 3 s waiting for 0x14, and it runs on every
    connect, so repeating it would put that on the command path for the
    whole session. Having settled the activation question, the lamp is not
    re-interrogated.
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


async def test_a_failed_reconnect_does_not_wedge_reconnection(
    hass: HomeAssistant,
) -> None:
    """One failed attempt must not stop the lamp being retried.

    The poll is the only thing that gets a dropped link back, and it refuses
    to start a second attempt while one is in flight. If a failure left that
    flag set, the lamp would never be reconnected again for the rest of the
    session - on this hardware failures are the normal case, not the rare one.
    """
    coordinator, lamp = _at_a_lamp(hass)
    lamp.out_of_range()

    link_of(coordinator).tick()
    await hass.async_block_till_done()
    assert lamp.dials == 1

    link_of(coordinator).tick()
    await hass.async_block_till_done()
    assert lamp.dials == 2  # and again, and again


async def test_advertisements_do_not_start_a_connect_storm(
    hass: HomeAssistant,
) -> None:
    """Only one connect at a time, however fast the lamp advertises.

    Advertisements arrive about once a second; starting a connect for each
    would pile them onto a device that allows exactly one connection.
    """
    coordinator, lamp = _at_a_lamp(hass)
    release = asyncio.Event()

    async def _hangs(*_a: object, **_kw: object) -> None:
        await release.wait()
        raise BleakError("gone again")

    lamp.dials_through(_hangs)

    for _ in range(5):
        link_of(coordinator).advertising(True)
    link_of(coordinator).tick()  # the poll must not add one either
    await asyncio.sleep(0)
    assert lamp.dials == 1

    release.set()
    await hass.async_block_till_done()


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
    coordinator, lamp, link = await _holding_a_link(hass)
    cancels = {name: MagicMock() for name in ("bluetooth", "unavailable", "poll")}
    coordinator._cancel_bluetooth = cancels["bluetooth"]
    coordinator._cancel_unavailable = cancels["unavailable"]
    coordinator._cancel_poll = cancels["poll"]
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


async def test_a_write_on_a_turn_whose_link_is_gone_is_refused_not_dropped(
    hass: HomeAssistant,
) -> None:
    """Writing on a turn whose link has gone must raise, so the retry and the user hear.

    Returning quietly would make every command look like it succeeded while
    nothing reached the lamp. Every write of the device half goes on a turn;
    the raw write the bench once had for this went with #21, stage 3.
    """
    coordinator, _lamp, link = await _holding_a_link(hass)
    turn = turn_over(coordinator, link)
    link.lose()

    with pytest.raises(link_module.LinkLostError):
        await turn.write(WRITE_UUID, cbor.encode({KEY_POWER: True}))


async def test_the_entities_are_told_when_a_link_is_taken(hass: HomeAssistant) -> None:
    """A link is half of what the entities' reach goes by, and they hear of it.

    Availability is "advertising, or a link". A lamp that is linked while it
    is not heard advertising is in reach from the moment the link is held -
    not from the end of the first exchange seconds later, and not from
    whenever the lamp next has something to report.
    """
    coordinator, lamp = _at_a_lamp(hass)
    lamp.never_acknowledges_a_write()  # asked, never answered
    seen: list[bool] = []
    coordinator.async_add_listener(lambda: seen.append(coordinator.available))

    connecting = asyncio.create_task(link_of(coordinator).connect())
    try:
        for _ in range(5):  # dialled, subscribed, waiting on its first question
            await asyncio.sleep(0)
        assert seen == [True]
    finally:
        connecting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await connecting


async def test_a_connect_that_waited_behind_a_command_dials_nothing(
    hass: HomeAssistant,
) -> None:
    """A background connect that gets the lock after a command took a link stops.

    Both take the same lock, and the connect looked before it began to wait.
    Dialling on what it saw then would put a second client in place of the
    first, which nothing would ever hang up - on a lamp with one slot.
    """
    coordinator, lamp = _at_a_lamp(hass)
    found = asyncio.Event()
    lamp.dials_when(found)

    command = asyncio.create_task(coordinator.async_set_power(True))
    for _ in range(3):  # the command has the lock, and is dialling
        await asyncio.sleep(0)
    waiting = asyncio.create_task(link_of(coordinator).connect())
    for _ in range(3):  # the connect has looked, and waits for the lock
        await asyncio.sleep(0)
    found.set()  # the link the command made
    await command
    await waiting

    assert lamp.dials == 1  # the connect dialled nothing
    assert link_of(coordinator).client is lamp.links[0]


def _info_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return what the coordinator logged at INFO, the level a user reads."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and record.name == coordinator_module.__name__
    ]


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        pytest.param("Glowrium-G7", "Glowrium-G7 (AA:BB:CC:DD:EE:FF)", id="discovered"),
        pytest.param(
            "Glowrium-G7_DDEEFF (AA:BB:CC:DD:EE:FF)",
            "Glowrium-G7_DDEEFF (AA:BB:CC:DD:EE:FF)",
            id="picked from the list",
        ),
    ],
)
async def test_going_out_of_reach_and_coming_back_are_each_said_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, name: str, shown: str
) -> None:
    """The log says when the lamp became unreachable, and when it returned.

    Its entities go unavailable and nothing said why or since when. Once
    each way, however many times the same thing is observed in between - by
    the lamp's name, and with its address once: a lamp picked from the list
    has its address in its title already.
    """
    coordinator, lamp = _at_a_lamp(hass, name)
    lamp.out_of_range()  # this is about the log, not about dialling
    link_of(coordinator).begin(present=True)

    with caplog.at_level(logging.INFO, logger=coordinator_module.__name__):
        link_of(coordinator).advertising(False)
        link_of(coordinator).advertising(False)
        coordinator._async_notify_listeners()
        (gone,) = _info_lines(caplog)
        assert gone.startswith(f"{shown} is out of reach")

        caplog.clear()
        link_of(coordinator).advertising(True)
        link_of(coordinator).advertising(True)
        coordinator._async_notify_listeners()
        (back,) = _info_lines(caplog)
        assert back == f"{shown} is back in reach"
    await hass.async_block_till_done()  # the dials the advertisements set off


async def test_a_lamp_with_a_link_is_not_out_of_reach_for_being_quiet(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Reach is the advertisement or the link, the same as for the entities.

    A lamp may stop advertising while it is connected. Its entities stay
    available then, and the log must not say otherwise - until the link goes
    as well, which is the moment it really is out of reach.
    """
    coordinator, _lamp, link = await _holding_a_link(hass)
    link_of(coordinator).begin(present=True)

    with caplog.at_level(logging.INFO, logger=coordinator_module.__name__):
        link_of(coordinator).advertising(False)
        assert coordinator.available
        assert _info_lines(caplog) == []

        link.lose()
        assert not coordinator.available
        assert len(_info_lines(caplog)) == 1
        assert "out of reach" in _info_lines(caplog)[0]
    await hass.async_block_till_done()  # the hang-up the disconnect started


async def test_a_command_that_fails_says_the_link_is_gone(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed command lets go of the link, and that may be all there was.

    The lamp was connected and no longer advertising. The command fails, the
    link is dropped, and nothing reaches the lamp any more - which the
    entities have to hear there and then, and the log with them. The error
    handed to the caller used to be all that was said: the entities stayed
    available until something else happened to tell them.
    """
    coordinator, lamp, _link = await _holding_a_link(hass)  # and not advertising
    lamp.fails_writes(BleakError("Not connected"))
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    told: list[bool] = []
    coordinator.async_add_listener(lambda: told.append(coordinator.available))
    assert coordinator.available

    with caplog.at_level(logging.INFO, logger=coordinator_module.__name__):
        with pytest.raises(HomeAssistantError):
            await coordinator.async_set_power(True)
        await hass.async_block_till_done()

    assert not coordinator.available
    assert told
    assert told[-1] is False
    assert len(_info_lines(caplog)) == 1
    assert "out of reach" in _info_lines(caplog)[0]


async def test_a_link_dropped_while_it_is_primed_is_said_to_be_gone(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The poll's priming can be what finds a link dead, and drops it."""
    coordinator, lamp, link = await _holding_a_link(hass)  # and not advertising
    lamp.fails_writes(BleakError("Not connected"))
    told: list[bool] = []
    coordinator.async_add_listener(lambda: told.append(coordinator.available))

    with caplog.at_level(logging.INFO, logger=coordinator_module.__name__):
        await link_of(coordinator).prime_held()
        await hass.async_block_till_done()

    assert link.hung_up
    assert told == [False]
    assert len(_info_lines(caplog)) == 1
    assert "out of reach" in _info_lines(caplog)[0]


@pytest.mark.parametrize("stopped_by", ["an unload", "Home Assistant stopping"])
async def test_a_stopped_coordinator_does_not_say_where_the_lamp_is(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stopped_by: str,
) -> None:
    """A coordinator that was told to stop is not the one to call the lamp gone.

    It let go of its link because it was stopped, not because the lamp went
    anywhere, and it no longer hears advertisements. A command that still
    arrives is refused, and tells the listeners as any command does - which
    must not turn into a line saying that the lamp is out of reach until it
    is heard again. Nobody is listening for it.
    """
    coordinator, _lamp, _link = await _holding_a_link(hass)  # held by its link alone
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)

    with caplog.at_level(logging.INFO, logger=coordinator_module.__name__):
        if stopped_by == "an unload":
            await coordinator.async_stop()
        else:
            coordinator.async_shutdown()
        with pytest.raises(HomeAssistantError):
            await coordinator.async_set_power(True)
        await hass.async_block_till_done()

    assert not coordinator.available
    assert _info_lines(caplog) == []


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
    coordinator, _lamp = _at_a_lamp(hass, "Glowrium-G7_DDEEFF (AA:BB:CC:DD:EE:FF)")
    fake = MagicMock()
    fake.async_register_callback.return_value = lambda: None
    fake.async_track_unavailable.return_value = lambda: None
    fake.async_address_present.return_value = present
    monkeypatch.setattr(coordinator_module, "bluetooth", fake)
    entry = MagicMock()
    entry.async_create_background_task = lambda _hass, coro, _name: coro.close()

    with caplog.at_level(logging.INFO, logger=coordinator_module.__name__):
        await coordinator.async_start(entry)
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


async def test_starting_watches_for_the_device(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start registers both bluetooth watchers and seeds presence.

    Without the advertisement callback the lamp is never noticed coming back;
    without the unavailable tracker its entities never go unavailable when it
    is unplugged.
    """
    coordinator, _lamp = _at_a_lamp(hass)  # this is about the watchers, not the link
    calls: list[str] = []
    fake = MagicMock()
    fake.async_register_callback.side_effect = lambda *a, **k: (
        calls.append("advertisement") or (lambda: None)
    )
    fake.async_track_unavailable.side_effect = lambda *a, **k: (
        calls.append("unavailable") or (lambda: None)
    )
    fake.async_address_present.side_effect = lambda *a, **k: calls.append("presence")
    fake.BluetoothCallbackMatcher = MagicMock()
    fake.BluetoothScanningMode = MagicMock()
    monkeypatch.setattr(coordinator_module, "bluetooth", fake)

    handed_over: list[str] = []

    def _background(_hass: object, coro: object, name: str) -> object:
        coro.close()  # the test does not run it, but must not leak it
        handed_over.append(name)
        return MagicMock()

    entry = MagicMock()
    entry.async_create_background_task = _background

    await coordinator.async_start(entry)
    try:
        assert sorted(calls) == ["advertisement", "presence", "unavailable"]
        # The first connect is handed to the entry, which is what cancels it
        # on unload rather than letting it outlive the coordinator.
        assert len(handed_over) == 1
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


async def test_a_command_that_never_reached_the_wire_fails_at_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lamp the scanner has lost fails the command at once, and as out of range.

    The dial says so without any I/O, so no byte ever left. Waiting the
    grace window for a notification that cannot arrive - there is no link -
    added two seconds to every command an automation sends to a lamp that is
    off or out of range. Of the reasons a command gets no link, this one is
    about range: the scanner no longer has the lamp, which is the radio's
    doing, so the message about range and a proxy is the right one for it,
    and the command goes round for its second attempt as it always did - the
    lamp may be heard again by then.
    """
    coordinator, lamp = _at_a_lamp(hass)
    lamp.out_of_range()  # nothing held, and the lamp is not in the list
    coordinator._mirror.echo({KEY_POWER: True})  # and the mirror happens to agree
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 30.0)

    async with asyncio.timeout(1):  # nowhere near the grace window
        with pytest.raises(HomeAssistantError) as err:
            await coordinator.async_set_power(True)
    await hass.async_block_till_done()

    assert err.value.translation_key == "cannot_connect"
    assert err.value.translation_placeholders == {"name": "Glowrium-G7"}
    assert lamp.dials == 2


@pytest.mark.parametrize(
    "stranger", ["one an earlier attempt gave up on", "one still being connected"]
)
async def test_an_old_client_disconnecting_does_not_drop_the_live_one(
    hass: HomeAssistant, stranger: str
) -> None:
    """The callback for a lost link checks WHICH client it is being told about.

    A failed write drops its client and the retry establishes another. When
    the OS later notices the first one is gone, bleak fires that client's
    callback - and letting go of the held client unconditionally there
    discarded the live connection instead. The link then read as not
    connected, so the poll opened yet another link to a lamp with a single
    slot, and every attempt failed with "out of connection slots" while the
    working connection sat there unreferenced until the lamp's own churn
    dropped it.

    And only the client the link holds is hung up from the callback. The
    first build of this fix hung up whichever client the callback named, as
    a second chance for a hang-up cut short by its ceiling. On the real lamp
    it lasted minutes: bleak reports a link lost in the middle of a connect
    to the same callback, while establish_connection is still working on
    that client. Disconnecting it there closed the bus underneath bleak's
    own clean-up - "Failed to cancel connection ... Bad file descriptor" on
    every such drop, and a retry that died on a bus that was no longer
    there.
    """
    coordinator, lamp, held = await _holding_a_link(hass)
    if stranger == "one an earlier attempt gave up on":
        lamp.fails_writes(BleakError("dropped"), times=1)
        await coordinator.async_set_power(True)  # retried, on a link of its own
        other, live = lamp.links  # the client an earlier attempt gave up on
        assert other.hung_up == 1
    else:
        live = held
        other = await lamp.dial(MagicMock())  # still inside establish_connection
    hang_ups = other.hang_ups

    link_of(coordinator).on_lost(other)
    await hass.async_block_till_done()

    assert link_of(coordinator).client is live
    assert link_of(coordinator).diagnostics()["connected"] is True
    assert live.hang_ups == 0
    assert other.hang_ups == hang_ups  # not hung up from the callback, nor again

    # The live one going down is still heard.
    live.lose()
    assert link_of(coordinator).diagnostics()["connected"] is False


@pytest.mark.parametrize("hang_up", ["at once", "slowly, past the deadline"])
async def test_a_connect_that_fails_half_way_leaves_no_link_behind(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hang_up: str
) -> None:
    """A connect that cannot finish must hang up, not abandon the link.

    The client is established before notifications are subscribed. If that
    subscription fails, walking away leaves a connected client holding the
    lamp's single slot with nothing referencing it: every later attempt then
    fails for want of a slot until the lamp's own churn drops it. And the
    client of a failed subscription is not disconnected on borrowed time: it
    was disconnected inline, inside the deadline of whatever was connecting,
    and a deadline that ran out during that disconnect cancelled it part-way
    - the bus left open, by the route the hang-up was written to close. The
    connect still waits for the hang-up, so that a retry does not dial over
    it; what the deadline ends now is that wait.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONNECT_TIMEOUT", 0.05)
    lamp.subscription_fails(BleakError("subscribe failed"))
    released = asyncio.Event()
    if hang_up != "at once":
        lamp.hangs_up_when(released)

    with pytest.raises(BleakError if hang_up == "at once" else TimeoutError):
        await link_of(coordinator).connect()  # the deadline, while it waits

    if hang_up != "at once":
        assert not lamp.links[0].hung_up
        released.set()
        await hass.async_block_till_done()
    assert lamp.links[0].hung_up == 1
    # And nothing is left claiming to be live.
    assert link_of(coordinator).diagnostics()["connected"] is False


async def test_stopping_hangs_up_even_when_the_lock_is_busy(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopping neither waits out the lock a slow exchange holds nor abandons the link.

    Unload waiting behind a connect is what made reloading the integration
    take the best part of ten seconds. And a busy lock must not mean the
    connection is simply abandoned: dropping the reference does not close a
    BLE link - bleak has no disconnect on garbage collection - so the lamp's
    one slot stays taken and the next coordinator cannot have it.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    monkeypatch.setattr(link_module, "_STOP_TIMEOUT", 0.05)
    lamp.never_acknowledges_a_write()  # the exchange on the held link hangs...
    priming = asyncio.create_task(link_of(coordinator).prime_held())
    await asyncio.sleep(0)  # ...holding the lock

    try:
        async with asyncio.timeout(0.4):  # must return, not hang
            await coordinator.async_stop()
    finally:
        priming.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await priming

    assert link.hung_up == 1  # hung up anyway
    assert link_of(coordinator).diagnostics()["connected"] is False


async def test_background_work_is_tied_to_the_entry(hass: HomeAssistant) -> None:
    """Reconnects and priming must die with the entry, like the first connect.

    Tasks created on hass are only awaited at shutdown, so on a reload one
    outlives its coordinator, finishes connecting, and claims the lamp's only
    slot for a coordinator nobody owns - while the replacement cannot connect.
    """
    coordinator, _lamp = _at_a_lamp(hass)
    spawned: list[str] = []

    def _background(_hass: object, coro: object, name: str) -> object:
        coro.close()
        spawned.append(name)
        return MagicMock()

    entry = MagicMock()
    entry.async_create_background_task = _background
    coordinator._entry = entry

    link_of(coordinator).tick()
    assert len(spawned) == 1  # the reconnect went to the entry, not to hass

    await coordinator.async_set_power(True)  # a link a command made, not primed
    link_of(coordinator).tick()
    assert len(spawned) == 2  # and so does the priming


async def test_one_background_connect_at_a_time(hass: HomeAssistant) -> None:
    """A connect still on its way is not joined by another.

    A dial to a lamp on a weak signal outlasts the poll's interval whenever
    it is given the chance, and the lamp advertises about once a second.
    Each of those starting a connect of its own would queue them up behind
    the lock, every one with its own deadline and its own line in the log.
    """
    coordinator, _lamp = _at_a_lamp(hass)
    spawned: list[str] = []

    def _background(_hass: object, coro: object, name: str) -> object:
        coro.close()  # started, and never done
        spawned.append(name)
        return MagicMock()

    entry = MagicMock()
    entry.async_create_background_task = _background
    coordinator._entry = entry

    link_of(coordinator).tick()
    link_of(coordinator).tick()
    link_of(coordinator).advertising(True)

    assert len(spawned) == 1


def test_no_path_holds_the_lock_longer_than_a_command_will_wait() -> None:
    """The timing constants have to make sense relative to each other.

    Every test that exercises a timeout patches it, so the shipped numbers are
    otherwise unconstrained - and they interact. A background connect holds the
    connection lock; a command waits for that same lock inside its own budget.
    If the holder is allowed longer than the waiter, pressing a switch during a
    background connect reports failure on a perfectly reachable lamp, having
    attempted nothing at all.
    """
    connect = link_module._CONNECT_TIMEOUT
    ask = link_module._ASK_TIMEOUT
    command = link_module._COMMAND_TIMEOUT
    hang_up = link_module._HANG_UP_TIMEOUT
    poll = coordinator_module._RECONNECT_INTERVAL.total_seconds()

    assert connect < command, "a lock holder outlasting the waiter is an inversion"
    # A dial waits for a hang-up still under way, inside the connect's own
    # time. A hang-up allowed as long as the connect would leave no time to
    # dial once it had ended.
    assert hang_up < connect
    assert ask < command
    assert connect >= link_module._STOP_TIMEOUT
    # The library gives one try BLEAK_TIMEOUT before it gives up and tidies up
    # after itself. A ceiling below that cuts even the first try from outside,
    # in the middle of a connect - where, measured on a G7 whose connects take
    # five to ten seconds, they were about to finish. It still covers the wait
    # for the lock and the later tries, so this is a floor and not a promise.
    assert connect >= BLEAK_TIMEOUT
    # Three tries inside one dial: on a weak link a connection is often made
    # and lost within a second or two, and the next try is what gets through.
    # Agreed against that measurement; another number wants another one.
    assert link_module._CONNECT_ATTEMPTS == 3
    # Priming is spawned from the poll and takes the same lock, so it must be
    # finished before the next tick or the ticks pile up on top of each other.
    assert connect < poll
    assert ask < poll
    # A link that died without the stack noticing is found only by asking it,
    # so this is how long the entities can go on showing a lamp that is not
    # there. Five minutes was agreed; another number wants a reason of its own.
    assert link_module._PROBE_INTERVAL == 300
    # A write retry waits for the hang-up of the client it gave up on, inside
    # the command's budget and before it dials. A hang-up allowed as long as
    # the command leaves the retry no time to happen in exactly the case it is
    # for: a link that will not confirm it has closed.
    assert hang_up < command


@pytest.mark.parametrize("path", ["a background connect", "the poll"])
@pytest.mark.parametrize(
    "failure",
    [BleakError("Not connected"), *_BUS_CLOSED],
    ids=["not connected", "eof", "bad-fd"],
)
async def test_a_link_that_answers_nothing_is_dropped(
    hass: HomeAssistant, path: str, failure: Exception
) -> None:
    """A link that cannot even be read is not a working link, and is hung up.

    bleak can report a client as connected while BlueZ answers "Not
    connected" to everything - seen on real hardware: establish_connection
    returned a client whose every operation answered so while still
    reporting itself connected. Keeping it means the link reads as
    connected, so the poll never reconnects and the coordinator is wedged
    until the device's own churn; marking it primed regardless meant one
    failed attempt stopped the poll ever trying again, and the entities sat
    at one of fourteen indefinitely. The link can go under the state request
    or under the read that follows it, and the bus closing under either is
    the same dead link, not an exception with a traceback.

    So it is dropped where it is detected - by the poll's priming of a link
    a command took, or by the connect itself, not a poll tick later:
    observed live, a connect that kept such a client left a command in the
    next thirty seconds writing into it before failing and reconnecting.
    And forgetting a client is not disconnecting it: about two and a half
    hours after each start the system bus refused every new connection from
    Home Assistant's user, Bluetooth included, because bleak opens a D-Bus
    connection per client and closes it only in ``disconnect()``, and every
    client let go of by clearing the reference kept its connection until
    the bus's limit of 256 per user was reached. The hang-up is not waited
    for: both paths run under a deadline and hold the lock, and waiting
    there would keep the lock for as long as BlueZ takes to confirm and let
    the deadline cancel the disconnect part-way - the leak again, by the
    route the fix closes for commands.
    """
    if path == "the poll":
        coordinator, lamp, _link = await _holding_a_link(hass)
    else:
        coordinator, lamp = _at_a_lamp(hass)
    lamp.fails_writes(failure)
    lamp.fails_reads(failure)  # and there is nothing to read
    released = asyncio.Event()
    lamp.hangs_up_when(released)  # a real disconnect is not instant either

    async with asyncio.timeout(1):  # returns with the hang-up pending
        if path == "the poll":
            await link_of(coordinator).prime_held()
        else:
            await link_of(coordinator).connect()

    link = lamp.links[0]
    assert link_of(coordinator).diagnostics()["primed"] is False
    assert link_of(coordinator).diagnostics()["connected"] is False  # let go at once
    assert link.hang_ups == 1
    assert not link_of(coordinator).lock.locked()
    assert not link.hung_up
    released.set()
    await hass.async_block_till_done()
    assert link.hung_up == 1


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
async def test_only_an_application_level_refusal_silences_the_request(
    hass: HomeAssistant, error: BleakError, readable: bool, is_a_refusal: bool
) -> None:
    """Silence the request on a refusal, never on anything merely unrecognised.

    Two attempts to tell the cases apart failed on real hardware. A
    successful read does not prove the device is there: seen on a real G7 in
    0.2.0, the read answered - the low property block arrived and six
    entities came alive - and the request then failed with "Not connected"
    three times running, because the link dropped in between (because of
    the read, as it turned out: see _request_state). Nor does
    ``is_connected``: measured on a G7, it still reported True at the moment
    the write failed with "not connected", and the disconnect callback
    arrived two seconds later. A weak link fails the read and the request
    alike, and counting that muted the request on a perfectly good lamp that
    merely sat far from the adapter - seen forty seconds after start-up.

    So the test is inverted. Only an error that positively looks like the
    device answering "no" - an authorization or ATT protocol error - counts,
    and which ATT error it was is in the code bleak gives, not in the words
    that render it: told by the text, a refusal lasts as long as the wording
    does, and an error that only mentions authorization in passing is taken
    for one. The text is still what there is to go by where the error is
    not bleak's own - a Bluetooth proxy's, say. Everything else is treated
    as the link, and is not even counted, however often it happens: muting a
    working lamp costs it four properties silently, while asking an exotic
    device once too often costs a reconnect.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    if readable:
        lamp.readable(NOTIFY_UUID, cbor.encode({KEY_POWER: True}))
    lamp.fails_writes(error)

    for _ in range(coordinator_module._STATE_REQUEST_ATTEMPTS * 3):
        await coordinator._request_state(turn_over(coordinator, link))

    assert coordinator._state_request_muted is is_a_refusal
    if not is_a_refusal:
        assert coordinator._state_request_failures == 0


async def _the_warnings_that_name_the_lamp(
    coordinator: GlowriumCoordinator, caplog: pytest.LogCaptureFixture
) -> list[str]:
    """Draw each warning that names the lamp's model, and return what was said.

    Four of them: a frame with trailing bytes, one with an item that cannot be
    read, more properties than are kept, and a refused state request.
    """
    lamp = lamp_of(coordinator)
    await coordinator.async_set_indicator(True)  # a link to be asked on
    link = lamp.links[-1]
    _refusing(lamp)
    with caplog.at_level(logging.WARNING, logger=coordinator_module.__name__):
        lamp.say(bytes.fromhex("a106f5deadbeef"))  # trailing bytes
        lamp.say(_PARTLY_READABLE)
        flood(lamp.say, ids=65)  # one more than there is room for
        for _ in range(coordinator_module._STATE_REQUEST_ATTEMPTS):
            await coordinator._request_state(turn_over(coordinator, link))
    said = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(said) == 4
    assert "trailing bytes" in said[0]
    assert "cannot read" in said[1]
    assert "reports more properties than" in said[2]
    assert "refused the batched state request" in said[3]
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
    hands over one long field with the others inside it. Four warnings name
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


def _counting_connects(coordinator: GlowriumCoordinator) -> list[int]:
    """Note each time the coordinator sets about getting a link.

    Counted where a link is asked for and not at the dial: a link that is
    refused - the coordinator has stopped, a client would not close - is
    refused before anything is dialled, and how often it was asked for is
    what these tests are about.
    """
    asked: list[int] = []
    link = link_of(coordinator)
    ask = link.open

    async def _counted() -> Any:
        asked.append(1)
        return await ask()

    link.open = _counted
    return asked


@pytest.mark.parametrize("failed", ["the write", "the subscription"])
async def test_a_retry_dials_only_after_the_client_it_gave_up_on_is_hung_up(
    hass: HomeAssistant, failed: str
) -> None:
    """The client a command failed on is closed, and closed before the retry dials.

    Order matters as much as the hang-up. The lamp has one slot: while BlueZ
    still shows the old link as up, a connect is handed that very link - the
    one being closed - and the hang-up then closes it underneath the retry.
    Hanging the client of a failed subscription up in the background lost
    that order: the retry dialled first, which on a link that reports itself
    connected while answering nothing means a second attempt on the link the
    first one had just failed on.
    """
    if failed == "the write":
        coordinator, lamp, _first = await _holding_a_link(hass)
        lamp.fails_writes(BleakError("dropped"), times=1)
    else:
        coordinator, lamp = _at_a_lamp(hass)
        lamp.subscription_fails(BleakError("Not connected"))
    released = asyncio.Event()
    lamp.hangs_up_when(released)  # a real disconnect is not instant either

    command = asyncio.create_task(coordinator.async_set_power(True))
    await asyncio.sleep(0.01)
    first = lamp.links[0]
    assert first.hang_ups == 1  # being hung up...
    assert lamp.dials == 1  # ...and not dialled again before that is through
    lamp.subscription_fails(None)  # the next link subscribes
    released.set()
    await command

    assert first.hung_up == 1
    assert lamp.dials == 2
    second = lamp.links[1]
    assert link_of(coordinator).client is second
    assert second.hang_ups == 0  # the link that worked is kept


@pytest.mark.parametrize("report", ["inside the failing write", "a moment after it"])
async def test_a_failed_command_hangs_up_only_after_the_device_could_confirm(
    hass: HomeAssistant, report: str
) -> None:
    """The last client is closed too - but not before confirmation has listened.

    A client whose write failed is still the channel the confirming report
    arrives on (see test_confirmation_waits_for_a_report_that_arrives_late).
    Hanging it up the moment the write fails would close the leak and quietly
    take that away: every command whose acknowledgement was lost would be
    reported as failed again.

    So the lamp here does what that window exists for. It acts on the write,
    the acknowledgement is lost, and its report arrives - with the failure,
    or a moment after it - on the link the write failed on, and only if that
    link is still up: a client that has been hung up delivers nothing. And
    being told the command worked is no reason to keep the leak: both
    clients are hung up once it has.
    """
    coordinator, lamp, first = await _holding_a_link(hass)
    still_up_when_reporting: list[bool] = []

    def _report_if_still_connected() -> None:
        second = lamp.links[-1]
        still_up_when_reporting.append(second.hang_ups == 0)
        lamp.say(cbor.encode({KEY_POWER: True}))  # nothing over a hung-up link

    if report == "inside the failing write":
        lamp.fails_writes(
            BleakError("GATT Protocol Error: Unlikely Error"),
            saying=lambda _frame: cbor.encode({KEY_POWER: True}),
        )
    else:
        lamp.fails_writes(BleakError("GATT Protocol Error: Unlikely Error"))
        # Acted on, and not acknowledged: the lamp's report comes a moment after.
        hass.loop.call_later(0.05, _report_if_still_connected)

    await coordinator.async_set_power(True)  # confirmed by the report
    await hass.async_block_till_done()

    if report == "a moment after it":
        assert still_up_when_reporting == [True]  # up while the device could answer
    assert coordinator.state[KEY_POWER] is True
    assert first.hung_up == 1
    assert lamp.links[1].hung_up == 1  # ...and hung up once it had
    assert link_of(coordinator).diagnostics()["connected"] is False


@pytest.mark.parametrize("behind_it", ["Home Assistant", "nothing: the bench"])
async def test_a_link_the_lamp_dropped_is_hung_up_as_well(
    hass: HomeAssistant, behind_it: str
) -> None:
    """The link going down by itself lets go of the client, and closes it.

    The callback for a lost link lets go of the client there and then:
    everything downstream goes by the link's ``connected`` (read here
    through ``link_of(coordinator).diagnostics()``), and a stale client left
    in place would read as connected, so the poll would never reconnect and
    every command would write into a dead handle. And bleak leaves the
    client's D-Bus connection open after the device disconnects; only
    ``disconnect()`` releases it. This was where the quota actually went: on
    the lamp it was found on, at the edge of range, the link came up on
    every poll tick and the lamp dropped it two to ten seconds later - 678
    times in one night - and each time the callback only cleared the
    reference.

    With no Home Assistant behind the coordinator too. tools/bench.py builds
    the real one with ``hass=None`` and takes the paths the integration
    takes; with the hang-up scheduled on hass, every path that lets go of a
    client - a link the lamp drops, a connect that answers nothing, a write
    that needs its retry - ended in "'NoneType' object has no attribute
    'async_create_task'", with the client still connected. Found by walking
    those three paths on a coordinator built the way the bench builds it;
    0.2.1 took all three.
    """
    with_hass = behind_it == "Home Assistant"
    coordinator, _lamp, link = await _holding_a_link(
        hass if with_hass else None, "Glowrium-G7" if with_hass else "bench"
    )
    assert link_of(coordinator).diagnostics()["connected"] is True

    link.lose()
    assert link_of(coordinator).diagnostics()["connected"] is False  # at once
    if with_hass:
        await hass.async_block_till_done()
    else:
        await asyncio.sleep(0)  # nothing to block on without hass; one turn does it

    assert link_of(coordinator).diagnostics()["connected"] is False
    assert link.hung_up == 1


async def test_a_hang_up_outlives_the_deadline_of_whoever_asked_for_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command running out of time must not take the hang-up with it.

    A disconnect that is cancelled part-way has asked BlueZ to drop the link
    and then walked away before closing its own D-Bus connection - the leak
    again, by another route.
    """
    coordinator, lamp, first = await _holding_a_link(hass)
    monkeypatch.setattr(link_module, "_COMMAND_TIMEOUT", 0.05)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    lamp.fails_writes(BleakError("dropped"))
    released = asyncio.Event()
    lamp.hangs_up_when(released)

    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_power(True)  # the deadline ends the wait

    assert not first.hung_up
    assert lamp.dials == 1  # and it never dialled over the old link
    released.set()
    await hass.async_block_till_done()
    assert first.hung_up == 1  # the hang-up itself ran to the end


async def test_a_hang_up_that_fails_or_hangs_troubles_nobody(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hanging up is cleanup: it is bounded, and its failures stay in the log.

    bleak's disconnect passes on whatever the bus raised and ends on an
    assertion, and against a wedged BlueZ it can wait indefinitely. None of
    that may reach the path that gave the link up, or outlive the test - and
    none of it may vanish either: the log line says why a bus had to be closed
    by hand (tests/test_bus_lifetime.py is about the closing itself).
    """
    monkeypatch.setattr(link_module, "_HANG_UP_TIMEOUT", 0.05)
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    for failure in (BleakError("gone"), OSError(9, "Bad file descriptor"), EOFError()):
        coordinator, lamp, link = await _holding_a_link(hass)
        lamp.fails_hang_ups(failure)
        caplog.clear()

        await link_of(coordinator).hang_up(link)  # the failure does not come out

        assert link.hang_ups == 1
        assert link_of(coordinator).diagnostics()["connected"] is False
        assert f"failed: {failure!r}" in caplog.text

    coordinator, lamp, link = await _holding_a_link(hass)
    lamp.hangs_up_when(asyncio.Event())  # never
    caplog.clear()
    async with asyncio.timeout(1):
        await link_of(coordinator).hang_up(link)  # ends at the ceiling, not never
    assert link_of(coordinator).diagnostics()["connected"] is False
    assert "failed: TimeoutError()" in caplog.text


async def test_a_hang_up_is_not_tied_to_the_entry(hass: HomeAssistant) -> None:
    """The one background task that must survive the entry being unloaded: hass's.

    Everything else is created on the entry so that it dies with it (see
    test_background_work_is_tied_to_the_entry): a connect that outlives its
    coordinator claims the lamp's slot for nobody. A hang-up is the opposite
    - cancelled by an unload, it leaves the slot taken and the D-Bus
    connection open. With Home Assistant behind the coordinator it is
    hass's task to see through; the standalone path keeps its own task, and
    this is the other side of that choice. A task hass does not track is
    one async_block_till_done walks straight past, and that call is how Home
    Assistant - and every test here - lets pending work settle before it
    looks at the result.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    entry = MagicMock()
    coordinator._entry = entry
    released = asyncio.Event()
    lamp.hangs_up_when(released)
    hass.loop.call_later(0.05, released.set)  # it takes a moment

    link_of(coordinator).hang_up(link)
    await hass.async_block_till_done()

    entry.async_create_background_task.assert_not_called()
    assert link.hung_up == 1


async def test_reading_the_device_info_does_not_need_home_assistant() -> None:
    """What the lamp says about itself is kept even with nowhere to publish it.

    Home Assistant gets it in its device registry and its config entry; the
    bench has neither, and reads the string all the same.
    """
    coordinator, lamp, link = await _holding_a_link(None, "bench")
    lamp.readable(INFO_UUID, b"pkey:Glowrium-C051;version:4;;")

    await coordinator._async_read_device_info(turn_over(coordinator, link))

    assert coordinator.model_id == "Glowrium-C051"
    assert coordinator.sw_version == "4"


async def test_background_work_does_not_need_home_assistant() -> None:
    """What the poll spawns - a priming, a probe - runs with no hass to run it on.

    With neither an entry nor Home Assistant the task was handed to
    ``hass.async_create_task``, on None. The coordinator keeps such a task
    itself, as it keeps a hang-up: the loop holds a task only weakly, and one
    nobody else refers to can be collected half-way through.
    """
    coordinator = GlowriumCoordinator(None, "AA:BB:CC:DD:EE:FF", "bench")
    started = asyncio.Event()
    release = asyncio.Event()
    finished: list[int] = []

    async def _work() -> None:
        started.set()
        await release.wait()
        finished.append(1)

    coordinator._spawn(_work(), "probe")
    await asyncio.wait_for(started.wait(), 1)
    assert len(coordinator._kept_tasks) == 1  # held for as long as it runs

    release.set()
    for _ in range(3):  # the task ends, then its done-callback runs
        await asyncio.sleep(0)

    assert finished == [1]
    assert not coordinator._kept_tasks  # and let go of once it is done


async def test_watching_the_lamp_needs_home_assistant() -> None:
    """Starting registers with Home Assistant's Bluetooth; without one it says so.

    It failed a few lines in, inside Home Assistant's own code and in its
    words, with the entry already taken. It is refused at the door instead,
    with nothing put on and nothing registered.
    """
    coordinator = GlowriumCoordinator(None, "AA:BB:CC:DD:EE:FF", "bench")

    with pytest.raises(RuntimeError, match="needs Home Assistant"):
        await coordinator.async_start(MagicMock())

    assert coordinator._entry is None


async def test_stopping_does_not_cut_the_hang_up_short(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disconnect that hangs costs the stop one ceiling, and is not cut short by it.

    Measured on the real integration: reloading took 6.1 s, all of it in
    unload, because a first bounded disconnect timed out and a fallback then
    timed out again. The lock and the hang-up are separate problems and need
    separate deadlines - the lock is best-effort, the disconnect is tried
    once. And unload stops waiting at its ceiling; the disconnect itself
    carries on. Stopping disconnected under its own ceiling of three seconds,
    and on the real integration that ceiling has fired. Run against bleak
    3.0.2 with a bus that never confirms: a disconnect cancelled at that
    ceiling has asked BlueZ to drop the link and returns with the client's
    D-Bus connection still open. That is the leak this fix is about, taken
    on every reload that meets a slow link - and a reload is what one
    reaches for when Bluetooth misbehaves.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    monkeypatch.setattr(link_module, "_STOP_TIMEOUT", 0.05)
    released = asyncio.Event()
    lamp.hangs_up_when(released)

    async with asyncio.timeout(0.4):  # comfortably under two ceilings plus slack
        await coordinator.async_stop()

    assert link.hang_ups == 1
    assert not link.hung_up
    assert link_of(coordinator).diagnostics()["connected"] is False
    released.set()
    await hass.async_block_till_done()
    assert link.hung_up == 1  # the disconnect ran to the end, after the unload


async def test_stopping_is_not_broken_by_what_the_bus_raises(
    hass: HomeAssistant,
) -> None:
    """A dead bus must not make stopping raise.

    With the bus's quota spent, bleak's calls end in EOFError and "Bad file
    descriptor" - neither a BleakError. Stopping caught only BleakError and
    TimeoutError and let these through. Home Assistant runs the unload callback
    as a task and does not pass its exception on, so the reload still went
    through - with an unretrieved exception left in the log, in exactly the
    state where reloading is the remedy and the log is what gets read.
    """
    for failure in (OSError(9, "Bad file descriptor"), EOFError()):
        coordinator, lamp, link = await _holding_a_link(hass)
        lamp.fails_hang_ups(failure)

        await coordinator.async_stop()

        assert link.hang_ups == 1
        assert link_of(coordinator).diagnostics()["connected"] is False


# The two ways a connect is started in the background, and what each is called
# in the log.
_BACKGROUND_CONNECTS = (
    ("reconnect", "Reconnect to"),
    ("initial_connect", "Initial connect to"),
)


async def _never_returns(*_args: object, **_kwargs: object) -> None:
    """Stand in for a call the deadline finds still waiting."""
    await asyncio.Event().wait()


@pytest.mark.parametrize(("connect", "named"), _BACKGROUND_CONNECTS)
async def test_a_deadline_that_falls_on_a_held_link_leaves_it_and_calls_it_held(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    connect: str,
    named: str,
) -> None:
    """A connect that got its link and ran out of time after it has not failed.

    Seen on the G7's host under load (2026-10-06): the connect went through
    late, the ceiling ran out during the first exchange, and the log said
    "Reconnect to ... failed" of a link the coordinator went on holding. The
    link is not let go of - the poll primes it on its next tick, without
    another dial - and the log says that it is held, so whoever reads it is
    not sent looking for a lamp out of range.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONNECT_TIMEOUT", 0.05)
    lamp.never_acknowledges_a_write()
    caplog.set_level(logging.DEBUG)

    await getattr(link_of(coordinator), connect)()

    client = lamp.links[0]
    assert link_of(coordinator).client is client  # taken, and not let go of
    assert client.hang_ups == 0
    assert f"{named} AA:BB:CC:DD:EE:FF ran out of time" in caplog.text
    assert "the link is held (not primed yet)" in caplog.text
    assert "failed" not in caplog.text

    lamp.answers({KEY_POWER: True, KEY_ACTIVATED: True})
    link_of(coordinator).tick()
    await hass.async_block_till_done()

    assert link_of(coordinator).diagnostics()["primed"] is True
    assert lamp.dials == 1  # the same link, primed; nothing was redialled


@pytest.mark.parametrize(("connect", "named"), _BACKGROUND_CONNECTS)
@pytest.mark.parametrize(
    "ended_by", ["the link's own no", "a write that lost its link", "a refusal"]
)
async def test_a_background_connect_ends_in_the_log_whatever_ended_it(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    connect: str,
    named: str,
    ended_by: str,
) -> None:
    """Nothing the link itself raises gets out of a connect made in the background.

    Its errors are its own since the split (#21), no longer the Bluetooth
    library's, and a background connect is a task nobody waits for: an error
    that got out of it would be an ERROR in the log - "Task exception was
    never retrieved" - for a lamp that had only gone out of reach. It ends
    as a line of its own whichever of them ended it: the link's "no" to a
    new client, a write of the first exchange that lost its link, or one the
    lamp refused.
    """
    kept = link_module.Unclosed()
    lamp = ScriptedLamp()
    coordinator = lamp.coordinator(hass, unclosed=kept)
    if ended_by == "the link's own no":
        kept.keep(object(), None)  # a client that would not close
    else:
        failure = BleakError(
            "Not connected"
            if ended_by == "a write that lost its link"
            else "Insufficient authorization (8)"
        )
        # A lamp that reports itself not activated: the exchange has to write.
        lamp.answers({KEY_ACTIVATED: False})
        lamp.fails_writes(failure, of=WRITE_UUID)
    caplog.set_level(logging.DEBUG)

    await getattr(link_of(coordinator), connect)()  # and nothing is raised

    assert f"{named} AA:BB:CC:DD:EE:FF failed: " in caplog.text


async def test_a_deadline_that_falls_after_priming_says_the_link_is_primed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The device-info read comes last, and can be what the deadline cuts.

    The state has arrived by then and the link is primed; the poll has nothing
    to add to it, so the line must not promise that it will.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONNECT_TIMEOUT", 0.05)
    lamp.answers({KEY_POWER: True, KEY_ACTIVATED: True})
    lamp.never_answers_a_read()
    caplog.set_level(logging.DEBUG)

    await link_of(coordinator).reconnect()

    assert link_of(coordinator).diagnostics()["primed"] is True
    assert "the link is held (primed)" in caplog.text
    assert "failed" not in caplog.text


async def test_a_deadline_spent_waiting_behind_a_command_that_connected_is_no_failure(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A command can take the link while the background connect waits its turn.

    The connect then runs out of time without having dialled at all, and the
    lamp is connected all the same - by the command, which does not prime.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONNECT_TIMEOUT", 0.05)
    found = asyncio.Event()
    lamp.dials_when(found)
    lamp.never_acknowledges_a_write()
    caplog.set_level(logging.DEBUG)

    command = asyncio.create_task(coordinator.async_set_power(True))
    await asyncio.sleep(0)  # the command's turn: it has the lock, and dials
    waiting = asyncio.create_task(link_of(coordinator).reconnect())
    await asyncio.sleep(0)  # ...and the connect waits behind it
    found.set()  # the link the command made, unprimed; its write never returns
    await waiting
    command.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await command

    assert lamp.dials == 1
    assert "the link is held (not primed yet)" in caplog.text
    assert "failed" not in caplog.text


@pytest.mark.parametrize(("connect", "named"), _BACKGROUND_CONNECTS)
async def test_a_connect_that_gets_no_link_is_still_called_a_failed_connect(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    connect: str,
    named: str,
) -> None:
    """The deadline running out with nothing held is the failure it always was."""
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONNECT_TIMEOUT", 0.05)
    lamp.dials_through(_never_returns)
    caplog.set_level(logging.DEBUG)

    await getattr(link_of(coordinator), connect)()

    assert link_of(coordinator).diagnostics()["connected"] is False
    assert f"{named} AA:BB:CC:DD:EE:FF failed" in caplog.text
    assert "the link is held" not in caplog.text


async def test_a_connect_that_fails_on_a_held_link_keeps_the_error_it_failed_with(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only running out of time is reworded: an error says what it was."""
    coordinator, lamp = _at_a_lamp(hass)
    lamp.answers({KEY_POWER: True, KEY_ACTIVATED: True})
    coordinator._async_sync_clock_if_needed = AsyncMock(
        side_effect=BleakError("the clock would not be set")
    )
    caplog.set_level(logging.DEBUG)

    await link_of(coordinator).reconnect()

    assert link_of(coordinator).client is lamp.links[0]  # held all the same
    assert "Reconnect to AA:BB:CC:DD:EE:FF failed: the clock would not" in caplog.text
    assert "the link is held" not in caplog.text


@pytest.mark.parametrize(
    ("error", "said"),
    [
        pytest.param(TimeoutError(), "TimeoutError()", id="a deadline"),
        pytest.param(EOFError(), "EOFError()", id="a bus closed under a call"),
        pytest.param(BleakError("Not connected"), "Not connected", id="a lost link"),
        pytest.param(
            OSError(9, "Bad file descriptor"),
            "[Errno 9] Bad file descriptor",
            id="bad-fd",
        ),
    ],
)
@pytest.mark.parametrize(
    "where", ["a reconnect", "an initial connect", "the state read", "the priming"]
)
async def test_an_error_goes_into_the_log_by_what_it_says_or_else_by_what_it_is(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    where: str,
    error: Exception,
    said: str,
) -> None:
    """Every line about a lost link names what lost it: its words, or else its name.

    The two errors with nothing to say are the two commonest on a weak link.
    Running out of time is the commonest way for a connect to fail, and
    ``TimeoutError`` carries no message: one night on the G7's host left 290
    lines that ended in "failed: " with nothing after it (2026-10-08). Not
    the connect alone: priming that ran out its deadline and a read whose
    bus was closed under it said "failed: " and stopped there too. And
    neither background connect lets the error out: they run as tasks nobody
    awaits, so what escapes one is not handled by anybody - it ends up in
    the log as an exception with a traceback, on every poll tick for as long
    as the bus stays the way it is.
    """
    assert link_module._reason(error) == said
    caplog.set_level(logging.DEBUG)
    if where == "the state read":
        coordinator, lamp, link = await _holding_a_link(hass)
        lamp.fails_reads(error)
        assert await coordinator._async_read_state(turn_over(coordinator, link)) == (
            False,
            frozenset(),
        )
        line = f"AA:BB:CC:DD:EE:FF state read failed: {said}"
    elif where == "the priming":
        coordinator, lamp, _link = await _holding_a_link(hass)
        coordinator._request_state = AsyncMock(side_effect=error)
        await link_of(coordinator).prime_held()
        line = f"Priming state of AA:BB:CC:DD:EE:FF failed: {said}"
    else:
        coordinator, lamp = _at_a_lamp(hass)
        lamp.dials_through(AsyncMock(side_effect=error))
        if where == "a reconnect":
            await link_of(coordinator).reconnect()  # and nothing is raised
            line = f"Reconnect to AA:BB:CC:DD:EE:FF failed: {said}"
        else:
            await link_of(coordinator).initial_connect()  # nor here
            line = f"Initial connect to AA:BB:CC:DD:EE:FF failed: {said}"
    assert line in caplog.text


async def test_hanging_up_a_client_we_gave_up_on_leaves_the_one_we_hold(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hang-up after the confirmation window must not drop a newer link.

    A command that failed twice lets go of its client at once but hangs it up
    only after the confirmation window, which is spent outside the lock. A
    second command queued behind it takes the lock in that window and
    establishes a link of its own. When the first command then hangs up the
    client it gave up on, the coordinator must still hold the second one:
    forgetting it would leak that client's D-Bus connection - the very thing
    the hang-up exists to prevent - and leave the lamp's single slot taken by
    nobody.
    """
    coordinator, lamp, _first = await _holding_a_link(hass)
    # Twice: on the link held, and on the one the retry makes. The command fails.
    lamp.fails_writes(BleakError("Unlikely Error"), times=2)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.2)

    failing = asyncio.create_task(coordinator.async_set_power(True))
    await asyncio.sleep(0)  # it has failed twice and now waits for a report
    second = lamp.links[1]
    # Let go of at once, not after the wait.
    assert link_of(coordinator).diagnostics()["connected"] is False
    assert second.hang_ups == 0

    await coordinator.async_set_brightness(40)  # queued behind it; connects
    third = lamp.links[2]  # the next command's link works
    assert link_of(coordinator).client is third

    with pytest.raises(HomeAssistantError):
        await failing
    await hass.async_block_till_done()

    assert second.hung_up == 1  # the abandoned client is closed
    assert link_of(coordinator).client is third  # ...and the live one is still ours
    assert third.hang_ups == 0


async def test_a_connect_cancelled_half_way_is_hung_up_but_not_waited_for(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadline that cancels the subscription still gets the client hung up.

    And is not kept waiting for it. The connect holds the lock, and whoever set
    the deadline has already stopped waiting: staying on here for as long as
    BlueZ takes to close the link would hold the lock past the deadline for
    nobody's benefit.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_CONNECT_TIMEOUT", 0.05)
    lamp.subscribes_when(asyncio.Event())  # never
    released = asyncio.Event()
    lamp.hangs_up_when(released)

    async with asyncio.timeout(0.5):
        with pytest.raises(TimeoutError):
            await link_of(coordinator).connect()

    link = lamp.links[0]
    assert link.hang_ups == 1
    assert not link_of(coordinator).lock.locked()
    assert not link.hung_up
    released.set()
    await hass.async_block_till_done()
    assert link.hung_up == 1


async def test_what_the_watchers_hear_reaches_the_link(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lamp heard, the lamp gone and the timer each reach the link.

    Home Assistant's watchers are the coordinator's, and each is one line
    that hands the link what it saw: an advertisement, the lamp no longer
    heard, the tick. What the link then does is tested on the link itself;
    this holds the three lines, which nothing else did once the tests had
    crossed the seam - three mutants of the stage 2 gate outlived stage 3.
    """
    coordinator, lamp = _at_a_lamp(hass)
    lamp.out_of_range()  # every dial fails at once, and is counted
    heard: dict[str, Callable[..., None]] = {}

    def _register(
        _hass: object, callback: Callable[..., None], *_a: object, **_k: object
    ) -> Callable[[], None]:
        heard["advertisement"] = callback
        return lambda: None

    def _track(
        _hass: object, callback: Callable[..., None], *_a: object, **_k: object
    ) -> Callable[[], None]:
        heard["unavailable"] = callback
        return lambda: None

    fake = MagicMock()
    fake.async_register_callback.side_effect = _register
    fake.async_track_unavailable.side_effect = _track
    fake.async_address_present.return_value = False
    monkeypatch.setattr(coordinator_module, "bluetooth", fake)
    entry = MagicMock()
    # Run what is handed over: a connect that fails has to end as one.
    entry.async_create_background_task = lambda _hass, coro, name: (
        hass.async_create_task(coro, name)
    )

    await coordinator.async_start(entry)
    try:
        await hass.async_block_till_done()  # the initial connect, failing
        assert not coordinator.available

        heard["advertisement"](MagicMock(), MagicMock())
        assert coordinator.available  # the link heard the lamp
        await hass.async_block_till_done()  # the reconnect it set off, failing

        heard["unavailable"](MagicMock())
        assert not coordinator.available  # and heard it go

        dials = lamp.dials
        later = dt_util.utcnow() + coordinator_module._RECONNECT_INTERVAL
        async_fire_time_changed(hass, later + timedelta(seconds=1))
        await hass.async_block_till_done()
        assert lamp.dials == dials + 1  # the tick dialled
    finally:
        await coordinator.async_stop()


async def test_a_reconnect_started_while_starting_belongs_to_the_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advertisement Home Assistant replays on registration is not an orphan.

    When it already knows the device, Home Assistant calls the advertisement
    callback at once, from inside async_register_callback - and a lamp that
    advertises continuously is always already known, so that is every reload.
    The coordinator took it for the lamp reappearing and started a reconnect
    before it had been handed its entry. With no entry to put it on, the task
    went to hass, where an unload does not reach it.

    Seen on the real integration after two reloads in a row: "Reconnect to ...
    failed" logged ten seconds after the coordinator that started it had been
    unloaded, while its successor's own connects were answered "In Progress".
    On a lamp whose link holds, a connect that outlives its coordinator keeps
    the single slot for nobody.
    """
    coordinator, lamp = _at_a_lamp(hass)
    lamp.out_of_range()

    def _register(_hass: object, callback: object, *_a: object) -> object:
        callback(MagicMock(), MagicMock())  # the replayed advertisement
        return lambda: None

    fake = MagicMock()
    fake.async_register_callback.side_effect = _register
    fake.async_track_unavailable.return_value = lambda: None
    fake.async_address_present.return_value = True
    monkeypatch.setattr(coordinator_module, "bluetooth", fake)

    handed_over: list[str] = []

    def _background(_hass: object, coro: object, name: str) -> object:
        coro.close()  # the test does not run it, but must not leak it
        handed_over.append(name)
        return MagicMock()

    entry = MagicMock()
    entry.async_create_background_task = _background

    await coordinator.async_start(entry)
    try:
        await hass.async_block_till_done()
        assert len(handed_over) == 2
        assert any("reconnect" in name for name in handed_over)
    finally:
        await coordinator.async_stop()


@pytest.mark.parametrize("failure", _BUS_CLOSED, ids=["eof", "bad-fd"])
async def test_priming_that_meets_a_closed_bus_only_logs_it(
    hass: HomeAssistant, failure: Exception
) -> None:
    """The same for the priming the poll does on a link a command made.

    Here the link goes after the read has answered, under the bring-up write.
    """
    coordinator, lamp, _link = await _holding_a_link(hass)
    lamp.readable(NOTIFY_UUID, cbor.encode({KEY_ACTIVATED: False}))
    lamp.fails_writes(failure)

    await link_of(coordinator).prime_held()

    assert link_of(coordinator).diagnostics()["primed"] is False


async def test_stopping_lets_go_of_a_link_made_while_it_was_stopping(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command in flight must not leave a stopped coordinator holding a link.

    Stopping took whatever client it held at that instant and was done. A
    command still running - its write had failed and its retry was dialling -
    then finished its connect and committed the new link to a coordinator
    that no longer watches anything and that nobody will stop again. On a
    lamp whose link holds, that keeps the single slot for good: the
    coordinator a reload puts in its place cannot connect.
    """
    coordinator, lamp, _first = await _holding_a_link(hass)
    lamp.fails_writes(BleakError("dropped"), times=1)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    answered = asyncio.Event()
    lamp.dials_when(answered)

    command = asyncio.create_task(coordinator.async_set_power(True))
    await asyncio.sleep(0)  # its write has failed; the retry is dialling
    stopping = asyncio.create_task(coordinator.async_stop())
    await asyncio.sleep(0)
    answered.set()  # ...and the lamp picks up, after the stop began

    with pytest.raises(HomeAssistantError):
        await command  # the coordinator was stopped under it
    await stopping
    await hass.async_block_till_done()

    assert link_of(coordinator).diagnostics()["connected"] is False
    assert lamp.links[1].hung_up == 1


async def test_stopping_that_is_cancelled_still_lets_go_of_the_link(
    hass: HomeAssistant,
) -> None:
    """Cancelled while it waits for the lock, stopping still hangs up.

    It took the client first and waited afterwards, so a cancellation in that
    wait dropped the only reference to a connected client - the leak, and the
    lamp's slot held by nobody.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    lamp.never_acknowledges_a_write()
    command = asyncio.create_task(coordinator.async_set_power(True))  # in flight
    await asyncio.sleep(0)  # it holds the lock, in its write
    try:
        stopping = asyncio.create_task(coordinator.async_stop())
        await asyncio.sleep(0)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
    finally:
        command.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await command
    await hass.async_block_till_done()

    assert link_of(coordinator).diagnostics()["connected"] is False
    assert link.hung_up == 1


@pytest.mark.parametrize("stopped_by", ["an unload", "Home Assistant stopping"])
async def test_a_command_refused_because_it_has_stopped_says_so(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, stopped_by: str
) -> None:
    """A stopped coordinator takes no new link at all, says so, and says so at once.

    The command is refused because the integration is being reloaded or
    Home Assistant is going down. The radio did nothing, and "the device may
    be out of range ... a Bluetooth proxy near the device usually fixes
    this" is advice for something else: a stopped coordinator does not send
    its user off to buy a Bluetooth proxy. It is said as what it is - and
    once: a link that is refused on purpose is not asked for a second time.
    And at once: the deadline around the command is what tells a refusal
    from a command that never got the lock - stopping takes the lock, and
    one that failed to give it back would end the same way fifteen seconds
    later, for a different reason.
    """
    coordinator, lamp, _link = await _holding_a_link(hass)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    asked = _counting_connects(coordinator)
    if stopped_by == "an unload":
        await coordinator.async_stop()
    else:
        coordinator.async_shutdown()
    assert not link_of(coordinator).lock.locked()

    async with asyncio.timeout(1):
        with pytest.raises(HomeAssistantError) as err:
            await coordinator.async_set_power(True)
    await hass.async_block_till_done()

    assert err.value.translation_key == "not_running"
    assert err.value.translation_placeholders == {"name": "Glowrium-G7"}
    assert len(asked) == 1
    assert lamp.dials == 1  # the link it was stopped with; none since
    assert link_of(coordinator).diagnostics()["connected"] is False


@pytest.mark.parametrize(
    "meanwhile", ["the stack says nothing", "the stack reports it lost"]
)
async def test_a_command_that_runs_out_of_time_inside_a_write_lets_go_of_the_link(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, meanwhile: str
) -> None:
    """A write that has not come back by the deadline is a link not to keep.

    The caller is told the command failed. Left held, the link gets the next
    command too, which waits just as long and fails the same way - until the
    probe, minutes later, finds it dead. Let go of, it is hung up, and the
    next command dials. The stack may report the link gone while the write
    is still waiting: then it has been let go of already, by the callback,
    and the deadline finds nothing of it left to take - one hang-up, not a
    second one for a client that is no longer the coordinator's.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    monkeypatch.setattr(link_module, "_COMMAND_TIMEOUT", 0.05)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    lamp.never_acknowledges_a_write()
    if meanwhile == "the stack reports it lost":
        hass.loop.call_later(0.01, link.lose)  # dropped, while the write waits

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_power(True)
    await hass.async_block_till_done()

    assert err.value.translation_key == "cannot_connect"
    assert link_of(coordinator).diagnostics()["connected"] is False
    assert link.hang_ups == 1
    assert link.hung_up == 1


async def test_shutting_down_does_not_hold_home_assistant_up(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On shutdown the hang-up is fired at once, and given a short ceiling.

    Home Assistant waits for whatever its stop listeners start, and a
    container is killed ten seconds after it is told to stop. What matters
    then is that BlueZ is asked to drop the link straight away - not that the
    lock is free, and not that a link which will not confirm it has closed is
    waited on for the ten seconds a hang-up is normally allowed: that would
    spend the whole grace period, and the rest of Home Assistant's shutdown
    with it. The bus is not worth waiting for either; the process is leaving.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    monkeypatch.setattr(link_module, "_STOP_TIMEOUT", 0.05)
    lamp.hangs_up_when(asyncio.Event())  # never
    lamp.never_acknowledges_a_write()
    command = asyncio.create_task(coordinator.async_set_power(True))  # in flight
    await asyncio.sleep(0)  # it holds the lock: not waited for
    try:
        coordinator.async_shutdown()

        assert link_of(coordinator).diagnostics()["connected"] is False
        assert link.hang_ups == 1  # asked to drop it, already
        async with asyncio.timeout(1):  # nowhere near _HANG_UP_TIMEOUT
            await hass.async_block_till_done()
    finally:
        command.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await command


async def test_shutting_down_says_so_in_the_log(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The one line that shows, afterwards, that the stop was answered.

    Home Assistant is on its way out when this runs, so nothing else will say
    whether the coordinator held a link at that moment and let go of it. On a
    host where the phantom link has been seen, that is the first question.
    """
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    coordinator, _lamp, _link = await _holding_a_link(hass)

    coordinator.async_shutdown()
    await hass.async_block_till_done()
    assert "Home Assistant is stopping: hanging up" in caplog.text

    caplog.clear()
    idle, _idle_lamp = _at_a_lamp(hass)

    idle.async_shutdown()
    assert "Home Assistant is stopping: no link held" in caplog.text


async def test_stopping_with_no_link_does_not_wait_for_a_busy_lock(
    hass: HomeAssistant,
) -> None:
    """With nothing held, unload has nothing to wait for.

    A command that is still dialling holds the lock and holds no link. Once
    the coordinator is stopped that command is refused the link it is dialling
    for, so nothing can turn up while unload waits - and waiting anyway put
    back the reload that takes seconds for no reason, which a ceiling on that
    wait had been introduced to remove.
    """
    coordinator, lamp = _at_a_lamp(hass)
    lamp.dials_when(asyncio.Event())  # a command, dialling - for as long as it takes
    command = asyncio.create_task(coordinator.async_set_power(True))
    await asyncio.sleep(0)
    try:
        async with asyncio.timeout(0.5):  # _STOP_TIMEOUT is three seconds
            await coordinator.async_stop()
    finally:
        command.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await command


async def test_a_hang_up_started_after_the_stop_began_is_as_short(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every hang-up Home Assistant will wait for gets the short ceiling.

    Not only the one for the link held when the stop was announced. A connect
    cancelled by the stop, or a command finishing after it, lets go of a
    client later - and Home Assistant waits for whatever starts after it began
    to stop. Left under the usual ten seconds, one link that will not confirm
    it has closed spends the whole grace period a container is given.
    """
    coordinator, lamp = _at_a_lamp(hass)
    monkeypatch.setattr(link_module, "_STOP_TIMEOUT", 0.05)
    lamp.hangs_up_when(asyncio.Event())  # never
    late = await lamp.dial(MagicMock())  # a client let go of after the stop began

    coordinator.async_shutdown()
    link_of(coordinator).hang_up(late)

    async with asyncio.timeout(1):  # nowhere near _HANG_UP_TIMEOUT
        await hass.async_block_till_done()
    assert late.hang_ups == 1


async def test_stopping_gives_the_lock_back_however_it_ends(
    hass: HomeAssistant,
) -> None:
    """Cancelled while it waits for the hang-up, stopping still releases the lock.

    It takes the lock so that the hang-up does not race a write in flight. A
    lock kept by a coordinator that has stopped would hold up nothing that
    matters - but one kept by a stop that was cancelled and may be tried again
    would hold up that.
    """
    coordinator, lamp, link = await _holding_a_link(hass)
    released = asyncio.Event()
    lamp.hangs_up_when(released)

    stopping = asyncio.create_task(coordinator.async_stop())
    await asyncio.sleep(0)  # it holds the lock and waits for the hang-up
    assert link_of(coordinator).lock.locked()
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert not link_of(coordinator).lock.locked()
    released.set()
    await hass.async_block_till_done()
    assert link.hung_up == 1  # and the hang-up was not cancelled with it


@pytest.mark.parametrize("asked_by", ["a command", "a background connect"])
async def test_a_link_subscribed_while_the_coordinator_stopped_is_not_kept(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, asked_by: str
) -> None:
    """The check for a stop comes after the subscription, not before it.

    The subscription is the last thing a connect waits for before it commits
    the link. A stop that comes and goes during that wait finds nothing held
    and returns; checked any earlier, the connect would then commit its link
    to a coordinator that has already been stopped. A command whose own
    connect was on its way is told why - not sent round a second time to be
    refused at the door instead.
    """
    coordinator, lamp = _at_a_lamp(hass)
    subscribed = asyncio.Event()
    lamp.subscribes_when(subscribed)
    monkeypatch.setattr(link_module, "_CONFIRM_TIMEOUT", 0.01)
    asked = _counting_connects(coordinator)

    if asked_by == "a command":
        connecting = asyncio.create_task(coordinator.async_set_power(True))
    else:
        connecting = asyncio.create_task(link_of(coordinator).connect())
    await asyncio.sleep(0)  # dialled, and now waiting to be subscribed
    await coordinator.async_stop()  # nothing held: over at once
    subscribed.set()

    if asked_by == "a command":
        with pytest.raises(HomeAssistantError) as err:
            await connecting
        assert err.value.translation_key == "not_running"
    else:
        with pytest.raises(link_module.NoNewLinkError):
            await connecting
    await hass.async_block_till_done()

    assert len(asked) == 1
    assert lamp.dials == 1
    assert lamp.links[0].hung_up == 1
    assert link_of(coordinator).diagnostics()["connected"] is False
