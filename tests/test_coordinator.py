"""Tests for the Glowrium coordinator's command encoding."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from bleak.exc import BleakError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.glowrium import cbor, coordinator as coordinator_module
from custom_components.glowrium.const import (
    KEY_BRIGHTNESS,
    KEY_CIRCADIAN,
    KEY_DST,
    KEY_INDICATOR,
    KEY_LIGHTING_MODE,
    KEY_POWER,
    KEY_SCHEDULE,
    KEY_TIMER,
    NOTIFY_UUID,
    STATE_KEYS,
    TIMER_BRIGHTNESS,
    TIMER_DEFAULT,
    TIMER_START_H,
    TIMER_START_M,
    WRITE_UUID,
)
from custom_components.glowrium.coordinator import (
    GlowriumCoordinator,
    _encode_device_time,
    _parse_device_info,
)


def _connected_coordinator(
    hass: HomeAssistant,
) -> tuple[GlowriumCoordinator, MagicMock]:
    """Return a coordinator wired to a fake, already-connected BLE client."""
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    client = MagicMock()
    client.is_connected = True
    client.write_gatt_char = AsyncMock()
    coordinator._client = client
    return coordinator, client


async def test_set_power(hass: HomeAssistant) -> None:
    """Power writes {6: bool} to the command characteristic."""
    coordinator, client = _connected_coordinator(hass)
    await coordinator.async_set_power(True)
    client.write_gatt_char.assert_awaited_once_with(
        WRITE_UUID, bytes.fromhex("a106f5"), response=True
    )
    assert coordinator.state[KEY_POWER] is True


async def test_set_brightness_clamped(hass: HomeAssistant) -> None:
    """Brightness is clamped to 0..100 and encoded as {8: n}."""
    coordinator, client = _connected_coordinator(hass)
    await coordinator.async_set_brightness(150)
    client.write_gatt_char.assert_awaited_once_with(
        WRITE_UUID, bytes.fromhex("a1081864"), response=True
    )
    assert coordinator.state[KEY_BRIGHTNESS] == 100


async def test_set_light_state_batches(hass: HomeAssistant) -> None:
    """Power + brightness go out as a single CBOR map ({6: bool, 8: n})."""
    coordinator, client = _connected_coordinator(hass)
    await coordinator.async_set_light_state(True, 25)
    client.write_gatt_char.assert_awaited_once_with(
        WRITE_UUID, bytes.fromhex("a206f5081819"), response=True
    )
    assert coordinator.state[KEY_POWER] is True
    assert coordinator.state[KEY_BRIGHTNESS] == 25
    # Turning off carries no brightness key.
    client.write_gatt_char.reset_mock()
    await coordinator.async_set_light_state(False)
    client.write_gatt_char.assert_awaited_once_with(
        WRITE_UUID, bytes.fromhex("a106f4"), response=True
    )


async def test_set_lighting_mode_matches_capture(hass: HomeAssistant) -> None:
    """Lighting-mode selection matches the captured command frame."""
    coordinator, client = _connected_coordinator(hass)
    await coordinator.async_set_lighting_mode(5)
    client.write_gatt_char.assert_awaited_once_with(
        WRITE_UUID,
        bytes.fromhex("a4182b05182c4202d0182f420e10183242001e"),
        response=True,
    )
    assert coordinator.state[KEY_LIGHTING_MODE] == 5


async def test_set_ramp_preserves_mode(hass: HomeAssistant) -> None:
    """Ramp re-sends the current lighting mode with a new 0x2f (30 min)."""
    coordinator, client = _connected_coordinator(hass)
    coordinator.state[KEY_LIGHTING_MODE] = 1
    await coordinator.async_set_ramp(30)
    client.write_gatt_char.assert_awaited_once_with(
        WRITE_UUID,
        bytes.fromhex("a4182b01182c4202d0182f420708183242001e"),
        response=True,
    )


async def test_set_operating_mode_circadian(hass: HomeAssistant) -> None:
    """Circadian mode clears schedule (0x0d) then sets circadian (0x09)."""
    coordinator, client = _connected_coordinator(hass)
    await coordinator.async_set_operating_mode("circadian")
    assert client.write_gatt_char.await_count == 2
    client.write_gatt_char.assert_any_await(
        WRITE_UUID, bytes.fromhex("a10df4"), response=True
    )
    client.write_gatt_char.assert_any_await(
        WRITE_UUID, bytes.fromhex("a109f5"), response=True
    )
    assert coordinator.state[KEY_CIRCADIAN] is True
    assert coordinator.state[KEY_SCHEDULE] is False


async def test_circadian_reapplies_ramp(hass: HomeAssistant) -> None:
    """Entering Circadian re-applies the user's ramp (the device resets it)."""
    coordinator, client = _connected_coordinator(hass)
    coordinator.state[KEY_LIGHTING_MODE] = 1
    await coordinator.async_set_ramp(90)  # 90 min = 5400 s = 0x1518
    client.write_gatt_char.reset_mock()
    await coordinator.async_set_operating_mode("circadian")
    # {0x0d: False}, {0x09: True}, then the mode payload re-applying the ramp.
    assert client.write_gatt_char.await_count == 3
    payload = cbor.decode(client.write_gatt_char.await_args_list[-1].args[1])
    assert payload[0x2F] == bytes.fromhex("1518")


