"""Tests for the Glowrium coordinator's command encoding."""

import asyncio
from datetime import datetime
import logging
from time import monotonic
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

    async def _reconnect(**_kw: object) -> None:
        reconnects.append(1)
        client.is_connected = True
        coordinator._client = client

    coordinator._connect_locked = _reconnect
    await coordinator.async_set_power(True)
    assert client.write_gatt_char.await_count == 2  # failed, then retried
    assert reconnects  # a reconnect happened before the retry
    assert coordinator.state[KEY_POWER] is True


async def test_write_raises_after_two_failures(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that keeps failing is reported as a readable HA error."""
    coordinator, client = _connected_coordinator(hass)
    # Nothing will confirm this write, so do not sit out the whole grace window.
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.01)
    client.write_gatt_char = AsyncMock(side_effect=BleakError("down"))

    async def _reconnect(**_kw: object) -> None:
        client.is_connected = True
        coordinator._client = client

    coordinator._connect_locked = _reconnect
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_power(True)
    assert err.value.translation_key == "cannot_connect"
    assert isinstance(err.value.__cause__, BleakError)  # the BLE error is kept
    assert client.write_gatt_char.await_count == 2  # tried twice, then gave up


async def test_state_request_abandoned_only_after_repeated_refusal(
    hass: HomeAssistant,
) -> None:
    """A device that keeps rejecting the request is eventually left alone.

    Re-sending it is what destroyed the link on every command for models that
    answer ATT "Insufficient authorization" (0x08) and disconnect - but it takes
    a run of failures, not one: see the transient-failure test below.
    """
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G8")
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(side_effect=BleakError("unreadable"))
    client.write_gatt_char = AsyncMock(
        side_effect=BleakError("Insufficient authorization (8)")
    )

    for _ in range(coordinator_module._STATE_REQUEST_ATTEMPTS):
        await coordinator._request_state(client)
    assert (
        client.write_gatt_char.await_count == coordinator_module._STATE_REQUEST_ATTEMPTS
    )
    assert coordinator._state_request_muted is True

    # Later connects must not re-send it.
    await coordinator._request_state(client)
    await coordinator._request_state(client)
    assert (
        client.write_gatt_char.await_count == coordinator_module._STATE_REQUEST_ATTEMPTS
    )


async def test_one_dropped_link_does_not_abandon_the_state_request(
    hass: HomeAssistant,
) -> None:
    """A transient failure must not cost the session its unread properties.

    A dropped connection raises the same BleakError as an outright refusal, and
    on a weak link it happens routinely - a real G7 hit it 40 s after start-up.
    Abandoning the request there left the indicator, lighting mode, ramp and DST
    unread until Home Assistant was restarted.
    """
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(side_effect=BleakError("unreadable"))
    client.write_gatt_char = AsyncMock(
        side_effect=BleakError("[org.bluez.Error.Failed] Not connected")
    )

    await coordinator._request_state(client)
    assert coordinator._state_request_muted is False  # one failure means nothing

    # And a success clears the count, so occasional failures never accumulate
    # into a false refusal.
    client.write_gatt_char = AsyncMock()
    await coordinator._request_state(client)
    assert coordinator._state_request_failures == 0


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
    assert coordinator._state_request_muted is False
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
    coordinator._state_request_muted_until = monotonic() + 60
    activated = []
    coordinator.async_activate = AsyncMock(side_effect=lambda: activated.append(1))

    await coordinator._async_activate_if_needed()

    assert not activated  # must not replay the vendor bring-up blind
    assert coordinator._activation_checked is True  # and must not re-wait


async def test_partial_read_still_sends_the_request(hass: HomeAssistant) -> None:
    """A read that covers only part of the map must not skip the request.

    Measured on a real lamp: the connect-time read carries only the low property
    block, so the indicator (0x17), lighting mode (0x2b), ramp (0x2f) and DST
    (0x35) are absent from it and arrive solely through the batched request.
    Treating the read as the whole story left those four entities `unknown` for
    the entire session.
    """
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    client = MagicMock()
    client.is_connected = True
    client.write_gatt_char = AsyncMock()
    client.read_gatt_char = AsyncMock(return_value=bytearray.fromhex("a206f5081846"))

    await coordinator._request_state(client)

    client.read_gatt_char.assert_awaited_once_with(NOTIFY_UUID)
    assert coordinator.state[KEY_POWER] is True  # what the read did carry
    assert coordinator.state[KEY_BRIGHTNESS] == 70
    client.write_gatt_char.assert_awaited_once_with(  # and the rest is asked for
        NOTIFY_UUID, bytes(STATE_KEYS), response=True
    )


async def test_read_covering_every_key_skips_the_request(hass: HomeAssistant) -> None:
    """The unreliable request is skipped when the read already has everything."""
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G8")
    client = MagicMock()
    client.is_connected = True
    client.write_gatt_char = AsyncMock()
    client.read_gatt_char = AsyncMock(
        return_value=bytearray(cbor.encode(dict.fromkeys(STATE_KEYS, 0)))
    )

    await coordinator._request_state(client)

    client.read_gatt_char.assert_awaited_once_with(NOTIFY_UUID)
    client.write_gatt_char.assert_not_awaited()


async def test_falls_back_to_request_when_read_fails(hass: HomeAssistant) -> None:
    """If the read is unavailable the batched request is still tried."""
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(side_effect=BleakError("not readable"))
    client.write_gatt_char = AsyncMock(side_effect=BleakError("rejected"))

    await coordinator._request_state(client)
    assert client.write_gatt_char.await_count == 1
    client.write_gatt_char.assert_awaited_with(
        NOTIFY_UUID, bytes(STATE_KEYS), response=True
    )


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
        with pytest.raises(HomeAssistantError) as err:
            await call
        assert err.value.translation_key == "schedule_not_read"
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
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_ramp(30)
    assert err.value.translation_key == "lighting_mode_not_read"
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
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.01)

    async def _never_connects(**_kw: object) -> None:
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
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.01)
    await coordinator._lock.acquire()
    try:
        with pytest.raises(HomeAssistantError) as err:
            await coordinator.async_set_power(True)
    finally:
        coordinator._lock.release()
    assert err.value.translation_key == "cannot_connect"


async def test_trailing_bytes_are_reported_as_themselves(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A frame with trailing bytes is warned about once, not buried in debug.

    Rejecting these is what #5 changed, so on a model whose frames were always
    fully consumed this is the regression that change risks - it has to be
    visible as itself rather than as a generic undecodable frame.
    """
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    frame = bytes.fromhex("a106f5deadbeef")  # {6: True} plus 4 stray bytes

    with caplog.at_level(logging.DEBUG, logger=coordinator_module.__name__):
        assert coordinator._ingest(frame) is False
        assert not coordinator.state  # the frame is still rejected wholesale

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "4 trailing bytes" in warnings[0].getMessage()
        assert frame.hex() in warnings[0].getMessage()
        assert "Undecodable frame" not in caplog.text

        # A second such frame must not warn again - notifications are constant.
        caplog.clear()
        assert coordinator._ingest(frame) is False
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
        assert "trailing bytes" in caplog.text  # still recorded, at debug


async def test_malformed_frame_is_not_reported_as_trailing_bytes(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A truncated frame keeps the generic message and raises no warning."""
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    with caplog.at_level(logging.DEBUG, logger=coordinator_module.__name__):
        assert coordinator._ingest(bytes.fromhex("81")) is False
    assert "Undecodable frame" in caplog.text
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


async def test_setup_is_not_held_by_a_connect_that_never_finishes(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connect to an unreachable lamp must not hold the connection lock open.

    async_setup_entry awaits this path. When the reconnect poll held _lock while
    grinding through attempts to a lamp that was out of range, setup waited on
    that lock with no deadline and the entry stayed in "setup in progress"
    forever - never even reaching setup_retry.
    """
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    monkeypatch.setattr(coordinator_module, "_CONNECT_TIMEOUT", 0.05)

    # Stand in for the other holder: the lock is taken and not given back.
    await coordinator._lock.acquire()
    try:
        with pytest.raises(TimeoutError):
            await coordinator._async_ensure_connected()
    finally:
        coordinator._lock.release()


async def test_lost_acknowledgement_is_not_reported_as_failure(
    hass: HomeAssistant,
) -> None:
    """A write the lamp acted on must not be reported as having failed.

    Observed on a real G7 at RSSI -88: both attempts of light.turn_on raised
    "GATT Protocol Error: Unlikely Error", yet the lamp lit and notified its new
    state 32 ms BEFORE the error surfaced. The user saw a failure toast, a lit
    lamp, and an entity reading `on`.
    """
    coordinator, client = _connected_coordinator(hass)

    async def _write_then_notify(*_args: object, **_kwargs: object) -> None:
        # The device receives the write and reports the new state; only the
        # acknowledgement is lost, so bleak still raises.
        coordinator._ingest(cbor.encode({KEY_POWER: True}))
        raise BleakError("GATT Protocol Error: Unlikely Error")

    client.write_gatt_char = AsyncMock(side_effect=_write_then_notify)

    await coordinator.async_set_power(True)  # must not raise
    assert coordinator.state[KEY_POWER] is True


async def test_a_command_that_truly_failed_still_raises(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silence is not success: with no confirmation the error still surfaces."""
    coordinator, client = _connected_coordinator(hass)
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.05)
    client.write_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_power(True)
    assert err.value.translation_key == "cannot_connect"


async def test_confirmation_ignores_keys_the_device_never_reports(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mode command confirms on 0x2b/0x2f, not on its fixed parameters.

    0x2c and 0x32 are constants the lamp never reports back; requiring them to
    match would mean no mode command could ever be confirmed.
    """
    coordinator, client = _connected_coordinator(hass)
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.05)
    coordinator.state[KEY_LIGHTING_MODE] = 1

    async def _write_then_notify(_uuid: str, data: bytes, **_kw: object) -> None:
        # The lamp reports back the properties it actually tracks - the mode and
        # the ramp - and never 0x2c or 0x32, which are fixed parameters.
        sent = cbor.decode(data)
        reported = {k: v for k, v in sent.items() if k in (KEY_LIGHTING_MODE, 0x2F)}
        assert set(sent) - set(reported) == {0x2C, 0x32}
        coordinator._ingest(cbor.encode(reported))
        raise BleakError("Unlikely Error")

    client.write_gatt_char = AsyncMock(side_effect=_write_then_notify)
    await coordinator.async_set_lighting_mode(5)  # must not raise
    assert coordinator.state[KEY_LIGHTING_MODE] == 5


async def test_command_writes_before_reading_anything(hass: HomeAssistant) -> None:
    """A command connects and writes; it does not pay for priming first.

    Priming costs a device-info read, a state read, the batched request and up
    to 3 s waiting for the activation flag - all before the write, and all
    inside the command budget. On a lamp where the connect alone is marginal
    that is what turned a working command into a reported failure.
    """
    coordinator, client = _connected_coordinator(hass)
    coordinator._client = None
    order: list[str] = []

    async def _connect(*, prime: bool = True) -> None:
        # The point of the fix: a command must ask for a bare link.
        assert prime is False
        order.append("connect")
        client.is_connected = True
        coordinator._client = client

    async def _read(_uuid: str) -> bytes:
        order.append("read")
        return b""

    coordinator._connect_locked = _connect
    client.read_gatt_char = AsyncMock(side_effect=_read)
    client.write_gatt_char = AsyncMock(
        side_effect=lambda *a, **k: order.append("write")
    )

    await coordinator.async_set_power(True)
    assert order == ["connect", "write"]  # nothing read on the way


async def test_a_command_connect_is_primed_by_the_poll(hass: HomeAssistant) -> None:
    """The properties a command's connect skipped are fetched afterwards."""
    coordinator, client = _connected_coordinator(hass)
    primed: list[str] = []
    coordinator._async_prime = AsyncMock(side_effect=lambda: primed.append("primed"))

    # A link exists but nothing has primed it - exactly what a command leaves.
    assert coordinator._primed_client is None
    coordinator._async_poll_reconnect(None)
    await hass.async_block_till_done()
    assert primed == ["primed"]

    # Once primed, the poll leaves it alone.
    coordinator._primed_client = client
    coordinator._async_poll_reconnect(None)
    await hass.async_block_till_done()
    assert primed == ["primed"]


async def test_muted_state_request_recovers_after_the_cooldown(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Muting the request expires, so a weak link is not punished for a session.

    On a real G7 three consecutive failures accumulated 70 s after start-up
    purely from a bad link. Making that permanent cost the lamp four properties
    until Home Assistant was restarted.
    """
    monkeypatch.setattr(coordinator_module, "_STATE_REQUEST_COOLDOWN", 0.05)
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(side_effect=BleakError("unreadable"))
    client.write_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))

    for _ in range(coordinator_module._STATE_REQUEST_ATTEMPTS):
        await coordinator._request_state(client)
    assert coordinator._state_request_muted is True

    sent = client.write_gatt_char.await_count
    await coordinator._request_state(client)
    assert client.write_gatt_char.await_count == sent  # silent while muted

    await asyncio.sleep(0.06)
    assert coordinator._state_request_muted is False
    await coordinator._request_state(client)
    assert client.write_gatt_char.await_count == sent + 1  # and asks again


async def test_unload_does_not_wait_out_a_connect(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopping must not block on the lock a slow connect is holding.

    Unload waiting behind a connect is what made reloading the integration take
    the best part of ten seconds.
    """
    coordinator, client = _connected_coordinator(hass)
    monkeypatch.setattr(coordinator_module, "_STOP_TIMEOUT", 0.05)
    client.disconnect = AsyncMock()

    await coordinator._lock.acquire()  # stand in for a connect in flight
    try:
        await coordinator.async_stop()  # must return, not hang
    finally:
        coordinator._lock.release()
    assert coordinator._client is None
