"""Tests for the Glowrium coordinator's command encoding."""

import asyncio
from datetime import datetime
import logging
from time import monotonic
from unittest.mock import AsyncMock, MagicMock

from bleak.exc import BleakError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import pytest

from custom_components.glowrium import cbor, coordinator as coordinator_module
from custom_components.glowrium.const import (
    DST_OFF,
    DST_ON,
    KEY_ACTIVATED,
    KEY_BRIGHTNESS,
    KEY_CIRCADIAN,
    KEY_DST,
    KEY_INDICATOR,
    KEY_LIGHTING_MODE,
    KEY_POWER,
    KEY_RAMP,
    KEY_SCHEDULE,
    KEY_TIME,
    KEY_TIME_SYNCED,
    KEY_TIMER,
    NOTIFY_UUID,
    STATE_KEYS,
    TIMER_BRIGHTNESS,
    TIMER_DEFAULT,
    TIMER_END_H,
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
    client.disconnect = AsyncMock()
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
    # The read has to work: a request that fails on a link which just served a
    # read is a refusal, and one that fails alongside the read is a bad link.
    client = _refusing_client()

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
    """With the slot read, a setter changes only its own field.

    The slot below is deliberately unlike TIMER_DEFAULT in every byte a setter
    could clobber: a fixture that shares the enabled flag or the brightness with
    the default cannot tell "preserved the user's value" from "substituted the
    default", which is the regression this exists to catch.
    """
    coordinator, client = _connected_coordinator(hass)
    slot = bytes.fromhex("000300fe091111115a0102")
    assert slot != TIMER_DEFAULT
    coordinator.state[KEY_TIMER] = slot
    await coordinator.async_set_timer_start(7, 30)
    written = cbor.decode(client.write_gatt_char.await_args_list[-1].args[1])[KEY_TIMER]
    assert (written[TIMER_START_H], written[TIMER_START_M]) == (7, 30)
    # Every other byte is the user's, untouched.
    untouched = [i for i in range(len(slot)) if i not in (TIMER_START_H, TIMER_START_M)]
    assert [written[i] for i in untouched] == [slot[i] for i in untouched]


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
        coordinator._ingest(frame)
        assert not coordinator.state  # the frame is still rejected wholesale

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "4 trailing bytes" in warnings[0].getMessage()
        assert frame.hex() in warnings[0].getMessage()
        assert "Undecodable frame" not in caplog.text

        # A second such frame must not warn again - notifications are constant.
        caplog.clear()
        coordinator._ingest(frame)
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
        assert "trailing bytes" in caplog.text  # still recorded, at debug


async def test_malformed_frame_is_not_reported_as_trailing_bytes(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A truncated frame keeps the generic message and raises no warning."""
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    with caplog.at_level(logging.DEBUG, logger=coordinator_module.__name__):
        coordinator._ingest(bytes.fromhex("81"))
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
    """The properties a command's connect skipped are actually fetched later.

    A command connects without priming to stay inside its budget, so something
    has to go back for the rest. Asserting that the poll calls a method by name
    would pass with that method emptied out; what matters is that the device is
    read.
    """
    coordinator, client = _connected_coordinator(hass)
    # A real lamp reports its activation flag too; without it every connect
    # sits out the 3 s wait for 0x14.
    client.read_gatt_char = AsyncMock(
        return_value=bytearray(cbor.encode({KEY_POWER: True, KEY_ACTIVATED: True}))
    )

    # A link exists that nothing has primed - exactly what a command leaves.
    coordinator._async_poll_reconnect(None)
    await hass.async_block_till_done()

    client.read_gatt_char.assert_awaited_with(NOTIFY_UUID)
    assert coordinator.state[KEY_POWER] is True  # the properties actually landed

    # And it is not re-read on every tick from then on.
    reads = client.read_gatt_char.await_count
    coordinator._async_poll_reconnect(None)
    await hass.async_block_till_done()
    assert client.read_gatt_char.await_count == reads


async def test_a_device_reporting_unactivated_is_brought_up(
    hass: HomeAssistant,
) -> None:
    """A lamp whose 0x14 reads False is activated, not merely noticed.

    This is the whole point of the bring-up: a factory-reset lamp advertises
    and accepts config writes, but gates its light output on 0x14, so without
    this it stays dark however many commands it is sent.
    """
    coordinator, client = _connected_coordinator(hass)
    coordinator.state[KEY_ACTIVATED] = False

    await coordinator._async_activate_if_needed()

    written = [
        cbor.decode(call.args[1]) for call in client.write_gatt_char.await_args_list
    ]
    assert KEY_ACTIVATED in written[-1]
    assert written[-1][KEY_ACTIVATED] is True  # the flag that ungates the light


async def test_an_activated_device_is_left_alone(hass: HomeAssistant) -> None:
    """A lamp already reporting 0x14 True is not put through the bring-up."""
    coordinator, client = _connected_coordinator(hass)
    coordinator.state[KEY_ACTIVATED] = True

    await coordinator._async_activate_if_needed()

    client.write_gatt_char.assert_not_awaited()


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
    client = _refusing_client()

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


async def test_a_device_report_reaches_the_entities(hass: HomeAssistant) -> None:
    """Ingesting a frame must notify listeners, not just update the mirror.

    Entities re-render from a coordinator listener. Updating `state` without
    firing them leaves every entity in Home Assistant showing stale values
    while the coordinator quietly knows better - invisible in any test that
    inspects `state` directly.
    """
    coordinator, _ = _connected_coordinator(hass)
    fired: list[int] = []
    remove = coordinator.async_add_listener(lambda: fired.append(1))

    coordinator._ingest(cbor.encode({KEY_POWER: True}))
    assert fired == [1]

    remove()
    coordinator._ingest(cbor.encode({KEY_POWER: False}))
    assert fired == [1]  # and a removed listener stops hearing about it


async def test_a_command_reaches_the_entities(hass: HomeAssistant) -> None:
    """A successful command notifies listeners too, on its optimistic echo."""
    coordinator, _ = _connected_coordinator(hass)
    fired: list[int] = []
    coordinator.async_add_listener(lambda: fired.append(1))

    await coordinator.async_set_power(True)
    assert fired == [1]


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
    coordinator, client = _connected_coordinator(hass)
    client.write_gatt_char = AsyncMock(side_effect=BleakError("Unlikely Error"))
    assert KEY_POWER not in coordinator.state  # nothing to match at failure time

    async def _relink(*, prime: bool = True) -> None:
        assert prime is False  # a command asks for a bare link
        client.is_connected = True
        coordinator._client = client

    coordinator._connect_locked = _relink

    async def _report_after_the_failure() -> None:
        await asyncio.sleep(0.05)
        coordinator._ingest(cbor.encode({KEY_POWER: True}))

    reporter = asyncio.create_task(_report_after_the_failure())
    try:
        await coordinator.async_set_power(True)  # must not raise
    finally:
        await reporter
    assert coordinator.state[KEY_POWER] is True


async def test_a_write_with_nothing_reportable_is_never_confirmed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A payload the device never reports back cannot vouch for itself.

    Confirmation compares against what the lamp reports, so a write carrying
    only keys outside STATE_KEYS has no evidence available either way, and
    silence must not be read as success.
    """
    coordinator, client = _connected_coordinator(hass)
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.05)
    client.write_gatt_char = AsyncMock(side_effect=BleakError("down"))

    async def _relink(*, prime: bool = True) -> None:
        assert prime is False
        client.is_connected = True
        coordinator._client = client

    coordinator._connect_locked = _relink

    with pytest.raises(HomeAssistantError):
        await coordinator._async_write({0x2C: b"\x02\xd0"})


async def test_a_background_connect_primes_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connect that primes says so, so the poll does not do it again.

    This drives the real _connect_locked: subscribe, read the device-info
    string, prime the state, and mark the link primed. Without the mark the
    poll re-interrogates the lamp every 30 s forever.
    """
    coordinator, client = _connected_coordinator(hass)
    coordinator._client = None
    client.is_connected = True
    client.start_notify = AsyncMock()
    client.read_gatt_char = AsyncMock(
        side_effect=[
            bytearray(b"brand:Glowrium;pkey:Glowrium-C051;version:4;;"),
            bytearray(cbor.encode({KEY_POWER: True, KEY_ACTIVATED: True})),
        ]
    )
    monkeypatch.setattr(
        coordinator_module, "establish_connection", AsyncMock(return_value=client)
    )

    def _in_range() -> object:
        return object()

    coordinator._ble_device = _in_range

    await coordinator._connect_locked()

    client.start_notify.assert_awaited_once()  # or no state ever arrives
    assert coordinator.model_id == "Glowrium-C051"
    assert coordinator.state[KEY_POWER] is True
    assert coordinator._primed_client is client

    reads = client.read_gatt_char.await_count
    coordinator._async_poll_reconnect(None)
    await hass.async_block_till_done()
    assert client.read_gatt_char.await_count == reads  # not primed twice


async def test_the_bring_up_is_attempted_once_per_session(
    hass: HomeAssistant,
) -> None:
    """Having settled the activation question, the lamp is not re-interrogated.

    The check costs up to 3 s waiting for 0x14, and it runs on every connect,
    so repeating it would put that on the command path for the whole session.
    """
    coordinator, client = _connected_coordinator(hass)
    coordinator.state[KEY_ACTIVATED] = True
    await coordinator._async_activate_if_needed()
    client.write_gatt_char.assert_not_awaited()

    # Settled. Even a later False must not restart the bring-up.
    coordinator.state[KEY_ACTIVATED] = False
    await coordinator._async_activate_if_needed()
    client.write_gatt_char.assert_not_awaited()


async def test_a_failed_reconnect_does_not_wedge_reconnection(
    hass: HomeAssistant,
) -> None:
    """One failed attempt must not stop the lamp being retried.

    The poll is the only thing that gets a dropped link back, and it refuses
    to start a second attempt while one is in flight. If a failure left that
    flag set, the lamp would never be reconnected again for the rest of the
    session - on this hardware failures are the normal case, not the rare one.
    """
    coordinator, _ = _connected_coordinator(hass)
    coordinator._client = None
    attempts: list[int] = []

    async def _fails() -> None:
        attempts.append(1)
        raise BleakError("not in range")

    coordinator._async_ensure_connected = _fails

    coordinator._async_poll_reconnect(None)
    await hass.async_block_till_done()
    assert attempts == [1]

    coordinator._async_poll_reconnect(None)
    await hass.async_block_till_done()
    assert attempts == [1, 1]  # and again, and again


async def test_advertisements_do_not_start_a_connect_storm(
    hass: HomeAssistant,
) -> None:
    """Only one connect at a time, however fast the lamp advertises.

    Advertisements arrive about once a second; starting a connect for each
    would pile them onto a device that allows exactly one connection.
    """
    coordinator, _ = _connected_coordinator(hass)
    coordinator._client = None
    attempts: list[int] = []
    release = asyncio.Event()

    async def _hangs() -> None:
        attempts.append(1)
        await release.wait()

    coordinator._async_ensure_connected = _hangs

    for _ in range(5):
        coordinator._async_on_advertisement(None, None)
    coordinator._async_poll_reconnect(None)  # the poll must not add one either
    await asyncio.sleep(0)
    assert attempts == [1]

    release.set()
    await hass.async_block_till_done()


async def test_stopping_tears_everything_down(hass: HomeAssistant) -> None:
    """Stopping cancels all three watchers and drops the link.

    Leaving any of them behind means a reload leaves the old coordinator
    reacting to advertisements and polling for reconnects alongside the new
    one, both competing for the lamp's single connection.
    """
    coordinator, client = _connected_coordinator(hass)
    client.disconnect = AsyncMock()
    cancels = {name: MagicMock() for name in ("bluetooth", "unavailable", "poll")}
    coordinator._cancel_bluetooth = cancels["bluetooth"]
    coordinator._cancel_unavailable = cancels["unavailable"]
    coordinator._cancel_poll = cancels["poll"]

    await coordinator.async_stop()

    for name, cancel in cancels.items():
        assert cancel.call_count == 1, f"{name} watcher was left running"
    client.disconnect.assert_awaited_once()
    assert coordinator._client is None


async def test_a_dropped_link_is_forgotten(hass: HomeAssistant) -> None:
    """The disconnect callback must clear the client, not just log.

    Everything downstream asks `_is_connected`, which trusts this: a stale
    client left in place looks connected, so the poll never reconnects and
    every command writes into a dead handle.
    """
    coordinator, client = _connected_coordinator(hass)
    assert coordinator._is_connected is True

    coordinator._async_on_disconnect(client)

    assert coordinator._client is None
    assert coordinator._is_connected is False


async def test_a_write_without_a_link_is_refused_not_dropped(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing with no client must raise, so the retry and the user hear about it.

    Returning quietly would make every command look like it succeeded while
    nothing reached the lamp.
    """
    coordinator, _ = _connected_coordinator(hass)
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.01)
    coordinator._client = None

    with pytest.raises(BleakError):
        await coordinator._write_raw({KEY_POWER: True})