async def test_set_indicator(hass: HomeAssistant) -> None:
    """Indicator writes {0x17: bool}."""
    coordinator, client = _connected_coordinator(hass)
    await coordinator.async_set_indicator(True)
    client.write_gatt_char.assert_awaited_once_with(
        WRITE_UUID, bytes.fromhex("a117f5"), response=True
    )
    assert coordinator.state[KEY_INDICATOR] is True


async def test_operating_mode_property(hass: HomeAssistant) -> None:
    """operating_mode is None until read, then reflects circadian/schedule keys."""
    coordinator, _ = _connected_coordinator(hass)
    assert coordinator.operating_mode is None  # state not read yet -> unknown
    coordinator.state[KEY_CIRCADIAN] = False
    coordinator.state[KEY_SCHEDULE] = False
    assert coordinator.operating_mode == "manual"  # both flags read as off
    coordinator.state[KEY_CIRCADIAN] = True
    assert coordinator.operating_mode == "circadian"
    coordinator.state[KEY_CIRCADIAN] = False
    coordinator.state[KEY_SCHEDULE] = True
    assert coordinator.operating_mode == "schedule"


async def test_mode_allows_when_mode_unknown(hass: HomeAssistant) -> None:
    """mode_allows keeps mode entities available while the mode is unknown."""
    coordinator, _ = _connected_coordinator(hass)
    # Unknown mode (state not read) -> allowed for every mode, so nothing hides.
    assert coordinator.mode_allows("circadian") is True
    assert coordinator.mode_allows("schedule") is True
    # Once known, only the matching mode is allowed.
    coordinator.state[KEY_CIRCADIAN] = True
    coordinator.state[KEY_SCHEDULE] = False
    assert coordinator.mode_allows("circadian") is True
    assert coordinator.mode_allows("schedule") is False


async def test_set_dst(hass: HomeAssistant) -> None:
    """DST writes {0x35: [enabled, offset]} - enabled byte 01, offset 3600s."""
    coordinator, client = _connected_coordinator(hass)
    await coordinator.async_set_dst(True)
    client.write_gatt_char.assert_awaited_once_with(
        WRITE_UUID, bytes.fromhex("a11835450100000e10"), response=True
    )
    assert coordinator.state[KEY_DST] == bytes.fromhex("0100000e10")


async def test_sync_location(hass: HomeAssistant) -> None:
    """Sync writes HA's home coordinates as float64 to keys 0x0a/0x0b."""
    coordinator, client = _connected_coordinator(hass)
    hass.config.latitude = 41.3166
    hass.config.longitude = 69.2906
    await coordinator.async_sync_location()
    client.write_gatt_char.assert_awaited_once_with(
        WRITE_UUID, cbor.encode({0x0A: 41.3166, 0x0B: 69.2906}), response=True
    )


