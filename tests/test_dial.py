"""The coordinator's dial: handed in, and Home Assistant's when it is not.

The dial is the seam the link stands on (#21): a callable that is given the
callback for a link that is lost and returns a connected client. Tests hand
the coordinator a scripted lamp's; the integration hands it nothing, and it
then dials through Home Assistant's Bluetooth as it always has.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.glowrium import (
    cbor,
    coordinator as coordinator_module,
    link as link_module,
)
from custom_components.glowrium.const import (
    INFO_UUID,
    KEY_BRIGHTNESS,
    KEY_POWER,
    NOTIFY_UUID,
    WRITE_UUID,
)
from custom_components.glowrium.coordinator import GlowriumCoordinator

from .lamp import ADDRESS, ScriptedLamp, lamp_of, link_of


async def test_the_coordinator_dials_through_the_dial_it_is_handed(
    hass: HomeAssistant,
) -> None:
    """A command to a lamp with no link dials it, once, and is written to it."""
    lamp = ScriptedLamp()
    coordinator = GlowriumCoordinator(hass, ADDRESS, "Glowrium-G7", dial=lamp.dial)

    await coordinator.async_set_power(True)
    await coordinator.async_set_brightness(40)

    assert lamp.dials == 1  # the second command went out on the link of the first
    assert lamp.written == [
        (WRITE_UUID, cbor.encode({KEY_POWER: True})),
        (WRITE_UUID, cbor.encode({KEY_BRIGHTNESS: 40})),
    ]


async def test_what_the_lamp_says_reaches_the_mirror(hass: HomeAssistant) -> None:
    """The link the dial hands out is subscribed to before it is kept."""
    lamp = ScriptedLamp()
    coordinator = lamp.coordinator(hass)
    await coordinator.async_set_power(True)

    lamp.say(cbor.encode({KEY_BRIGHTNESS: 70}))

    assert coordinator.brightness_percent == 70


async def test_a_frame_goes_to_what_takes_frames_when_it_arrives(
    hass: HomeAssistant,
) -> None:
    """Not to what took them when the coordinator was built.

    The bench taps the frames by replacing the coordinator's intake before it
    connects. The link is handed its listener once, at construction, so that
    listener has to look the intake up each time.
    """
    lamp = ScriptedLamp()
    coordinator = lamp.coordinator(hass)
    tapped: list[bytes] = []
    coordinator._on_notify = lambda _characteristic, data: tapped.append(bytes(data))

    await coordinator.async_set_power(True)
    lamp.say(b"\xa0")

    assert tapped == [b"\xa0"]


async def test_a_link_the_dial_reports_lost_is_let_go_of(hass: HomeAssistant) -> None:
    """The dial is given the coordinator's own callback for a lost link.

    Through it the coordinator hears that the link is gone, hangs the client
    up - which is what closes its connection to the system bus - and dials
    afresh for the next command.
    """
    lamp = ScriptedLamp()
    coordinator = lamp.coordinator(hass)
    await coordinator.async_set_power(True)
    first = lamp.links[0]

    lamp.lose()
    await hass.async_block_till_done()

    assert first.hung_up
    await coordinator.async_set_power(False)
    assert lamp.dials == 2
    assert lamp.written[-1] == (WRITE_UUID, cbor.encode({KEY_POWER: False}))


async def test_the_scripted_lamp_is_silent_once_hung_up(hass: HomeAssistant) -> None:
    """What the lamp says after a hang-up reaches nobody.

    The tests lean on this: a lamp that went on reporting over a link that had
    been hung up would confirm a command no link could have carried.
    """
    lamp = ScriptedLamp()
    coordinator = lamp.coordinator(hass)
    await coordinator.async_set_power(True)
    lamp.say(cbor.encode({KEY_BRIGHTNESS: 70}))

    await coordinator.async_stop()
    lamp.say(cbor.encode({KEY_BRIGHTNESS: 15}))

    assert lamp.links[0].hung_up
    assert coordinator.brightness_percent == 70


async def test_the_scripted_lamp_is_silent_once_lost_and_takes_no_write() -> None:
    """A link that is lost says nothing more either, and refuses what is written.

    Asked of the lamp alone: with a coordinator at the dial, the hang-up that
    follows a loss would silence the link by itself, and hide a lamp that
    went on talking over a link it had just reported lost.
    """
    lamp = ScriptedLamp()
    heard: list[bytes] = []
    lost = MagicMock()
    link = await lamp.dial(lost)
    await link.start_notify("any", lambda _characteristic, data: heard.append(data))
    lamp.say(b"\xa0")

    lamp.lose()
    lamp.lose()  # and a link is lost once
    lamp.say(b"\xa1")

    assert heard == [b"\xa0"]
    lost.assert_called_once_with(link)
    assert not link.is_connected
    with pytest.raises(BleakError, match="Not connected"):
        await link.write_gatt_char(WRITE_UUID, b"\xa0")
    assert lamp.written == []


async def test_a_link_that_was_hung_up_takes_no_write_either() -> None:
    """The same of a link the coordinator let go of."""
    lamp = ScriptedLamp()
    link = await lamp.dial(MagicMock())

    await link.disconnect()

    with pytest.raises(BleakError, match="Not connected"):
        await link.write_gatt_char(WRITE_UUID, b"\xa0")
    with pytest.raises(BleakError, match="Not connected"):
        await link.start_notify("any", MagicMock())
    assert lamp.written == []


async def test_a_lamp_given_nothing_to_read_fails_the_read_as_a_lost_link() -> None:
    """A read of the scripted lamp fails as a lamp that cannot be read does.

    The first exchange on a link ends with the device-info read, so a lamp
    that is dialled and greeted is read. One given nothing to read answers
    "Not connected" - the library's error for a link that is gone - and the
    device half carries on without the device info, as it does on a lamp
    whose read fails.
    """
    lamp = ScriptedLamp()
    link = await lamp.dial(MagicMock())

    with pytest.raises(BleakError, match="Not connected"):
        await link.read_gatt_char(INFO_UUID)


async def test_a_test_takes_the_coordinators_link_through_one_door(
    hass: HomeAssistant,
) -> None:
    """``link_of`` is the one place a test reaches the link the coordinator holds.

    The link's own interface - whether it is connected, in reach, the tick,
    an advertisement - is what Home Assistant's watchers drive; a test drives
    it through this door instead of standing up the Bluetooth manager and
    the timer, and the door is the one reach into the coordinator the tests
    keep, with this as its reason.
    """
    lamp = ScriptedLamp()
    coordinator = lamp.coordinator(hass)
    assert not link_of(coordinator).connected

    await coordinator.async_set_power(True)

    assert link_of(coordinator).connected
    assert link_of(coordinator).in_reach is coordinator.available


async def test_a_write_the_lamp_is_scripted_to_fail_is_taken_and_then_fails() -> None:
    """A failed write was still put to the lamp: noted, then the error is raised.

    ``times`` writes fail - every one, if it is not given. A lamp at the edge
    of range fails one write and takes the next, on the link the retry made.
    """
    lamp = ScriptedLamp()
    lamp.fails_writes(BleakError("dropped"), times=1)
    link = await lamp.dial(MagicMock())

    with pytest.raises(BleakError, match="dropped"):
        await link.write_gatt_char(WRITE_UUID, b"\xa0")
    await link.write_gatt_char(WRITE_UUID, b"\xa1")

    assert lamp.written == [(WRITE_UUID, b"\xa0"), (WRITE_UUID, b"\xa1")]


async def test_a_lamp_that_acts_on_a_write_and_loses_the_acknowledgement() -> None:
    """It reports what the write set, and only then is the write called failed.

    ``saying`` is what the lamp reports back, made from the frame it was
    written: on a G7 at the edge of range the report arrived before the error
    did, and that is the order here.
    """
    lamp = ScriptedLamp()
    lamp.fails_writes(
        BleakError("Unlikely Error"), saying=lambda frame: b"\xa1\x06" + frame[-1:]
    )
    heard: list[bytes] = []
    link = await lamp.dial(MagicMock())
    await link.start_notify("any", lambda _characteristic, data: heard.append(data))

    with pytest.raises(BleakError, match="Unlikely Error"):
        await link.write_gatt_char(WRITE_UUID, b"\xa1\x06\xf5")

    assert heard == [b"\xa1\x06\xf5"]
    assert lamp.written == [(WRITE_UUID, b"\xa1\x06\xf5")]


async def test_a_lamp_that_answers_reports_the_ids_it_was_asked_for() -> None:
    """The state request is a write of ids; the answer is one report of them.

    As a G7 answers: inside the write, with every id asked for - zero unless
    the lamp was given a value - and only those it knows (``only``). What it
    was asked is kept apart from what it was commanded (``asked``).
    """
    lamp = ScriptedLamp()
    lamp.answers({KEY_POWER: True}, only=(KEY_POWER, KEY_BRIGHTNESS))
    heard: list[bytes] = []
    link = await lamp.dial(MagicMock())
    await link.start_notify("any", lambda _characteristic, data: heard.append(data))

    await link.write_gatt_char(NOTIFY_UUID, bytes((KEY_POWER, KEY_BRIGHTNESS, 0x14)))
    await link.write_gatt_char(WRITE_UUID, b"\xa0")

    assert [cbor.decode(frame) for frame in heard] == [
        {KEY_POWER: True, KEY_BRIGHTNESS: 0}
    ]
    assert lamp.asked == [bytes((KEY_POWER, KEY_BRIGHTNESS, 0x14))]
    assert lamp.written[-1] == (WRITE_UUID, b"\xa0")


async def test_a_lamp_may_answer_in_a_moment_or_with_a_frame_of_its_own() -> None:
    """``after`` holds the answer back; ``frame`` is reported as it is given."""
    lamp = ScriptedLamp()
    lamp.answers(after=0.01, frame=b"\xa1\x08\x18\x46")
    heard: list[bytes] = []
    link = await lamp.dial(MagicMock())
    await link.start_notify("any", lambda _characteristic, data: heard.append(data))

    writing = asyncio.create_task(link.write_gatt_char(NOTIFY_UUID, bytes([0x08])))
    await asyncio.sleep(0)
    assert heard == []  # not yet
    await writing

    assert heard == [b"\xa1\x08\x18\x46"]


async def test_a_lamp_is_read_what_it_was_given_to_read_in_the_order_asked() -> None:
    """``readable`` gives a characteristic a value; the rest fail as before.

    What the lamp was asked, written and read is kept in one order
    (``exchanges``): the device info is to be read last on a link, and that
    is a fact about order.
    """
    lamp = ScriptedLamp()
    lamp.readable(INFO_UUID, b"brand:x;;")
    link = await lamp.dial(MagicMock())
    await link.start_notify("any", MagicMock())

    await link.write_gatt_char(WRITE_UUID, b"\xa0")
    assert await link.read_gatt_char(INFO_UUID) == bytearray(b"brand:x;;")
    with pytest.raises(BleakError, match="Not connected"):
        await link.read_gatt_char(NOTIFY_UUID)

    assert link.subscribed
    assert lamp.read == [INFO_UUID, NOTIFY_UUID]
    assert lamp.exchanges == [
        ("write", WRITE_UUID),
        ("read", INFO_UUID),
        ("read", NOTIFY_UUID),
    ]


async def test_a_lamp_that_never_acknowledges_keeps_a_write_waiting() -> None:
    """The write neither returns nor fails: what a deadline is for."""
    lamp = ScriptedLamp()
    lamp.never_acknowledges_a_write()
    link = await lamp.dial(MagicMock())

    writing = asyncio.create_task(link.write_gatt_char(WRITE_UUID, b"\xa0"))
    await asyncio.sleep(0.01)

    assert not writing.done()
    assert lamp.written == [(WRITE_UUID, b"\xa0")]  # it was put to the lamp
    writing.cancel()


def test_a_helper_given_the_coordinator_alone_finds_its_lamp(
    hass: HomeAssistant,
) -> None:
    """And says what is wrong when the coordinator was built at none."""
    lamp = ScriptedLamp()

    assert lamp_of(lamp.coordinator(hass)) is lamp
    with pytest.raises(LookupError, match="not built at a scripted lamp"):
        lamp_of(GlowriumCoordinator(hass, ADDRESS, "Glowrium-G7"))


async def test_a_lamp_out_of_range_fails_the_command_as_one_that_cannot_connect(
    hass: HomeAssistant,
) -> None:
    """The dial's own error is a lost link like any other."""
    lamp = ScriptedLamp()
    lamp.out_of_range()
    coordinator = lamp.coordinator(hass)

    with pytest.raises(HomeAssistantError) as raised:
        await coordinator.async_set_power(True)

    assert raised.value.translation_key == "cannot_connect"
    assert lamp.dials == 2  # tried, and tried once more
    assert lamp.written == []