async def test_starting_watches_for_the_device(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start registers both bluetooth watchers and seeds presence.

    Without the advertisement callback the lamp is never noticed coming back;
    without the unavailable tracker its entities never go unavailable when it
    is unplugged.
    """
    coordinator, _ = _connected_coordinator(hass)
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
    coordinator._async_ensure_connected = AsyncMock()
    coordinator._client = None  # this is about the watchers, not the link

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


async def test_switches_send_the_state_they_were_given(hass: HomeAssistant) -> None:
    """Both polarities, for every switch. A hardcoded True is still "working".

    Each of these was only ever exercised in one direction, so a command that
    ignored its argument and always turned the thing on looked correct.
    """
    coordinator, client = _connected_coordinator(hass)

    await coordinator.async_set_indicator(False)
    assert (
        cbor.decode(client.write_gatt_char.await_args.args[1])[KEY_INDICATOR] is False
    )

    await coordinator.async_set_dst(False)
    assert cbor.decode(client.write_gatt_char.await_args.args[1])[KEY_DST] == DST_OFF

    await coordinator.async_set_power(False)
    assert cbor.decode(client.write_gatt_char.await_args.args[1])[KEY_POWER] is False


async def test_brightness_is_clamped_at_both_ends(hass: HomeAssistant) -> None:
    """Only the upper clamp was pinned; a missing lower one sends a negative."""
    coordinator, client = _connected_coordinator(hass)
    await coordinator.async_set_brightness(-20)
    assert cbor.decode(client.write_gatt_char.await_args.args[1])[KEY_BRIGHTNESS] == 0


async def test_every_operating_mode_sets_both_flags(hass: HomeAssistant) -> None:
    """The two flags are mutually exclusive, so each mode must write both.

    Only Circadian was covered. A Manual that wrote nothing, or a Schedule
    that set circadian, would have left the lamp in the wrong mode silently.
    """
    for mode, circadian, schedule in (
        ("manual", False, False),
        ("schedule", False, True),
        ("circadian", True, False),
    ):
        coordinator, client = _connected_coordinator(hass)
        await coordinator.async_set_operating_mode(mode)
        written: dict[int, object] = {}
        for call in client.write_gatt_char.await_args_list:
            written.update(cbor.decode(call.args[1]))
        assert written[KEY_CIRCADIAN] is circadian, mode
        assert written[KEY_SCHEDULE] is schedule, mode


async def test_each_schedule_setter_changes_its_own_field(
    hass: HomeAssistant,
) -> None:
    """A setter that wrote nothing at all would pass a test of its neighbour."""
    slot = bytes.fromhex("000300fe091111115a0102")
    for setter, args, index, expected in (
        ("async_set_timer_end", (19, 45), TIMER_END_H, 19),
        ("async_set_timer_brightness", (37,), TIMER_BRIGHTNESS, 37),
    ):
        coordinator, client = _connected_coordinator(hass)
        coordinator.state[KEY_TIMER] = slot
        await getattr(coordinator, setter)(*args)
        written = cbor.decode(client.write_gatt_char.await_args.args[1])[KEY_TIMER]
        assert written[index] == expected, setter
        assert written != slot, setter


async def test_the_remembered_ramp_survives_the_device_reporting(
    hass: HomeAssistant,
) -> None:
    """The user's ramp is remembered, and later reports must not overwrite it.

    The device resets its ramp when circadian is re-enabled, which is why it is
    remembered at all - so re-seeding it from every report would hand back
    exactly the value the memory exists to override.
    """
    coordinator, client = _connected_coordinator(hass)
    coordinator.state[KEY_LIGHTING_MODE] = 1
    await coordinator.async_set_ramp(90)  # 5400 s = 0x1518
    assert coordinator._desired_ramp == bytes.fromhex("1518")

    coordinator._ingest(
        cbor.encode({KEY_RAMP: bytes.fromhex("0e10")})
    )  # device default
    assert coordinator._desired_ramp == bytes.fromhex("1518")  # still the user's

    client.write_gatt_char.reset_mock()
    await coordinator.async_set_lighting_mode(5)
    sent = cbor.decode(client.write_gatt_char.await_args.args[1])
    assert sent[KEY_RAMP] == bytes.fromhex("1518")  # and it is what gets re-applied


async def test_a_stale_mirror_does_not_vouch_for_a_failed_write(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirmation needs a fresh report, not a matching one.

    The mirror is never invalidated - a disconnect clears the client, not the
    state - so it can be hours old. Asking a lamp to turn off while the stale
    mirror already says `off` would otherwise report success for a write that
    failed, leaving the lamp on and removing the only signal the user had that
    it is unreachable.
    """
    coordinator, client = _connected_coordinator(hass)
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.05)
    coordinator.state[KEY_POWER] = False  # what the lamp said, some time ago
    client.write_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))

    async def _relink(*, prime: bool = True) -> None:
        assert prime is False
        coordinator._client = client

    coordinator._connect_locked = _relink

    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_power(False)