async def test_set_timer_start(hass: HomeAssistant) -> None:
    """Setting the schedule start edits only the start bytes of the 0x11 slot."""
    coordinator, client = _connected_coordinator(hass)
    coordinator.state[KEY_TIMER] = bytes(TIMER_DEFAULT)  # slot must be read first
    await coordinator.async_set_timer_start(7, 15)
    expected = bytearray(TIMER_DEFAULT)
    expected[4], expected[5] = 7, 15
    client.write_gatt_char.assert_awaited_once_with(
        WRITE_UUID, cbor.encode({KEY_TIMER: bytes(expected)}), response=True
    )


async def test_set_timer_gradual(hass: HomeAssistant) -> None:
    """Gradual is stored as 2-byte big-endian seconds (5 min -> 300 = 0x012c)."""
    coordinator, client = _connected_coordinator(hass)
    coordinator.state[KEY_TIMER] = bytes(TIMER_DEFAULT)  # slot must be read first
    await coordinator.async_set_timer_gradual(5)
    expected = bytearray(TIMER_DEFAULT)
    expected[9:11] = (300).to_bytes(2, "big")
    client.write_gatt_char.assert_awaited_once_with(
        WRITE_UUID, cbor.encode({KEY_TIMER: bytes(expected)}), response=True
    )


async def test_available_follows_presence_not_connection(hass: HomeAssistant) -> None:
    """Availability tracks presence or a live link, so it does not flap on reconnect."""
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    assert coordinator.available is False  # neither present nor connected
    coordinator._present = True
    assert coordinator.available is True  # advertising -> available
    coordinator._present = False
    client = MagicMock()
    client.is_connected = True
    coordinator._client = client
    assert coordinator.available is True  # connected -> available
    client.is_connected = False
    assert coordinator.available is False  # link dropped and gone -> unavailable


async def test_presence_callbacks_notify(hass: HomeAssistant) -> None:
    """Advertisement/unavailable callbacks flip presence and notify listeners."""
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    coordinator._reconnecting = True  # suppress the reconnect attempt
    updates: list[int] = []
    coordinator.async_add_listener(lambda: updates.append(1))
    coordinator._async_on_advertisement(MagicMock(), MagicMock())
    assert coordinator._present is True
    coordinator._async_on_unavailable(MagicMock())
    assert coordinator._present is False
    assert updates == [1, 1]  # notified on the present flip and on going away


def test_encode_device_time() -> None:
    """Local time encodes as year_be(2), month, day, hour, minute, second."""
    stamp = datetime(2026, 7, 18, 21, 24, 35)
    assert _encode_device_time(stamp).hex() == "07ea0712151823"


async def test_async_activate_sequence(hass: HomeAssistant) -> None:
    """Bring-up replays the app's sequence: {0x53}, {time, 0x31}, then {0x14}."""
    coordinator, client = _connected_coordinator(hass)
    await coordinator.async_activate()
    assert client.write_gatt_char.await_count == 3
    payloads = [cbor.decode(c.args[1]) for c in client.write_gatt_char.await_args_list]
    assert payloads[0] == {0x53: 300}
    assert payloads[1].keys() == {0x05, 0x31}
    assert payloads[1][0x31] == 1
    assert payloads[2] == {0x14: True}
    assert coordinator.state[0x14] is True


async def test_activated_property(hass: HomeAssistant) -> None:
    """Activated reflects the device's 0x14 flag."""
    coordinator, _ = _connected_coordinator(hass)
    assert coordinator.activated is None
    coordinator.state[0x14] = False
    assert coordinator.activated is False
    coordinator.state[0x14] = True
    assert coordinator.activated is True


def test_parse_device_info() -> None:
    """The facebd80 device-info string parses into a key/value map."""
    raw = (
        b"brand:Glowrium;pkey:Glowrium-C051;subid:3;"
        b"devid:CST-80F4166DCB8A;mac:80F4166DCB8A;version:4;;"
    )
    info = _parse_device_info(raw)
    assert info["pkey"] == "Glowrium-C051"
    assert info["version"] == "4"
    assert info["devid"] == "CST-80F4166DCB8A"


