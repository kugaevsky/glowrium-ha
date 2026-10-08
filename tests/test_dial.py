"""The coordinator's dial: handed in, and Home Assistant's when it is not.

The dial is the seam the link stands on (#21): a callable that is given the
callback for a link that is lost and returns a connected client. Tests hand
the coordinator a scripted lamp's; the integration hands it nothing, and it
then dials through Home Assistant's Bluetooth as it always has.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.glowrium import cbor, coordinator as coordinator_module
from custom_components.glowrium.const import KEY_BRIGHTNESS, KEY_POWER, WRITE_UUID
from custom_components.glowrium.coordinator import GlowriumCoordinator

from .lamp import ADDRESS, ScriptedLamp, lamp_of


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
    assert lamp_of(coordinator) is lamp


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
    monkeypatch.setattr(coordinator_module, "establish_connection", connect)
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
    monkeypatch.setattr(coordinator_module, "establish_connection", connect)
    coordinator = GlowriumCoordinator(hass, ADDRESS, "Glowrium-G7")
    scanners.async_ble_device_from_address.assert_not_called()  # not before a dial

    await coordinator.async_set_power(True)

    scanners.async_ble_device_from_address.assert_called_once_with(
        hass, ADDRESS, connectable=True
    )
    assert connect.await_args.args[1] is device
    link.write_gatt_char.assert_awaited_once_with(
        WRITE_UUID, cbor.encode({KEY_POWER: True}), response=True
    )


async def test_a_lamp_bluetooth_does_not_find_is_not_dialled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing found is "not in range", said before the library is asked."""
    connect = AsyncMock()
    monkeypatch.setattr(coordinator_module, "establish_connection", connect)

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