async def test_a_command_that_never_reached_the_wire_fails_at_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An out-of-range lamp fails immediately; there is nothing to wait for.

    `_ble_device` returns None without any I/O, so no byte ever left. Waiting
    the grace window for a notification that cannot arrive - there is no link -
    added two seconds to every command an automation sends to a lamp that is
    off or out of range.
    """
    coordinator, _ = _connected_coordinator(hass)
    coordinator._client = None
    coordinator.state[KEY_POWER] = True  # and the mirror happens to agree
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 30.0)

    async def _out_of_range(*, prime: bool = True) -> None:
        raise BleakError("AA:BB:CC:DD:EE:FF is not in range")

    coordinator._connect_locked = _out_of_range

    async with asyncio.timeout(1):  # nowhere near the grace window
        with pytest.raises(HomeAssistantError):
            await coordinator.async_set_power(True)


async def test_an_old_client_disconnecting_does_not_drop_the_live_one(
    hass: HomeAssistant,
) -> None:
    """The disconnect callback must check WHICH client it is being told about.

    A failed write drops its client and the retry establishes another. When
    the OS later notices the first one is gone, bleak fires that client's
    callback - and clearing `_client` unconditionally there discards the live
    connection instead. `_is_connected` then reads False, so the poll opens
    yet another link to a lamp with a single slot, and every attempt fails
    with "out of connection slots" while the working connection sits there
    unreferenced until the lamp's own churn drops it.
    """
    coordinator, live = _connected_coordinator(hass)
    superseded = MagicMock()  # the client an earlier attempt gave up on
    superseded.disconnect = AsyncMock()

    coordinator._async_on_disconnect(superseded)

    assert coordinator._client is live
    assert coordinator._is_connected is True
    live.disconnect.assert_not_awaited()
    superseded.disconnect.assert_not_awaited()

    # The live one going down is still heard.
    coordinator._async_on_disconnect(live)
    assert coordinator._client is None


async def test_a_connect_that_fails_half_way_leaves_no_link_behind(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connect that cannot finish must hang up, not abandon the link.

    The client is established before notifications are subscribed. If that
    subscription fails - or the connect is cancelled by its deadline at that
    moment - walking away leaves a connected client holding the lamp's single
    slot with nothing referencing it: every later attempt then fails for want
    of a slot until the lamp's own churn drops it.
    """
    coordinator, client = _connected_coordinator(hass)
    coordinator._client = None
    client.start_notify = AsyncMock(side_effect=BleakError("subscribe failed"))
    client.disconnect = AsyncMock()
    monkeypatch.setattr(
        coordinator_module, "establish_connection", AsyncMock(return_value=client)
    )

    def _in_range() -> object:
        return object()

    coordinator._ble_device = _in_range

    with pytest.raises(BleakError):
        await coordinator._connect_locked()

    client.disconnect.assert_awaited_once()
    assert coordinator._client is None  # and nothing is left claiming to be live


async def test_stopping_hangs_up_even_when_the_lock_is_busy(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy lock must not mean the connection is simply abandoned.

    Dropping the reference does not close a BLE link - bleak has no disconnect
    on garbage collection - so the lamp's one slot stays taken and the next
    coordinator cannot have it.
    """
    coordinator, client = _connected_coordinator(hass)
    monkeypatch.setattr(coordinator_module, "_STOP_TIMEOUT", 0.05)
    client.disconnect = AsyncMock()

    await coordinator._lock.acquire()  # something else is mid-connect
    try:
        await coordinator.async_stop()
    finally:
        coordinator._lock.release()

    client.disconnect.assert_awaited()  # hung up anyway
    assert coordinator._client is None


async def test_background_work_is_tied_to_the_entry(hass: HomeAssistant) -> None:
    """Reconnects and priming must die with the entry, like the first connect.

    Tasks created on hass are only awaited at shutdown, so on a reload one
    outlives its coordinator, finishes connecting, and claims the lamp's only
    slot for a coordinator nobody owns - while the replacement cannot connect.
    """
    coordinator, _ = _connected_coordinator(hass)
    coordinator._client = None
    spawned: list[str] = []

    def _background(_hass: object, coro: object, name: str) -> object:
        coro.close()
        spawned.append(name)
        return MagicMock()

    entry = MagicMock()
    entry.async_create_background_task = _background
    coordinator._entry = entry

    coordinator._async_poll_reconnect(None)
    assert len(spawned) == 1  # the reconnect went to the entry, not to hass

    coordinator._client = MagicMock(is_connected=True)
    coordinator._async_poll_reconnect(None)
    assert len(spawned) == 2  # and so does the priming


def test_no_path_holds_the_lock_longer_than_a_command_will_wait() -> None:
    """The timing constants have to make sense relative to each other.

    Every test that exercises a timeout patches it, so the shipped numbers are
    otherwise unconstrained - and they interact. A background connect holds the
    connection lock; a command waits for that same lock inside its own budget.
    If the holder is allowed longer than the waiter, pressing a switch during a
    background connect reports failure on a perfectly reachable lamp, having
    attempted nothing at all.
    """
    connect = coordinator_module._CONNECT_TIMEOUT
    command = coordinator_module._COMMAND_TIMEOUT
    hang_up = coordinator_module._HANG_UP_TIMEOUT
    poll = coordinator_module._RECONNECT_INTERVAL.total_seconds()

    assert connect < command, "a lock holder outlasting the waiter is an inversion"
    assert connect >= coordinator_module._STOP_TIMEOUT
    # Priming is spawned from the poll and takes the same lock, so it must be
    # finished before the next tick or the ticks pile up on top of each other.
    assert connect < poll
    # A write retry waits for the hang-up of the client it gave up on, inside
    # the command's budget and before it dials. A hang-up allowed as long as
    # the command leaves the retry no time to happen in exactly the case it is
    # for: a link that will not confirm it has closed.
    assert hang_up < command


async def test_stopping_hangs_up_once_not_twice(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disconnect that hangs costs one ceiling, not two.

    Measured on the real integration: reloading took 6.1 s, all of it in
    unload, because a first bounded disconnect timed out and a fallback then
    timed out again. The lock and the hang-up are separate problems and need
    separate deadlines - the lock is best-effort, the disconnect is tried once.
    """
    coordinator, client = _connected_coordinator(hass)
    monkeypatch.setattr(coordinator_module, "_STOP_TIMEOUT", 0.05)
    attempts: list[int] = []

    async def _hangs() -> None:
        attempts.append(1)
        await asyncio.Event().wait()

    client.disconnect = _hangs

    async with asyncio.timeout(0.4):  # comfortably under two ceilings plus slack
        await coordinator.async_stop()

    assert attempts == [1]
    assert coordinator._client is None


async def test_a_prime_that_got_nothing_does_not_count_as_primed(
    hass: HomeAssistant,
) -> None:
    """Priming only counts when the lamp actually answered.

    Marking the link primed regardless meant one failed attempt stopped the
    poll ever trying again. Seen on real hardware: establish_connection
    returned a client whose every operation answered "Not connected" while
    still reporting itself connected, so the poll saw no reason to reconnect
    and no reason to prime, and the entities sat at one of fourteen
    indefinitely.
    """
    coordinator, client = _connected_coordinator(hass)
    client.read_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))
    client.write_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))

    await coordinator._async_prime()

    assert coordinator._primed_client is not client