async def test_device_info_properties(hass: HomeAssistant) -> None:
    """model_id/sw_version/serial_number derive from the parsed device-info."""
    coordinator, _ = _connected_coordinator(hass)
    assert coordinator.sw_version is None
    coordinator.device_info = {
        "pkey": "Glowrium-C051",
        "version": "4",
        "devid": "CST-80F4166DCB8A",
    }
    assert coordinator.model_id == "Glowrium-C051"
    assert coordinator.sw_version == "4"
    assert coordinator.serial_number == "CST-80F4166DCB8A"


async def test_model_resolution(hass: HomeAssistant) -> None:
    """coordinator.model resolves the pkey, with a generic (not G7) fallback."""
    coordinator, _ = _connected_coordinator(hass)
    # Not read yet -> generic profile (reference presets, no false model name).
    assert coordinator.model.name == "Glowrium"
    assert "Sun SYNC" in coordinator.model.lighting_modes
    # Known pkey -> full G7 profile.
    coordinator.device_info = {"pkey": "Glowrium-C051"}
    assert coordinator.model.name == "Glowrium G7"
    # Unknown pkey -> generic, not masquerading as a G7.
    coordinator.device_info = {"pkey": "Glowrium-XXXX"}
    assert coordinator.model.name == "Glowrium"
    assert "Sun SYNC" in coordinator.model.lighting_modes


async def test_write_retries_once_after_a_dropped_link(hass: HomeAssistant) -> None:
    """A write that fails once reconnects and retries before succeeding."""
    coordinator, client = _connected_coordinator(hass)
    client.write_gatt_char = AsyncMock(side_effect=[BleakError("dropped"), None])
    reconnects: list[int] = []

    async def _reconnect() -> None:
        reconnects.append(1)
        client.is_connected = True
        coordinator._client = client

    coordinator._connect_locked = _reconnect
    await coordinator.async_set_power(True)
    assert client.write_gatt_char.await_count == 2  # failed, then retried
    assert reconnects  # a reconnect happened before the retry
    assert coordinator.state[KEY_POWER] is True


async def test_write_raises_after_two_failures(hass: HomeAssistant) -> None:
    """A write that keeps failing is reported as a readable HA error."""
    coordinator, client = _connected_coordinator(hass)
    client.write_gatt_char = AsyncMock(side_effect=BleakError("down"))

    async def _reconnect() -> None:
        client.is_connected = True
        coordinator._client = client

    coordinator._connect_locked = _reconnect
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_power(True)
    assert err.value.translation_key == "cannot_connect"
    assert isinstance(err.value.__cause__, BleakError)  # the BLE error is kept
    assert client.write_gatt_char.await_count == 2  # tried twice, then gave up


async def test_state_request_sent_once_then_never_again(hass: HomeAssistant) -> None:
    """A device that rejects the state request is not asked on later connects.

    Re-sending it is what destroyed the link on every command for models that
    answer ATT "Insufficient authorization" (0x08) and disconnect.
    """
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G8")
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(side_effect=BleakError("unreadable"))
    client.write_gatt_char = AsyncMock(
        side_effect=BleakError("Insufficient authorization (8)")
    )

    await coordinator._request_state(client)
    assert client.write_gatt_char.await_count == 1
    assert coordinator._state_request_rejected is True

    # Later connects must not re-send it.
    await coordinator._request_state(client)
    await coordinator._request_state(client)
    assert client.write_gatt_char.await_count == 1


async def test_state_request_repeats_while_accepted(hass: HomeAssistant) -> None:
    """An unreadable device that accepts the request keeps being asked."""
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(side_effect=BleakError("unreadable"))
    client.write_gatt_char = AsyncMock()

    await coordinator._request_state(client)
    await coordinator._request_state(client)
    assert client.write_gatt_char.await_count == 2
    assert coordinator._state_request_rejected is False
    client.write_gatt_char.assert_awaited_with(
        NOTIFY_UUID, bytes(STATE_KEYS), response=True
    )


async def test_activation_skipped_when_state_unreadable(hass: HomeAssistant) -> None:
    """A device whose state cannot be read is never activated, and never waits.

    0x14 can never arrive on such a device, so the 3 s wait would run on every
    connect - including the connect the command path performs.
    """
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G8")
    client = MagicMock()
    client.is_connected = True
    coordinator._client = client
    coordinator._state_request_rejected = True
    activated = []
    coordinator.async_activate = AsyncMock(side_effect=lambda: activated.append(1))

    await coordinator._async_activate_if_needed()

    assert not activated  # must not replay the vendor bring-up blind
    assert coordinator._activation_checked is True  # and must not re-wait


