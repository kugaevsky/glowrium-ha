"""Active BLE coordinator for a single Glowrium device."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import cbor
from .const import (
    ACTIVATE_MISC_VALUE,
    DST_OFF,
    DST_ON,
    INFO_UUID,
    KEY_ACTIVATE_MISC,
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
    MODE_CIRCADIAN,
    MODE_MANUAL,
    MODE_PARAM_2C,
    MODE_PARAM_32,
    MODE_SCHEDULE,
    NOTIFY_UUID,
    RAMP_DEFAULT,
    STATE_KEYS,
    TIMER_BRIGHTNESS,
    TIMER_DEFAULT,
    TIMER_END_H,
    TIMER_END_M,
    TIMER_GRADUAL,
    TIMER_START_H,
    TIMER_START_M,
    WRITE_UUID,
)
from .models import GlowriumModel, resolve_model

_LOGGER = logging.getLogger(__name__)
_RECONNECT_INTERVAL = timedelta(seconds=30)


def _encode_device_time(now: datetime | None = None) -> bytes:
    """Encode local time as the device clock: year_be(2), month, day, H, M, S."""
    now = now or dt_util.now()
    return bytes(
        [
            now.year >> 8,
            now.year & 0xFF,
            now.month,
            now.day,
            now.hour,
            now.minute,
            now.second,
        ]
    )


def _parse_device_info(raw: bytes) -> dict[str, str]:
    """Parse the facebd80 device-info string: 'key:value;key:value;...'."""
    info: dict[str, str] = {}
    for part in raw.decode("utf-8", "replace").split(";"):
        key, sep, value = part.partition(":")
        if sep and key.strip():
            info[key.strip()] = value.strip()
    return info


class GlowriumCoordinator:
    """Maintain a BLE connection and mirror the device's CBOR property state.

    Commands are CBOR maps written to ``WRITE_UUID`` (facebd01); the device
    reports its state as CBOR maps notified on ``NOTIFY_UUID`` (facebd02).
    """

    def __init__(self, hass: HomeAssistant, address: str, name: str) -> None:
        """Initialize the coordinator for the device at ``address``."""
        self.hass = hass
        self.address = address
        self.name = name
        self.state: dict[int, Any] = {}
        self.device_info: dict[str, str] = {}
        self._client: BleakClientWithServiceCache | None = None
        # The device resets its ramp to a default when circadian is re-enabled,
        # so remember the user's chosen ramp and re-apply it on mode switch.
        self._desired_ramp: bytes | None = None
        self._lock = asyncio.Lock()
        self._listeners: set[Callable[[], None]] = set()
        self._cancel_bluetooth: Callable[[], None] | None = None
        self._cancel_unavailable: Callable[[], None] | None = None
        self._cancel_poll: Callable[[], None] | None = None
        self._present = False
        self._reconnecting = False
        self._activation_checked = False

    @property
    def activated(self) -> bool | None:
        """Return the device's activation flag (False = needs pairing/bring-up)."""
        return self.state.get(KEY_ACTIVATED)

    @property
    def model(self) -> GlowriumModel:
        """Return the per-model profile resolved from the device-info pkey."""
        return resolve_model(self.device_info.get("pkey"))

    @property
    def model_id(self) -> str | None:
        """Device model code from the device-info string (e.g. Glowrium-C051)."""
        return self.device_info.get("pkey")

    @property
    def sw_version(self) -> str | None:
        """Firmware version from the device-info string."""
        return self.device_info.get("version")

    @property
    def serial_number(self) -> str | None:
        """Device id (serial) from the device-info string."""
        return self.device_info.get("devid")

    @property
    def _is_connected(self) -> bool:
        """Return True while a live GATT connection is held."""
        return self._client is not None and self._client.is_connected

    @property
    def available(self) -> bool:
        """Entity availability, based on the device being present (advertising).

        The GATT link is dropped and re-established periodically by the device;
        tying availability to it makes every entity flap to ``unavailable`` for
        a few seconds on each reconnect. The device advertises continuously, so
        we treat "present, or currently connected" as available and let the
        connection churn happen silently underneath.
        """
        return self._is_connected or self._present

    @property
    def operating_mode(self) -> str | None:
        """Return the active mode, or None if the state has not been read yet.

        None ("unknown") is distinct from Manual, which is only reported once
        both mode flags have actually been read as off - so a device we cannot
        reach yet does not look like it is in Manual.
        """
        circadian = self.state.get(KEY_CIRCADIAN)
        schedule = self.state.get(KEY_SCHEDULE)
        if circadian:
            return MODE_CIRCADIAN
        if schedule:
            return MODE_SCHEDULE
        if circadian is None or schedule is None:
            return None
        return MODE_MANUAL

    def mode_allows(self, mode: str) -> bool:
        """Return True if the device is in ``mode``, or its mode is unknown.

        Keeps mode-specific entities available while the state is unknown,
        instead of collapsing them to unavailable during a disconnect.
        """
        current = self.operating_mode
        return current is None or current == mode

    @callback
    def async_add_listener(
        self, update_callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Register an update listener; return a callable that removes it."""
        self._listeners.add(update_callback)

        @callback
        def _remove() -> None:
            self._listeners.discard(update_callback)

        return _remove

    @callback
    def _async_notify_listeners(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    async def async_start(self) -> None:
        """Watch for the device and keep it connected."""
        self._cancel_bluetooth = bluetooth.async_register_callback(
            self.hass,
            self._async_on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        # Track presence so entity availability follows the device, not the link.
        self._cancel_unavailable = bluetooth.async_track_unavailable(
            self.hass, self._async_on_unavailable, self.address, connectable=True
        )
        self._present = bluetooth.async_address_present(
            self.hass, self.address, connectable=True
        )
        # Advertisement callbacks are throttled, so also poll: reconnect within
        # _RECONNECT_INTERVAL after any drop, regardless of advertisement timing.
        self._cancel_poll = async_track_time_interval(
            self.hass, self._async_poll_reconnect, _RECONNECT_INTERVAL
        )
        try:
            await self._async_ensure_connected()
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("Initial connect to %s failed: %s", self.address, err)

    async def async_stop(self) -> None:
        """Cancel watching and disconnect."""
        if self._cancel_bluetooth is not None:
            self._cancel_bluetooth()
            self._cancel_bluetooth = None
        if self._cancel_unavailable is not None:
            self._cancel_unavailable()
            self._cancel_unavailable = None
        if self._cancel_poll is not None:
            self._cancel_poll()
            self._cancel_poll = None
        async with self._lock:
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except BleakError as err:
                    _LOGGER.debug("Disconnect of %s failed: %s", self.address, err)
                self._client = None

    @callback
    def _async_on_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        was_present = self._present
        self._present = True
        # Reconnect when the device reappears, but only one attempt at a time
        # (advertisements arrive ~every second; don't spawn a connect storm).
        if not self._is_connected and not self._reconnecting:
            self._reconnecting = True
            self.hass.async_create_task(self._async_reconnect())
        if not was_present:
            self._async_notify_listeners()

    @callback
    def _async_on_unavailable(
        self, _service_info: bluetooth.BluetoothServiceInfoBleak
    ) -> None:
        # The device stopped advertising (powered off / out of range).
        self._present = False
        self._async_notify_listeners()

    @callback
    def _async_poll_reconnect(self, _now: Any) -> None:
        if not self._is_connected and not self._reconnecting:
            self._reconnecting = True
            self.hass.async_create_task(self._async_reconnect())

    async def _async_reconnect(self) -> None:
        try:
            await self._async_ensure_connected()
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("Reconnect to %s failed: %s", self.address, err)
        finally:
            self._reconnecting = False

    def _ble_device(self) -> BLEDevice | None:
        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    async def _async_ensure_connected(self) -> None:
        if self._is_connected:
            return
        async with self._lock:
            if self._is_connected:
                return
            device = self._ble_device()
            if device is None:
                raise BleakError(f"{self.address} is not in range")
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                self.name,
                disconnected_callback=self._async_on_disconnect,
            )
            self._client = client
            await client.start_notify(NOTIFY_UUID, self._on_notify)
            # Ask the device to report the properties we track. The G7 drops the
            # link if asked for an id it does not expose, so treat a rejection as
            # non-fatal: another Glowrium model with a different property set can
            # still be controlled, just with limited state.
            try:
                await client.write_gatt_char(
                    NOTIFY_UUID, bytes(STATE_KEYS), response=True
                )
            except (BleakError, TimeoutError) as err:
                _LOGGER.warning(
                    "%s rejected the state request (%s) - a different Glowrium "
                    "model? Control may be limited",
                    self.address,
                    err,
                )
            if not self.device_info:
                try:
                    raw = await client.read_gatt_char(INFO_UUID)
                    self.device_info = _parse_device_info(bytes(raw))
                except (BleakError, TimeoutError) as err:
                    _LOGGER.debug(
                        "Device-info read from %s failed: %s", self.address, err
                    )
            await self._async_activate_if_needed()
            self._async_notify_listeners()

    @callback
    def _async_on_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        _LOGGER.debug("%s disconnected", self.address)
        self._client = None
        self._async_notify_listeners()

    @callback
    def _on_notify(self, _characteristic: Any, data: bytearray) -> None:
        try:
            decoded = cbor.decode(bytes(data))
        except (ValueError, IndexError) as err:
            _LOGGER.debug("Undecodable notification %s: %s", bytes(data).hex(), err)
            return
        if isinstance(decoded, dict):
            self.state.update(decoded)
            # Seed the remembered ramp from the device the first time we see it,
            # so it survives an HA restart (the device persists its own ramp).
            if self._desired_ramp is None:
                ramp = self.state.get(KEY_RAMP)
                if isinstance(ramp, (bytes, bytearray)):
                    self._desired_ramp = bytes(ramp)
            self._async_notify_listeners()

    async def _write_raw(self, payload: dict[int, Any]) -> None:
        """Write a command to an already-connected device (no lock, no notify)."""
        assert self._client is not None
        await self._client.write_gatt_char(
            WRITE_UUID, cbor.encode(payload), response=True
        )
        # Optimistic local echo; the device also notifies its new state.
        self.state.update(payload)

    async def _async_write(self, payload: dict[int, Any]) -> None:
        await self._async_ensure_connected()
        await self._write_raw(payload)
        self._async_notify_listeners()

    async def _async_activate_if_needed(self) -> None:
        """Bring the device up once if it reports as not yet activated (0x14).

        Runs inside the connection lock, right after the initial state request.
        """
        if self._activation_checked:
            return
        # Wait (briefly) for the initial state - including 0x14 - to arrive.
        for _ in range(12):
            if KEY_ACTIVATED in self.state:
                break
            await asyncio.sleep(0.25)
        if self.state.get(KEY_ACTIVATED) is False:
            await self.async_activate()
        if self.state.get(KEY_ACTIVATED):
            self._activation_checked = True

    async def async_activate(self) -> None:
        """Bring up a factory-reset device: clock + flags + enable light output.

        Replays the vendor app's first-pairing sequence - all local, no cloud and
        no BLE bond - so the light works without the app. The device gates its
        light output on 0x14; a virgin (factory-reset) device reports 0x14 False
        and its front-panel LEDs blink until this runs. Idempotent when already on.
        """
        await self._write_raw({KEY_ACTIVATE_MISC: ACTIVATE_MISC_VALUE})
        await self._write_raw({KEY_TIME: _encode_device_time(), KEY_TIME_SYNCED: 1})
        await self._write_raw({KEY_ACTIVATED: True})
        _LOGGER.info("Brought up (activated) %s", self.address)

    async def async_set_power(self, is_on: bool) -> None:
        """Turn the light on or off."""
        await self._async_write({KEY_POWER: is_on})

    async def async_set_brightness(self, value: int) -> None:
        """Set brightness as a 0..100 percentage."""
        await self._async_write({KEY_BRIGHTNESS: max(0, min(100, value))})

    async def async_set_light_state(
        self, is_on: bool, brightness: int | None = None
    ) -> None:
        """Set power and, optionally, brightness in a single CBOR command.

        The device accepts multi-key maps (as already used for the clock and
        location), so turning on at a brightness goes out atomically - one BLE
        write instead of two, with no on-at-old-then-change flicker.
        """
        payload: dict[int, Any] = {KEY_POWER: is_on}
        if brightness is not None:
            payload[KEY_BRIGHTNESS] = max(0, min(100, brightness))
        await self._async_write(payload)

    def _mode_payload(
        self, *, mode: int | None = None, ramp: bytes | None = None
    ) -> dict[int, Any]:
        """Build a lighting-mode command, preserving the other fields.

        The device expects the keys in the order mode, 0x2c, ramp, 0x32; the
        ramp (0x2f) is otherwise clobbered whenever the mode is set.
        """
        return {
            KEY_LIGHTING_MODE: self.state.get(KEY_LIGHTING_MODE, 1)
            if mode is None
            else mode,
            0x2C: MODE_PARAM_2C,
            KEY_RAMP: ramp
            if ramp is not None
            else self._desired_ramp or self.state.get(KEY_RAMP) or RAMP_DEFAULT,
            0x32: MODE_PARAM_32,
        }

    async def async_set_lighting_mode(self, index: int) -> None:
        """Select a circadian lighting mode by its index."""
        await self._async_write(self._mode_payload(mode=index))

    async def async_set_ramp(self, minutes: int) -> None:
        """Set the circadian ramp time in minutes (0 = Sun Sync auto)."""
        seconds = max(0, min(minutes, 0xFFFF // 60)) * 60
        self._desired_ramp = seconds.to_bytes(2, "big")
        await self._async_write(self._mode_payload(ramp=self._desired_ramp))

    async def async_set_operating_mode(self, mode: str) -> None:
        """Set the mutually-exclusive Manual/Circadian/Schedule mode."""
        if mode == MODE_CIRCADIAN:
            await self._async_write({KEY_SCHEDULE: False})
            await self._async_write({KEY_CIRCADIAN: True})
            # Enabling circadian resets the device's ramp to a default; re-apply
            # the user's ramp (with the current lighting mode) so it persists.
            if self._desired_ramp is not None:
                await self._async_write(self._mode_payload())
        elif mode == MODE_SCHEDULE:
            await self._async_write({KEY_CIRCADIAN: False})
            await self._async_write({KEY_SCHEDULE: True})
        else:  # manual
            await self._async_write({KEY_CIRCADIAN: False})
            await self._async_write({KEY_SCHEDULE: False})

    async def async_set_indicator(self, is_on: bool) -> None:
        """Turn the status indicator LED on or off."""
        await self._async_write({KEY_INDICATOR: is_on})

    async def async_set_dst(self, is_on: bool) -> None:
        """Enable or disable daylight-saving-time handling."""
        await self._async_write({KEY_DST: DST_ON if is_on else DST_OFF})

    async def async_sync_location(self) -> None:
        """Push HA's home coordinates; the device recomputes its circadian curve."""
        if self.hass is None:
            return
        lat = self.hass.config.latitude
        lon = self.hass.config.longitude
        if lat is None or lon is None:
            return
        await self._async_write({KEY_LATITUDE: float(lat), KEY_LONGITUDE: float(lon)})

    def _timer_slot(self) -> bytearray:
        """Return an editable copy of the schedule slot (0x11), or a default."""
        value = self.state.get(KEY_TIMER)
        if isinstance(value, (bytes, bytearray)) and len(value) >= len(TIMER_DEFAULT):
            return bytearray(value)
        return bytearray(TIMER_DEFAULT)

    async def async_set_timer_start(self, hour: int, minute: int) -> None:
        """Set the schedule start time."""
        slot = self._timer_slot()
        slot[TIMER_START_H], slot[TIMER_START_M] = hour, minute
        await self._async_write({KEY_TIMER: bytes(slot)})

    async def async_set_timer_end(self, hour: int, minute: int) -> None:
        """Set the schedule end time."""
        slot = self._timer_slot()
        slot[TIMER_END_H], slot[TIMER_END_M] = hour, minute
        await self._async_write({KEY_TIMER: bytes(slot)})

    async def async_set_timer_brightness(self, value: int) -> None:
        """Set the schedule brightness (0..100)."""
        slot = self._timer_slot()
        slot[TIMER_BRIGHTNESS] = max(0, min(100, value))
        await self._async_write({KEY_TIMER: bytes(slot)})

    async def async_set_timer_gradual(self, minutes: int) -> None:
        """Set the schedule gradual on/off fade duration in minutes."""
        slot = self._timer_slot()
        seconds = max(0, min(minutes, 0xFFFF // 60)) * 60
        slot[TIMER_GRADUAL : TIMER_GRADUAL + 2] = seconds.to_bytes(2, "big")
        await self._async_write({KEY_TIMER: bytes(slot)})