async def test_a_link_that_answers_nothing_is_dropped(hass: HomeAssistant) -> None:
    """A link that cannot even be read is not a working link.

    bleak can report a client as connected while BlueZ answers "Not connected"
    to everything. Keeping it means `_is_connected` stays True, so the poll
    never reconnects and the coordinator is wedged until the device's own
    churn. Dropping it lets the poll do its job.
    """
    coordinator, client = _connected_coordinator(hass)
    client.read_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))
    client.write_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))

    await coordinator._async_prime()

    assert coordinator._client is None
    assert coordinator._is_connected is False


async def test_a_connect_whose_read_fails_is_not_primed_either(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule holds on the connect path too, not just when the poll primes.

    A link established but never read from is the same wedged link either way;
    marking it primed here would stop the poll going back for the properties
    just as surely.
    """
    coordinator, client = _connected_coordinator(hass)
    coordinator._client = None
    client.is_connected = True
    client.start_notify = AsyncMock()
    client.read_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))
    client.write_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))
    monkeypatch.setattr(
        coordinator_module, "establish_connection", AsyncMock(return_value=client)
    )

    def _in_range() -> object:
        return object()

    coordinator._ble_device = _in_range

    await coordinator._connect_locked()

    assert coordinator._primed_client is not client


async def test_a_connect_that_cannot_be_read_is_dropped_at_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead link is dropped where it is detected, not a poll tick later.

    Observed live: the connect noticed the lamp answered nothing and kept the
    client anyway, so for the next thirty seconds `_is_connected` was True over
    a link that served nothing - and a command in that window wrote into it
    before failing and reconnecting. The poll rebuilds it either way; there is
    no reason to hold it in the meantime.
    """
    coordinator, client = _connected_coordinator(hass)
    coordinator._client = None
    client.is_connected = True
    client.start_notify = AsyncMock()
    client.disconnect = AsyncMock()
    client.read_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))
    client.write_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))
    monkeypatch.setattr(
        coordinator_module, "establish_connection", AsyncMock(return_value=client)
    )

    def _in_range() -> object:
        return object()

    coordinator._ble_device = _in_range

    await coordinator._connect_locked()

    assert coordinator._client is None
    assert coordinator._is_connected is False


def _refusing_client() -> MagicMock:
    """Return a client whose read works but which rejects the request.

    That is the shape of a model that refuses: issue #3 shows a G8 serving a
    twenty-key read while answering the request with an ATT error and dropping
    the link.
    """
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(
        return_value=bytearray(cbor.encode({KEY_POWER: True}))
    )
    client.write_gatt_char = AsyncMock(
        side_effect=BleakError("Insufficient authorization (8)")
    )
    return client


async def test_a_dead_link_never_counts_as_a_refusal(hass: HomeAssistant) -> None:
    """Only a device that answered can be said to have refused.

    A weak link fails the read and the request alike, and counting that muted
    the request on a perfectly good lamp that merely sat far from the adapter -
    seen on a real G7 forty seconds after start-up. A refusal is when the read
    worked and the request did not: the link was demonstrably alive in between.
    """
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))
    client.write_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))

    for _ in range(coordinator_module._STATE_REQUEST_ATTEMPTS * 3):
        await coordinator._request_state(client)

    assert coordinator._state_request_muted is False
    assert coordinator._state_request_failures == 0