async def test_handed_no_dial_the_coordinator_dials_by_bluetooth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dial the integration itself uses: what Bluetooth finds, connected.

    The one place the library's connect is replaced: it is what this dial is
    made of. It is called as it always was - with the client class that caches
    services, the device found, the lamp's name, the callback for a lost link
    and the number of tries.
    """
    device, client = object(), MagicMock()
    connect = AsyncMock(return_value=client)
    monkeypatch.setattr(link_module, "establish_connection", connect)
    lost = MagicMock()

    dial = coordinator_module.dial_by_bluetooth(lambda: device, ADDRESS, "Glowrium-G7")

    assert await dial(lost) is client
    connect.assert_awaited_once_with(
        BleakClientWithServiceCache,
        device,
        "Glowrium-G7",
        disconnected_callback=lost,
        max_attempts=3,
    )


async def test_handed_no_dial_the_lamp_is_looked_up_in_home_assistant(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the integration's own setup gets: it passes the coordinator no dial.

    The lamp is then asked of Home Assistant's scanners at the moment of each
    dial - as a device that can be connected to, by its address - and what
    they have is what is dialled.
    """
    device, link = object(), MagicMock()
    link.is_connected = True
    link.start_notify = AsyncMock()
    link.write_gatt_char = AsyncMock()
    scanners = MagicMock()
    scanners.async_ble_device_from_address.return_value = device
    monkeypatch.setattr(coordinator_module, "bluetooth", scanners)
    connect = AsyncMock(return_value=link)
    monkeypatch.setattr(link_module, "establish_connection", connect)
    coordinator = GlowriumCoordinator(hass, ADDRESS, "Glowrium-G7")
    scanners.async_ble_device_from_address.assert_not_called()  # not before a dial

    await coordinator.async_set_power(True)

    scanners.async_ble_device_from_address.assert_called_once_with(
        hass, ADDRESS, connectable=True
    )
    assert connect.await_args.args[1:] == (device, "Glowrium-G7")
    assert connect.await_args.kwargs["max_attempts"] == 3
    link.write_gatt_char.assert_awaited_once_with(
        WRITE_UUID, cbor.encode({KEY_POWER: True}), response=True
    )


async def test_a_lamp_bluetooth_does_not_find_is_not_dialled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing found is "not in range", said before the library is asked."""
    connect = AsyncMock()
    monkeypatch.setattr(link_module, "establish_connection", connect)

    dial = coordinator_module.dial_by_bluetooth(lambda: None, ADDRESS, "Glowrium-G7")

    with pytest.raises(BleakError, match=f"{ADDRESS} is not in range"):
        await dial(MagicMock())
    connect.assert_not_awaited()


async def test_without_home_assistant_and_without_a_dial_nothing_is_found(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Looking the lamp up goes through Home Assistant's scanners.

    The bench scans for itself and hands the coordinator a dial to what it
    found; built with neither, the coordinator says "not in range" rather
    than falling over.
    """
    coordinator = GlowriumCoordinator(None, ADDRESS, "bench")
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)

    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_power(True)

    assert f"{ADDRESS} is not in range" in caplog.text