async def test_state_primed_by_read_without_a_request(hass: HomeAssistant) -> None:
    """A readable device is read, and the unreliable request is not sent."""
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G8")
    client = MagicMock()
    client.is_connected = True
    client.write_gatt_char = AsyncMock()
    client.read_gatt_char = AsyncMock(return_value=bytearray.fromhex("a206f5081846"))

    await coordinator._request_state(client)

    client.read_gatt_char.assert_awaited_once_with(NOTIFY_UUID)
    client.write_gatt_char.assert_not_awaited()  # no request needed
    assert coordinator.state[KEY_POWER] is True
    assert coordinator.state[KEY_BRIGHTNESS] == 70


async def test_falls_back_to_request_when_read_fails(hass: HomeAssistant) -> None:
    """If the read is unavailable the batched request is still tried, once."""
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(side_effect=BleakError("not readable"))
    client.write_gatt_char = AsyncMock(side_effect=BleakError("rejected"))

    await coordinator._request_state(client)
    assert client.write_gatt_char.await_count == 1
    assert coordinator._state_request_rejected is True

    await coordinator._request_state(client)  # never asked again
    assert client.write_gatt_char.await_count == 1


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
    coordinator, client = _connected_coordinator(hass)
    for call in (
        coordinator.async_set_timer_start(7, 30),
        coordinator.async_set_timer_end(19, 0),
        coordinator.async_set_timer_brightness(80),
        coordinator.async_set_timer_gradual(15),
    ):
        with pytest.raises(HomeAssistantError):
            await call
    client.write_gatt_char.assert_not_awaited()


async def test_schedule_setters_work_once_slot_is_known(hass: HomeAssistant) -> None:
    """With the slot read, a setter changes only its own field."""
    coordinator, client = _connected_coordinator(hass)
    coordinator.state[KEY_TIMER] = bytes.fromhex("010200ff0a121212640000")
    await coordinator.async_set_timer_start(7, 30)
    written = cbor.decode(client.write_gatt_char.await_args_list[-1].args[1])[KEY_TIMER]
    assert written[TIMER_START_H], written[TIMER_START_M] == (7, 30)
    assert written[0] == 0x01  # enabled flag preserved, not forced
    assert written[TIMER_BRIGHTNESS] == 0x64  # brightness untouched


async def test_ramp_refuses_when_lighting_mode_unread(hass: HomeAssistant) -> None:
    """Setting the ramp must not silently reset the lighting mode to index 1."""
    coordinator, client = _connected_coordinator(hass)
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_ramp(30)
    client.write_gatt_char.assert_not_awaited()


async def test_command_gives_up_instead_of_hanging(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable device fails the command promptly, not after minutes.

    bleak's own retries can keep a connect attempt alive for minutes, which
    made a button in the UI look like it had hung; the command budget caps it.
    """
    coordinator, _client = _connected_coordinator(hass)
    coordinator._client = None
    monkeypatch.setattr(coordinator_module, "_COMMAND_TIMEOUT", 0.05)

    async def _never_connects() -> None:
        await asyncio.Event().wait()

    coordinator._connect_locked = _never_connects
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_power(True)
    assert err.value.translation_key == "cannot_connect"


async def test_command_budget_covers_waiting_for_the_lock(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command blocked by a background reconnect gives up too.

    The reconnect poll holds ``_lock`` while it retries, so the budget has to
    cover the wait for the lock, not just the write itself.
    """
    coordinator, _client = _connected_coordinator(hass)
    monkeypatch.setattr(coordinator_module, "_COMMAND_TIMEOUT", 0.05)
    await coordinator._lock.acquire()
    try:
        with pytest.raises(HomeAssistantError) as err:
            await coordinator.async_set_power(True)
    finally:
        coordinator._lock.release()
    assert err.value.translation_key == "cannot_connect"