async def test_a_model_that_keeps_refusing_is_left_alone_for_the_session(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second run of refusals after the cooldown ends the asking for good.

    The cooldown exists to give a first run the benefit of the doubt. A model
    that refuses again once it expires is refusing, not unlucky - and each
    round costs it a dropped link, which is what #5 set out to stop.
    """
    monkeypatch.setattr(coordinator_module, "_STATE_REQUEST_COOLDOWN", 0.05)
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G8")
    client = _refusing_client()

    for _ in range(coordinator_module._STATE_REQUEST_ATTEMPTS):
        await coordinator._request_state(client)
    assert coordinator._state_request_muted is True

    await asyncio.sleep(0.06)  # the cooldown expires
    assert coordinator._state_request_muted is False
    for _ in range(coordinator_module._STATE_REQUEST_ATTEMPTS):
        await coordinator._request_state(client)

    sent = client.write_gatt_char.await_count
    await asyncio.sleep(0.06)
    await coordinator._request_state(client)
    assert client.write_gatt_char.await_count == sent  # never again this session


async def test_a_link_that_dies_after_the_read_is_not_a_refusal(
    hass: HomeAssistant,
) -> None:
    """A read can succeed and the link still die before the request goes out.

    Seen on a real G7 in 0.2.0: the read answered - the low property block
    arrived and six entities came alive - and the request then failed with
    "Not connected" three times running, because the link dropped in between.
    Treating a successful read as proof the device is present muted the request
    on a perfectly good lamp, so the indicator, lighting mode, ramp and DST
    never arrived while commands kept working. Whether the device refused is
    told by the link being alive *after* the failure, not before it.
    """
    coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(
        return_value=bytearray(cbor.encode({KEY_POWER: True}))
    )

    async def _write_then_the_link_dies(*_a: object, **_kw: object) -> None:
        client.is_connected = False  # the drop is why the write failed
        raise BleakError("[org.bluez.Error.Failed] Not connected")

    client.write_gatt_char = AsyncMock(side_effect=_write_then_the_link_dies)

    for _ in range(coordinator_module._STATE_REQUEST_ATTEMPTS * 2):
        client.is_connected = True
        await coordinator._request_state(client)

    assert coordinator._state_request_muted is False
    assert coordinator._state_request_failures == 0


async def test_only_an_application_level_refusal_silences_the_request(
    hass: HomeAssistant,
) -> None:
    """Silence the request on a refusal, never on anything merely unrecognised.

    Two attempts to tell the cases apart failed on real hardware. A successful
    read does not prove the device is there - the link drops between the read
    and the write. Nor does `is_connected`: measured on a G7, it still reported
    True at the moment the write failed with "not connected", and the
    disconnect callback arrived two seconds later.

    So the test is inverted. Only an error that positively looks like the
    device answering "no" - an authorization or ATT protocol error - counts.
    Everything else is treated as the link, because muting a working lamp costs
    it four properties silently, while asking an exotic device once too often
    costs a reconnect.
    """
    cases = [
        ("Insufficient authorization (8)", True),
        ("[org.bluez.Error.Failed] Not connected", False),
        ("GATT Protocol Error: Unlikely Error", False),
        ("something nobody has seen before", False),
    ]
    for message, should_mute in cases:
        coordinator = GlowriumCoordinator(hass, "AA:BB:CC:DD:EE:FF", "Glowrium-G7")
        client = MagicMock()
        client.is_connected = True
        client.read_gatt_char = AsyncMock(
            return_value=bytearray(cbor.encode({KEY_POWER: True}))
        )
        client.write_gatt_char = AsyncMock(side_effect=BleakError(message))

        for _ in range(coordinator_module._STATE_REQUEST_ATTEMPTS):
            await coordinator._request_state(client)

        assert coordinator._state_request_muted is should_mute, message


async def test_the_request_is_repeated_on_every_connect(hass: HomeAssistant) -> None:
    """Coverage is judged by this connect's read, not by what we already knew.

    Measured on real hardware: the connect-time read never carries the
    indicator, lighting mode, ramp or DST, so once a request has filled those
    in, a check against the accumulated mirror is satisfied for ever and the
    request is never sent again. Anything the user then changes from the vendor
    app while Home Assistant is disconnected stays invisible until a restart -
    which is the opposite of what a reconnect is for.
    """
    coordinator, client = _connected_coordinator(hass)
    low_keys_only = cbor.encode({KEY_POWER: True, KEY_BRIGHTNESS: 50})
    client.read_gatt_char = AsyncMock(return_value=bytearray(low_keys_only))
    client.write_gatt_char = AsyncMock()

    await coordinator._request_state(client)
    assert client.write_gatt_char.await_count == 1

    # Pretend the request was answered: every key is now known.
    coordinator.state.update(dict.fromkeys(STATE_KEYS, 0))

    await coordinator._request_state(client)
    assert client.write_gatt_char.await_count == 2, (
        "a later connect must ask again - the read still lacks four keys"
    )


async def test_a_read_that_covers_everything_still_skips_the_request(
    hass: HomeAssistant,
) -> None:
    """A model whose read does carry every key is not asked needlessly."""
    coordinator, client = _connected_coordinator(hass)
    everything = cbor.encode(dict.fromkeys(STATE_KEYS, 0))
    client.read_gatt_char = AsyncMock(return_value=bytearray(everything))
    client.write_gatt_char = AsyncMock()

    await coordinator._request_state(client)
    await coordinator._request_state(client)

    client.write_gatt_char.assert_not_awaited()


async def test_a_stale_device_clock_is_corrected(hass: HomeAssistant) -> None:
    """A lamp whose clock has drifted is put right on connect.

    The clock was only ever written during first-time bring-up, so a lamp set
    up months ago runs its schedule and its circadian curve off whatever date
    it had then - one reporter's was six months out (issue #4). Nothing
    surfaces it either, because the clock is not an entity.
    """
    coordinator, client = _connected_coordinator(hass)
    stale = bytes.fromhex("07ea02010f0e2c")  # 2026-02-01 15:14:44
    coordinator.state[KEY_TIME] = stale

    await coordinator._async_sync_clock_if_needed()

    written = cbor.decode(client.write_gatt_char.await_args.args[1])
    assert written[KEY_TIME] != stale
    assert written[KEY_TIME_SYNCED] == 1
    year = (written[KEY_TIME][0] << 8) | written[KEY_TIME][1]
    assert year == dt_util.now().year


async def test_a_clock_that_is_near_enough_is_left_alone(
    hass: HomeAssistant,
) -> None:
    """Writing on every connect would cost a write an hour for nothing.

    The lamp reconnects itself every half hour or so; correcting a clock that
    is seconds out would mean a write each time, on a link that is the scarce
    resource here.
    """
    coordinator, client = _connected_coordinator(hass)
    coordinator.state[KEY_TIME] = _encode_device_time()

    await coordinator._async_sync_clock_if_needed()

    client.write_gatt_char.assert_not_awaited()


async def test_an_unreadable_clock_is_not_corrected_blind(
    hass: HomeAssistant,
) -> None:
    """With nothing read back, there is no drift to judge and nothing to fix."""
    coordinator, client = _connected_coordinator(hass)
    assert KEY_TIME not in coordinator.state

    await coordinator._async_sync_clock_if_needed()

    client.write_gatt_char.assert_not_awaited()


def _in_range() -> object:
    """Stand in for a device the adapter can see."""
    return object()


async def test_both_priming_paths_check_the_clock(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A background connect corrects the clock too, not only the poll's priming.

    Most connects on a healthy lamp are background reconnects; if only the poll
    checked, a lamp whose link is good enough never to need re-priming would
    keep a stale clock for ever.
    """
    for path in ("_connect_locked", "_async_prime"):
        coordinator, client = _connected_coordinator(hass)
        checked = 0

        async def _note() -> None:
            nonlocal checked
            checked += 1

        coordinator._async_sync_clock_if_needed = _note
        coordinator._request_state = AsyncMock(return_value=True)
        coordinator._async_activate_if_needed = AsyncMock()

        if path == "_connect_locked":
            coordinator._client = None  # a real connect, not the early return
            client.is_connected = True
            client.start_notify = AsyncMock()
            client.read_gatt_char = AsyncMock(return_value=bytearray(b"brand:x;;"))
            monkeypatch.setattr(
                coordinator_module,
                "establish_connection",
                AsyncMock(return_value=client),
            )
            coordinator._ble_device = _in_range
            await coordinator._connect_locked()
        else:
            await coordinator._async_prime()

        assert checked == 1, path


async def test_the_dst_offset_the_lamp_reports_is_preserved(
    hass: HomeAssistant,
) -> None:
    """Toggling DST must not overwrite the offset with a hardcoded hour.

    The 0x35 slot is a flag plus an offset, written together. Sending a fixed
    3600 seconds turns half-hour daylight-saving regions - Lord Howe Island,
    and historically others - into a full hour the moment the switch is
    touched, and the lamp had been reporting the right value all along
    (issue #4).
    """
    coordinator, client = _connected_coordinator(hass)
    half_hour = bytes.fromhex("0000000708")  # flag off, offset 1800 s
    coordinator.state[KEY_DST] = half_hour

    await coordinator.async_set_dst(True)

    written = cbor.decode(client.write_gatt_char.await_args.args[1])[KEY_DST]
    assert written[0] == 1  # the flag we asked for
    assert written[1:] == half_hour[1:], "the lamp's own offset was overwritten"


async def test_dst_falls_back_to_an_hour_when_unread(hass: HomeAssistant) -> None:
    """With nothing reported yet, the near-universal hour is the sane default.

    Unlike the schedule slot, this carries one field rather than five, and
    refusing would leave the switch unusable until the lamp reports - so a
    default is the better trade here.
    """
    coordinator, client = _connected_coordinator(hass)
    assert KEY_DST not in coordinator.state

    await coordinator.async_set_dst(True)

    assert cbor.decode(client.write_gatt_char.await_args.args[1])[KEY_DST] == DST_ON


def _dialling(
    coordinator: GlowriumCoordinator,
    monkeypatch: pytest.MonkeyPatch,
    *clients: MagicMock,
) -> AsyncMock:
    """Make the coordinator's next connects hand back ``clients``, in order."""
    dial = AsyncMock(side_effect=list(clients))
    monkeypatch.setattr(coordinator_module, "establish_connection", dial)

    def _in_range() -> object:
        return object()

    coordinator._ble_device = _in_range
    return dial


def _fresh_client() -> MagicMock:
    """Return a client as establish_connection hands one back: up, but unread."""
    client = MagicMock()
    client.is_connected = True
    client.start_notify = AsyncMock()
    client.disconnect = AsyncMock()
    client.read_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))
    client.write_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))
    return client


async def test_a_link_the_poll_gives_up_on_is_hung_up(hass: HomeAssistant) -> None:
    """Forgetting a client is not disconnecting it.

    Found on the real integration: the system bus refused every new connection
    from Home Assistant's user, Bluetooth included, about two and a half hours
    after each start. bleak opens a D-Bus connection per client and closes it
    only in ``disconnect()``; a link that answered nothing was "dropped" by
    clearing the reference, which closes nothing, and every client let go of
    that way kept its connection until the bus's limit of 256 per user was
    reached.
    """
    coordinator, client = _connected_coordinator(hass)
    client.read_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))
    client.write_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))

    await coordinator._async_prime()
    await hass.async_block_till_done()

    assert coordinator._client is None
    client.disconnect.assert_awaited_once()


async def test_a_connect_that_cannot_be_read_is_hung_up(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same on the connect path, which is the one the poll takes every tick."""
    coordinator, _ = _connected_coordinator(hass)
    coordinator._client = None
    client = _fresh_client()
    _dialling(coordinator, monkeypatch, client)

    await coordinator._connect_locked()
    await hass.async_block_till_done()

    assert coordinator._client is None
    client.disconnect.assert_awaited_once()


async def test_a_write_retry_hangs_up_before_it_dials_again(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client a write failed on is closed, and closed before the retry.

    Order matters as much as the hang-up. The lamp has one slot: a connect made
    while the old link is still up is handed that same link, and the hang-up
    then closes it underneath the retry.
    """
    coordinator, first = _connected_coordinator(hass)
    first.write_gatt_char = AsyncMock(side_effect=BleakError("dropped"))
    second = _fresh_client()
    second.write_gatt_char = AsyncMock()
    order: list[str] = []

    async def _hang_up_slowly() -> None:
        await asyncio.sleep(0.01)  # a real disconnect is not instant either
        order.append("hung up")

    async def _dial(*_a: object, **_kw: object) -> MagicMock:
        order.append("dialled")
        return second

    first.disconnect = AsyncMock(side_effect=_hang_up_slowly)
    _dialling(coordinator, monkeypatch).side_effect = _dial

    await coordinator.async_set_power(True)

    assert order == ["hung up", "dialled"]
    first.disconnect.assert_awaited_once()
    assert coordinator._client is second
    second.disconnect.assert_not_awaited()  # the link that worked is kept


async def test_a_failed_command_hangs_up_only_after_the_device_could_confirm(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last client is closed too - but not before confirmation has listened.

    A client whose write failed is still the channel the confirming report
    arrives on (see test_confirmation_waits_for_a_report_that_arrives_late).
    Hanging it up the moment the write fails would close the leak and quietly
    take that away: every command whose acknowledgement was lost would be
    reported as failed again.

    So the lamp here does what that window exists for. It acts on the write,
    the acknowledgement is lost, and its report arrives a moment after the
    failure - on the link the write failed on, and only if that link is still
    up: a client that has been hung up delivers nothing.
    """
    coordinator, first = _connected_coordinator(hass)
    first.write_gatt_char = AsyncMock(side_effect=BleakError("Unlikely Error"))
    second = _fresh_client()
    _dialling(coordinator, monkeypatch, second)
    still_up_when_reporting: list[bool] = []

    def _report_if_still_connected() -> None:
        still_up = second.disconnect.await_count == 0
        still_up_when_reporting.append(still_up)
        if still_up:
            notify = second.start_notify.await_args.args[1]
            notify(None, bytearray(cbor.encode({KEY_POWER: True})))

    async def _acted_on_but_unacknowledged(*_a: object, **_kw: object) -> None:
        hass.loop.call_later(0.05, _report_if_still_connected)
        raise BleakError("GATT Protocol Error: Unlikely Error")

    second.write_gatt_char = AsyncMock(side_effect=_acted_on_but_unacknowledged)

    await coordinator.async_set_power(True)  # confirmed by the late report
    await hass.async_block_till_done()

    assert still_up_when_reporting == [True]  # up while the device could answer
    assert coordinator.state[KEY_POWER] is True
    first.disconnect.assert_awaited_once()
    second.disconnect.assert_awaited_once()  # ...and hung up once it had
    assert coordinator._client is None


async def test_a_confirmed_command_still_hangs_up_the_client_it_gave_up_on(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Being told the command worked is no reason to keep the leak."""
    coordinator, first = _connected_coordinator(hass)
    second = _fresh_client()

    async def _write_then_notify(*_a: object, **_kw: object) -> None:
        coordinator._ingest(cbor.encode({KEY_POWER: True}))
        raise BleakError("GATT Protocol Error: Unlikely Error")

    first.write_gatt_char = AsyncMock(side_effect=_write_then_notify)
    second.write_gatt_char = AsyncMock(side_effect=_write_then_notify)
    _dialling(coordinator, monkeypatch, second)

    await coordinator.async_set_power(True)  # confirmed by the report
    await hass.async_block_till_done()

    first.disconnect.assert_awaited_once()
    second.disconnect.assert_awaited_once()


async def test_a_link_the_lamp_dropped_is_hung_up_as_well(hass: HomeAssistant) -> None:
    """The link going down by itself closes the link, not the client.

    bleak leaves the client's D-Bus connection open after the device
    disconnects; only ``disconnect()`` releases it. This was where the quota
    actually went. On the lamp it was found on, at the edge of range, the link
    came up on every poll tick and the lamp dropped it two to ten seconds
    later - 678 times in one night - and each time the callback only cleared
    the reference.
    """
    coordinator, client = _connected_coordinator(hass)

    coordinator._async_on_disconnect(client)
    await hass.async_block_till_done()

    assert coordinator._client is None
    client.disconnect.assert_awaited_once()


async def test_a_client_that_is_not_ours_is_left_to_bleak(
    hass: HomeAssistant,
) -> None:
    """Only the client the coordinator holds is hung up from the callback.

    The first build of this fix hung up whichever client the callback named, as
    a second chance for a hang-up cut short by its ceiling. On the real lamp it
    lasted minutes: bleak reports a link lost in the middle of a connect to the
    same callback, while establish_connection is still working on that client.
    Disconnecting it there closed the bus underneath bleak's own clean-up -
    "Failed to cancel connection ... Bad file descriptor" on every such drop,
    and a retry that died on a bus that was no longer there.
    """
    coordinator, live = _connected_coordinator(hass)
    connecting = MagicMock()  # still inside establish_connection
    connecting.disconnect = AsyncMock()

    coordinator._async_on_disconnect(connecting)
    await hass.async_block_till_done()

    connecting.disconnect.assert_not_awaited()
    assert coordinator._client is live


async def test_a_hang_up_outlives_the_deadline_of_whoever_asked_for_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command running out of time must not take the hang-up with it.

    A disconnect that is cancelled part-way has asked BlueZ to drop the link
    and then walked away before closing its own D-Bus connection - the leak
    again, by another route.
    """
    coordinator, first = _connected_coordinator(hass)
    monkeypatch.setattr(coordinator_module, "_COMMAND_TIMEOUT", 0.05)
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.01)
    first.write_gatt_char = AsyncMock(side_effect=BleakError("dropped"))
    released = asyncio.Event()
    finished: list[int] = []

    async def _slow_hang_up() -> None:
        await released.wait()
        finished.append(1)

    first.disconnect = AsyncMock(side_effect=_slow_hang_up)
    dial = _dialling(coordinator, monkeypatch, _fresh_client())

    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_power(True)  # the deadline ends the wait

    assert finished == []
    dial.assert_not_awaited()  # and it never dialled over the old link
    released.set()
    await hass.async_block_till_done()
    assert finished == [1]  # the hang-up itself ran to the end


async def test_a_hang_up_that_fails_or_hangs_troubles_nobody(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hanging up is cleanup: it is bounded, and its failures stay in the log.

    bleak's disconnect passes on whatever the bus raised and ends on an
    assertion, and against a wedged BlueZ it can wait indefinitely. None of
    that may reach the path that gave the link up, or outlive the test - and
    none of it may vanish either: the log line is the only trace a hang-up
    that left its bus open will ever leave.
    """
    monkeypatch.setattr(coordinator_module, "_HANG_UP_TIMEOUT", 0.05)
    caplog.set_level(logging.DEBUG, logger=coordinator_module.__name__)
    for failure in (BleakError("gone"), OSError(9, "Bad file descriptor"), EOFError()):
        coordinator, client = _connected_coordinator(hass)
        client.disconnect = AsyncMock(side_effect=failure)
        caplog.clear()

        await coordinator._hang_up(client)  # the failure does not come out

        client.disconnect.assert_awaited_once()
        assert coordinator._client is None
        assert f"failed: {failure!r}" in caplog.text

    coordinator, client = _connected_coordinator(hass)

    async def _never() -> None:
        await asyncio.Event().wait()

    client.disconnect = AsyncMock(side_effect=_never)
    caplog.clear()
    async with asyncio.timeout(1):
        await coordinator._hang_up(client)  # ends at the ceiling, not never
    assert coordinator._client is None
    assert "failed: TimeoutError()" in caplog.text


async def test_a_hang_up_is_not_tied_to_the_entry(hass: HomeAssistant) -> None:
    """The one background task that must survive the entry being unloaded.

    Everything else is created on the entry so that it dies with it (see
    test_background_work_is_tied_to_the_entry): a connect that outlives its
    coordinator claims the lamp's slot for nobody. A hang-up is the opposite -
    cancelled by an unload, it leaves the slot taken and the D-Bus connection
    open.
    """
    coordinator, client = _connected_coordinator(hass)
    entry = MagicMock()
    coordinator._entry = entry

    coordinator._hang_up(client)
    await hass.async_block_till_done()

    entry.async_create_background_task.assert_not_called()
    client.disconnect.assert_awaited_once()


async def test_hanging_up_does_not_need_home_assistant() -> None:
    """The bench drives this coordinator with no Home Assistant behind it.

    tools/bench.py builds the real coordinator with ``hass=None`` and takes the
    paths the integration takes. With the hang-up scheduled on hass, every one
    that lets go of a client - a link the lamp drops, a connect that answers
    nothing, a write that needs its retry - ended there in "'NoneType' object
    has no attribute 'async_create_task'", with the client still connected.
    Found by walking those three paths on a coordinator built the way the bench
    builds it; 0.2.1 took all three.
    """
    coordinator = GlowriumCoordinator(None, "AA:BB:CC:DD:EE:FF", "bench")
    client = _fresh_client()
    coordinator._client = client

    coordinator._async_on_disconnect(client)  # the lamp drops the link
    await asyncio.sleep(0)  # nothing to block on without hass; one turn does it

    assert coordinator._client is None
    client.disconnect.assert_awaited_once()


async def test_a_retry_hangs_up_without_home_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same for the path that waits for the hang-up before it dials again.

    This is the one the bench exists to exercise: a command on a link that
    drops under it.
    """
    coordinator = GlowriumCoordinator(None, "AA:BB:CC:DD:EE:FF", "bench")
    first = _fresh_client()
    first.write_gatt_char = AsyncMock(side_effect=BleakError("dropped"))
    coordinator._client = first
    second = _fresh_client()
    second.write_gatt_char = AsyncMock()
    _dialling(coordinator, monkeypatch, second)

    await coordinator.async_set_power(True)

    first.disconnect.assert_awaited_once()
    assert coordinator._client is second


async def test_home_assistant_waits_for_a_hang_up_in_flight(
    hass: HomeAssistant,
) -> None:
    """With Home Assistant behind it, the hang-up is hass's task to see through.

    The standalone path above keeps its own task; this is the other side of
    that choice. A task hass does not track is one async_block_till_done walks
    straight past, and that call is how Home Assistant - and every test here -
    lets pending work settle before it looks at the result.
    """
    coordinator, client = _connected_coordinator(hass)
    finished: list[int] = []

    async def _takes_a_moment() -> None:
        await asyncio.sleep(0.05)
        finished.append(1)

    client.disconnect = AsyncMock(side_effect=_takes_a_moment)

    coordinator._hang_up(client)
    await hass.async_block_till_done()

    assert finished == [1]


async def test_stopping_does_not_cut_the_hang_up_short(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unload stops waiting at its ceiling; the disconnect itself carries on.

    Stopping disconnected under its own ceiling of three seconds, and on the
    real integration that ceiling has fired (see
    test_stopping_hangs_up_once_not_twice). Run against bleak 3.0.2 with a bus
    that never confirms: a disconnect cancelled at that ceiling has asked BlueZ
    to drop the link and returns with the client's D-Bus connection still open.
    That is the leak this fix is about, taken on every reload that meets a slow
    link - and a reload is what one reaches for when Bluetooth misbehaves.
    """
    coordinator, client = _connected_coordinator(hass)
    monkeypatch.setattr(coordinator_module, "_STOP_TIMEOUT", 0.05)
    released = asyncio.Event()
    finished: list[int] = []

    async def _slow_hang_up() -> None:
        await released.wait()
        finished.append(1)

    client.disconnect = AsyncMock(side_effect=_slow_hang_up)

    async with asyncio.timeout(0.4):  # the unload itself is still bounded
        await coordinator.async_stop()

    assert finished == []
    released.set()
    await hass.async_block_till_done()
    assert finished == [1]  # the disconnect ran to the end, after the unload


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
        coordinator, client = _connected_coordinator(hass)
        client.disconnect = AsyncMock(side_effect=failure)

        await coordinator.async_stop()

        client.disconnect.assert_awaited_once()
        assert coordinator._client is None


async def test_a_connect_that_fails_half_way_hangs_up_outside_its_deadline(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client of a failed subscription is not disconnected on borrowed time.

    It was disconnected inline, inside the deadline of whatever was connecting.
    A deadline that ran out during that disconnect cancelled it part-way - the
    bus left open, by the route the hang-up was written to close. The connect
    still waits for the hang-up, so that a retry does not dial over it; what
    the deadline ends now is that wait.
    """
    coordinator, _ = _connected_coordinator(hass)
    coordinator._client = None
    monkeypatch.setattr(coordinator_module, "_CONNECT_TIMEOUT", 0.05)
    client = _fresh_client()
    client.start_notify = AsyncMock(side_effect=BleakError("subscribe failed"))
    released = asyncio.Event()
    finished: list[int] = []

    async def _slow_hang_up() -> None:
        await released.wait()
        finished.append(1)

    client.disconnect = AsyncMock(side_effect=_slow_hang_up)
    _dialling(coordinator, monkeypatch, client)

    with pytest.raises(TimeoutError):  # the deadline, while it waits
        await coordinator._async_ensure_connected()

    assert finished == []
    released.set()
    await hass.async_block_till_done()
    assert finished == [1]
    assert coordinator._client is None


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
    coordinator, first = _connected_coordinator(hass)
    first.write_gatt_char = AsyncMock(side_effect=BleakError("Unlikely Error"))
    second = _fresh_client()  # its write fails as well: the command fails
    third = _fresh_client()
    third.write_gatt_char = AsyncMock()  # the next command's link works
    _dialling(coordinator, monkeypatch, second, third)
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.2)

    failing = asyncio.create_task(coordinator.async_set_power(True))
    await asyncio.sleep(0)  # it has failed twice and now waits for a report
    assert coordinator._client is None  # let go of at once, not after the wait
    second.disconnect.assert_not_awaited()

    await coordinator.async_set_brightness(40)  # queued behind it; connects
    assert coordinator._client is third

    with pytest.raises(HomeAssistantError):
        await failing
    await hass.async_block_till_done()

    second.disconnect.assert_awaited_once()  # the abandoned client is closed
    assert coordinator._client is third  # ...and the live one is still ours
    third.disconnect.assert_not_awaited()


async def test_a_connect_does_not_wait_for_the_hang_up_it_started(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link that answers nothing is hung up in the background.

    The connect path runs under _CONNECT_TIMEOUT and holds the lock. Waiting
    there for the hang-up would keep the lock for as long as BlueZ takes to
    confirm, and let the connect's deadline cancel the disconnect part-way -
    the leak again, by the route the fix closes for commands.
    """
    coordinator, _ = _connected_coordinator(hass)
    coordinator._client = None
    client = _fresh_client()
    released = asyncio.Event()
    finished: list[int] = []

    async def _slow_hang_up() -> None:
        await released.wait()
        finished.append(1)

    client.disconnect = AsyncMock(side_effect=_slow_hang_up)
    _dialling(coordinator, monkeypatch, client)

    async with asyncio.timeout(1):
        await coordinator._connect_locked()  # returns with the hang-up pending

    client.disconnect.assert_awaited_once()
    assert finished == []
    released.set()
    await hass.async_block_till_done()
    assert finished == [1]


async def test_the_poll_does_not_wait_for_the_hang_up_it_started(
    hass: HomeAssistant,
) -> None:
    """The same for priming, which the poll runs under the lock every tick."""
    coordinator, client = _connected_coordinator(hass)
    client.read_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))
    client.write_gatt_char = AsyncMock(side_effect=BleakError("Not connected"))
    released = asyncio.Event()
    finished: list[int] = []

    async def _slow_hang_up() -> None:
        await released.wait()
        finished.append(1)

    client.disconnect = AsyncMock(side_effect=_slow_hang_up)

    async with asyncio.timeout(1):
        await coordinator._async_prime()  # returns with the hang-up pending

    client.disconnect.assert_awaited_once()
    assert not coordinator._lock.locked()
    assert finished == []
    released.set()
    await hass.async_block_till_done()
    assert finished == [1]


async def test_a_retry_dials_only_after_a_half_made_connect_is_hung_up(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subscription that fails is hung up before the command dials again.

    The reason is the one a failed write has (see
    test_a_write_retry_hangs_up_before_it_dials_again): the lamp has one slot,
    and while BlueZ still shows the link as up a connect is handed that very
    link - the one being closed. Hanging this client up in the background lost
    the order: the retry dialled first, which on a link that reports itself
    connected while answering nothing means a second attempt on the link the
    first one had just failed on.
    """
    coordinator, _ = _connected_coordinator(hass)
    coordinator._client = None
    first = _fresh_client()
    first.start_notify = AsyncMock(side_effect=BleakError("Not connected"))
    second = _fresh_client()
    second.write_gatt_char = AsyncMock()
    clients = iter((first, second))
    order: list[str] = []

    async def _hang_up_slowly() -> None:
        await asyncio.sleep(0.01)  # a real disconnect is not instant either
        order.append("hung up")

    async def _dial(*_a: object, **_kw: object) -> MagicMock:
        order.append("dialled")
        return next(clients)

    first.disconnect = AsyncMock(side_effect=_hang_up_slowly)
    _dialling(coordinator, monkeypatch).side_effect = _dial

    await coordinator.async_set_power(True)

    assert order == ["dialled", "hung up", "dialled"]
    assert coordinator._client is second


async def test_a_connect_cancelled_half_way_is_hung_up_but_not_waited_for(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadline that cancels the subscription still gets the client hung up.

    And is not kept waiting for it. The connect holds the lock, and whoever set
    the deadline has already stopped waiting: staying on here for as long as
    BlueZ takes to close the link would hold the lock past the deadline for
    nobody's benefit.
    """
    coordinator, _ = _connected_coordinator(hass)
    coordinator._client = None
    monkeypatch.setattr(coordinator_module, "_CONNECT_TIMEOUT", 0.05)
    client = _fresh_client()
    released = asyncio.Event()
    finished: list[int] = []

    async def _never(*_a: object) -> None:
        await asyncio.Event().wait()

    async def _slow_hang_up() -> None:
        await released.wait()
        finished.append(1)

    client.start_notify = AsyncMock(side_effect=_never)
    client.disconnect = AsyncMock(side_effect=_slow_hang_up)
    _dialling(coordinator, monkeypatch, client)

    async with asyncio.timeout(0.5):
        with pytest.raises(TimeoutError):
            await coordinator._async_ensure_connected()

    client.disconnect.assert_awaited_once()
    assert not coordinator._lock.locked()
    assert finished == []
    released.set()
    await hass.async_block_till_done()
    assert finished == [1]


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
    coordinator, _ = _connected_coordinator(hass)
    coordinator._client = None

    def _out_of_range() -> None:
        return None

    coordinator._ble_device = _out_of_range

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


# What a call receives when its client's bus is closed underneath it. Run against
# dbus-fast with a bus that is shut while a call is waiting for its reply: the
# call ends in EOFError, and with the socket gone, in "Bad file descriptor".
# Neither is a BleakError, and bleak passes both on as they are.
_BUS_CLOSED = (EOFError(), OSError(9, "Bad file descriptor"))


@pytest.mark.parametrize("failure", _BUS_CLOSED, ids=["eof", "bad-fd"])
async def test_a_write_on_a_bus_closed_under_it_is_retried_like_a_lost_link(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """The bus closing under a write is the link going, and is handled as that.

    When the lamp drops the link, the disconnected callback hangs the client
    up at once, which closes its D-Bus connection. A write still waiting for
    its reply on that connection does not get the BleakError a lost link
    usually produces: it gets whatever the bus raised. Caught as nothing in
    particular, that went straight out of the command - no retry, and a bare
    EOFError where the user should read "cannot connect".
    """
    coordinator, first = _connected_coordinator(hass)
    first.write_gatt_char = AsyncMock(side_effect=failure)
    second = _fresh_client()
    second.write_gatt_char = AsyncMock()
    _dialling(coordinator, monkeypatch, second)

    await coordinator.async_set_power(True)  # the retry, on a fresh link, works

    first.disconnect.assert_awaited_once()
    assert coordinator._client is second


@pytest.mark.parametrize("failure", _BUS_CLOSED, ids=["eof", "bad-fd"])
async def test_a_command_that_keeps_meeting_a_closed_bus_fails_readably(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """...and when the retry meets the same, the user is told in their words."""
    coordinator, first = _connected_coordinator(hass)
    first.write_gatt_char = AsyncMock(side_effect=failure)
    second = _fresh_client()
    second.write_gatt_char = AsyncMock(side_effect=failure)
    _dialling(coordinator, monkeypatch, second)
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.01)

    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_power(True)


@pytest.mark.parametrize("failure", _BUS_CLOSED, ids=["eof", "bad-fd"])
async def test_a_connect_whose_reads_meet_a_closed_bus_just_drops_the_link(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """Every read and the state request, on a bus that closed: a dead link.

    The connect path makes three calls after the subscription - the
    device-info read, the state read and the batched request - and the link
    can go under any of them. None of them may turn that into an exception
    with a traceback: a link that answers nothing is dropped, and the poll
    builds another.
    """
    coordinator, _ = _connected_coordinator(hass)
    coordinator._client = None
    client = _fresh_client()
    client.read_gatt_char = AsyncMock(side_effect=failure)
    client.write_gatt_char = AsyncMock(side_effect=failure)
    _dialling(coordinator, monkeypatch, client)

    await coordinator._connect_locked()
    await hass.async_block_till_done()

    assert coordinator._client is None
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize("failure", _BUS_CLOSED, ids=["eof", "bad-fd"])
async def test_background_connects_that_meet_a_closed_bus_only_log_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """Neither background connect lets it out.

    These run as tasks nobody awaits, so what escapes one is not handled by
    anybody: it ends up in the log as an exception with a traceback, on every
    poll tick for as long as the bus stays the way it is.
    """
    coordinator, _ = _connected_coordinator(hass)
    coordinator._client = None
    _dialling(coordinator, monkeypatch).side_effect = failure

    await coordinator._async_initial_connect()
    await coordinator._async_reconnect()


@pytest.mark.parametrize("failure", _BUS_CLOSED, ids=["eof", "bad-fd"])
async def test_priming_that_meets_a_closed_bus_only_logs_it(
    hass: HomeAssistant, failure: Exception
) -> None:
    """The same for the priming the poll does on a link a command made.

    Here the link goes after the read has answered, under the bring-up write.
    """
    coordinator, client = _connected_coordinator(hass)
    client.read_gatt_char = AsyncMock(return_value=cbor.encode({KEY_ACTIVATED: False}))
    client.write_gatt_char = AsyncMock(side_effect=failure)

    await coordinator._async_prime()

    assert coordinator._primed_client is not client


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
    coordinator, first = _connected_coordinator(hass)
    first.write_gatt_char = AsyncMock(side_effect=BleakError("dropped"))
    second = _fresh_client()
    second.write_gatt_char = AsyncMock()
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.01)
    answered = asyncio.Event()

    async def _dial_slowly(*_a: object, **_kw: object) -> MagicMock:
        await answered.wait()
        return second

    _dialling(coordinator, monkeypatch).side_effect = _dial_slowly

    command = asyncio.create_task(coordinator.async_set_power(True))
    await asyncio.sleep(0)  # its write has failed; the retry is dialling
    stopping = asyncio.create_task(coordinator.async_stop())
    await asyncio.sleep(0)
    answered.set()  # ...and the lamp picks up, after the stop began

    with pytest.raises(HomeAssistantError):
        await command  # the coordinator was stopped under it
    await stopping
    await hass.async_block_till_done()

    assert coordinator._client is None
    second.disconnect.assert_awaited_once()


async def test_stopping_that_is_cancelled_still_lets_go_of_the_link(
    hass: HomeAssistant,
) -> None:
    """Cancelled while it waits for the lock, stopping still hangs up.

    It took the client first and waited afterwards, so a cancellation in that
    wait dropped the only reference to a connected client - the leak, and the
    lamp's slot held by nobody.
    """
    coordinator, client = _connected_coordinator(hass)

    await coordinator._lock.acquire()  # a command in flight
    try:
        stopping = asyncio.create_task(coordinator.async_stop())
        await asyncio.sleep(0)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
    finally:
        coordinator._lock.release()
    await hass.async_block_till_done()

    assert coordinator._client is None
    client.disconnect.assert_awaited_once()


async def test_a_stopped_coordinator_does_not_dial(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once stopped, the coordinator takes no new link at all."""
    coordinator, _ = _connected_coordinator(hass)
    dial = _dialling(coordinator, monkeypatch, _fresh_client(), _fresh_client())
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.01)

    await coordinator.async_stop()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_power(True)

    dial.assert_not_awaited()
    assert coordinator._client is None


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
    coordinator, client = _connected_coordinator(hass)
    monkeypatch.setattr(coordinator_module, "_STOP_TIMEOUT", 0.05)

    async def _never() -> None:
        await asyncio.Event().wait()

    client.disconnect = AsyncMock(side_effect=_never)

    await coordinator._lock.acquire()  # a command in flight: not waited for
    try:
        coordinator.async_shutdown()

        assert coordinator._client is None
        client.disconnect.assert_awaited_once()  # asked to drop it, already
        async with asyncio.timeout(1):  # nowhere near _HANG_UP_TIMEOUT
            await hass.async_block_till_done()
    finally:
        coordinator._lock.release()


async def test_shutting_down_stops_watching_and_takes_no_new_link(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A coordinator shut down with Home Assistant is as finished as one unloaded.

    Left watching, it would answer the next advertisement by dialling the lamp
    again - while Home Assistant is on its way out, and after the one hang-up
    it will get.
    """
    coordinator, _ = _connected_coordinator(hass)
    cancels = {name: MagicMock() for name in ("bluetooth", "unavailable", "poll")}
    coordinator._cancel_bluetooth = cancels["bluetooth"]
    coordinator._cancel_unavailable = cancels["unavailable"]
    coordinator._cancel_poll = cancels["poll"]
    dial = _dialling(coordinator, monkeypatch, _fresh_client(), _fresh_client())
    monkeypatch.setattr(coordinator_module, "_CONFIRM_TIMEOUT", 0.01)

    coordinator.async_shutdown()
    await hass.async_block_till_done()

    for name, cancel in cancels.items():
        assert cancel.call_count == 1, f"{name} watcher was left running"
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_power(True)
    dial.assert_not_awaited()
