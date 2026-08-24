"""Active BLE coordinator for a single Glowrium device."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, time, timedelta
import logging
from time import monotonic
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import cbor, protocol
from .const import (
    ACTIVATE_MISC_VALUE,
    DOMAIN,
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
_WRITE_ATTEMPTS = 2  # the initial write plus one reconnect-and-retry
# bleak-retry-connector defaults to 4 connect attempts, each of which can sit
# through a 20 s bleak timeout plus a backoff. Against an unreachable device
# that adds up to minutes while _lock is held, so a queued command cannot even
# start. Two attempts is enough: the reconnect poll comes round again in 30 s.
_CONNECT_ATTEMPTS = 2
# Hard ceiling on one user-facing command, so a button reports a clear failure
# in seconds instead of appearing to hang while the retries stack up.
_COMMAND_TIMEOUT = 15.0
# Ceiling on a background connect, including the wait for _lock. Without it a
# connect to an unreachable device holds the lock indefinitely: setup awaits
# this same path, so the entry hangs in "setup in progress" forever rather than
# coming up with its entities unavailable, and any queued command starves behind
# it. Entity availability follows advertisement presence, not the GATT link, so
# giving up here costs nothing - the reconnect poll comes round again in 30 s.
_CONNECT_TIMEOUT = 20.0
# How long a failed command waits for the device to report the state it asked
# for before the failure is believed. A write-with-response on a marginal link
# can reach the lamp and be acted on while the acknowledgement is lost, which
# bleak reports as failure - measured on a real G7, the confirming notification
# arrived 22-32 ms BEFORE the error was raised, so this is grace for a slower
# link rather than a wait anyone should routinely pay.
_CONFIRM_TIMEOUT = 2.0
# Ceiling on the disconnect during unload, so reloading the integration does
# not wait out whatever connect currently holds the lock.
_STOP_TIMEOUT = 3.0
# The batched state request is muted after this many consecutive failures. One
# failure means nothing on a weak link - a dropped connection surfaces as the
# same BleakError as an outright refusal - and giving up after one leaves every
# property outside the connect-time read unread.
_STATE_REQUEST_ATTEMPTS = 3
# ...and muted only for this long, not for the session. A model that genuinely
# refuses the request must not have its link torn down on every connect, but a
# lamp on a weak signal fails the same way and then recovers: measured on a real
# G7, three consecutive failures accumulated 70 s after start-up purely from a
# bad link, which permanently cost it four properties until Home Assistant was
# restarted. Muting expires so that heals itself.
_STATE_REQUEST_COOLDOWN = 600.0


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
        # The batched state request is muted until this time after a run of
        # failures, rather than for the session - see _request_state.
        self._state_request_muted_until = 0.0
        self._state_request_failures = 0
        # The client whose state has been primed. A command connects without
        # priming (see _connect_locked), so this is how the poll notices there
        # is a connection whose properties were never fetched.
        self._primed_client: BleakClientWithServiceCache | None = None
        # Set once a frame has been rejected for trailing bytes, so the warning
        # is raised once per session instead of on every notification.
        self._trailing_warned = False
        # Set whenever the device reports state, so a command awaiting
        # confirmation wakes on the report instead of polling for it.
        self._state_reported = asyncio.Event()

    @property
    def _state_request_muted(self) -> bool:
        """True while the batched state request is in its post-failure cooldown."""
        return monotonic() < self._state_request_muted_until

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

    # Decoded read accessors - the byte layouts they wrap live in protocol.py,
    # so entities read meaningful values instead of the raw state dict.

    @property
    def ramp_minutes(self) -> int | None:
        """Circadian ramp duration in minutes (0x2f), or None if not yet read."""
        return protocol.ramp_minutes(self.state)

    @property
    def schedule_start(self) -> time | None:
        """Schedule on-time from the 0x11 slot, or None if not yet read."""
        return protocol.schedule_start(self.state)

    @property
    def schedule_end(self) -> time | None:
        """Schedule off-time from the 0x11 slot, or None if not yet read."""
        return protocol.schedule_end(self.state)

    @property
    def schedule_brightness(self) -> int | None:
        """Schedule target brightness (%) from the 0x11 slot, or None."""
        return protocol.schedule_brightness(self.state)

    @property
    def schedule_gradual_minutes(self) -> int | None:
        """Schedule gradual-fade duration in minutes, or None if not yet read."""
        return protocol.schedule_gradual_minutes(self.state)

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

    async def async_start(self, entry: ConfigEntry) -> None:
        """Watch for the device and keep it connected.

        Returns as soon as the watchers are in place. The first connect runs as
        a background task rather than being awaited: setup awaits this method,
        and a lamp that is out of range would otherwise hold the entry open for
        the whole connect budget - long enough for a reload to land inside it,
        cancel the setup and leave the entry in ``setup_error``. Waiting buys
        nothing, because entity availability follows advertisement presence
        rather than the GATT link. Tying the task to ``entry`` means it is
        cancelled on unload, so a half-finished connect cannot outlive us.
        """
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
        entry.async_create_background_task(
            self.hass,
            self._async_initial_connect(),
            f"glowrium initial connect {self.address}",
        )
        # Advertisement callbacks are throttled, so also poll: reconnect within
        # _RECONNECT_INTERVAL after any drop, regardless of advertisement timing.
        self._cancel_poll = async_track_time_interval(
            self.hass, self._async_poll_reconnect, _RECONNECT_INTERVAL
        )

    async def _async_initial_connect(self) -> None:
        """Connect once at start-up, off the setup path."""
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
        # Bounded: a connect in flight holds _lock for up to _CONNECT_TIMEOUT,
        # and unload waiting behind it is what made reloading the integration
        # take the best part of ten seconds. The watchers above are already
        # cancelled, so dropping the client without a clean disconnect is safe -
        # the link dies with the reference.
        client, self._client = self._client, None
        if client is None:
            return
        try:
            async with asyncio.timeout(_STOP_TIMEOUT), self._lock:
                await client.disconnect()
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("Disconnect of %s failed: %s", self.address, err)

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
        if not self._is_connected:
            if not self._reconnecting:
                self._reconnecting = True
                self.hass.async_create_task(self._async_reconnect())
            return
        # Connected, but by a command, which skips priming to stay fast. Fetch
        # the properties now, off the command's critical path.
        if self._client is not self._primed_client:
            self.hass.async_create_task(self._async_prime())

    async def _async_prime(self) -> None:
        """Fetch device properties for a link that was established by a command."""
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT), self._lock:
                client = self._client
                if client is None or client is self._primed_client:
                    return
                await self._request_state(client)
                await self._async_activate_if_needed()
                self._primed_client = client
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("Priming state of %s failed: %s", self.address, err)
        else:
            self._async_notify_listeners()

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
        """Connect if not already connected, under a bounded wait for the lock.

        Every background connect - setup, the reconnect poll and the
        advertisement callback - funnels through here, so the ceiling applies to
        all of them. It has to cover the wait for ``_lock`` too: the starvation
        that hung setup was one holder grinding through connect attempts to an
        unreachable lamp while another waited on the lock with no deadline.
        """
        if self._is_connected:
            return
        async with asyncio.timeout(_CONNECT_TIMEOUT), self._lock:
            await self._connect_locked()

    async def _connect_locked(self, *, prime: bool = True) -> None:
        """Establish the GATT link, and unless told otherwise prime the state.

        The caller must hold ``_lock``; ``_async_ensure_connected`` and the
        write path both funnel through here so a command can never race the
        connect/reconnect the device performs underneath.

        A command passes ``prime=False``. It needs the link and its own write,
        nothing else - and priming is expensive: a device-info read, a state
        read, the batched request, and up to 3 s waiting for the activation
        flag, all before the write is even attempted and all inside the command
        budget. On a lamp where the connect alone is marginal, that is what
        turns a working command into a reported failure. The poll picks the
        priming up afterwards (see ``_async_poll_reconnect``).
        """
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
            max_attempts=_CONNECT_ATTEMPTS,
        )
        self._client = client
        await client.start_notify(NOTIFY_UUID, self._on_notify)
        # Read the device-info string BEFORE the state request: on a model that
        # rejects that request the device drops the link, and the info read
        # would be lost along with it - leaving us unable to name the model in
        # the very warning that asks which model it is.
        if not self.device_info:
            try:
                raw = await client.read_gatt_char(INFO_UUID)
                self.device_info = _parse_device_info(bytes(raw))
            except (BleakError, TimeoutError) as err:
                _LOGGER.debug("Device-info read from %s failed: %s", self.address, err)
        if not prime:
            return
        await self._request_state(client)
        await self._async_activate_if_needed()
        self._primed_client = client
        self._async_notify_listeners()

    def _require_read(self, value: Any, translation_key: str) -> Any:
        """Return ``value``, or raise if the device has not reported it yet.

        The schedule slot and the lighting-mode frame are read-modify-write: one
        write carries several fields at once. Substituting a default to change a
        single field silently overwrites the rest with values the user never
        chose, so refuse instead and say why.

        ``translation_key`` names the field in the user's own language. It is a
        key per field rather than one message with the field name substituted in,
        because a field name is a translatable noun, not a value.
        """
        if value is None or value == b"":
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=translation_key,
                translation_placeholders={"name": self.name},
            )
        return value

    async def _request_state(self, client: BleakClientWithServiceCache) -> None:
        """Prime the state mirror: read first, then ask for whatever is missing.

        ``NOTIFY_UUID`` is readable, and one read returns a property map in a
        single response - cheaper and sturdier than asking the device to report,
        since the request-and-notify path is subject to the map being split
        across notifications (see ``_ingest``), and the request write itself is
        unreliable on some models: a G8 (``Glowrium-C064``) answers it with ATT
        ``Insufficient authorization``, a not-connected error, or a timeout
        depending on route and timing, and drops the link while doing so.

        **But the read does not return everything.** Measured on real hardware,
        it carries only the low property block - up to 0x15 - so the indicator
        (0x17), lighting mode (0x2b), ramp (0x2f) and DST (0x35) never appear in
        it. Treating a successful read as the whole story left those four unread
        for the entire session. So the read primes what it can, and the request
        still goes out unless the read happened to cover every key we want.

        A model that genuinely refuses the request is stopped from being asked
        again, but only after ``_STATE_REQUEST_ATTEMPTS`` consecutive failures:
        a dropped link raises the same error as a refusal, and on a weak link
        the first attempt fails routinely.
        """
        try:
            raw = bytes(await client.read_gatt_char(NOTIFY_UUID))
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("%s state read failed: %s", self.address, err)
        else:
            self._ingest(raw)
        if all(key in self.state for key in STATE_KEYS):
            return  # the read covered everything; no need to ask as well
        if self._state_request_muted:
            return
        try:
            await client.write_gatt_char(NOTIFY_UUID, bytes(STATE_KEYS), response=True)
        except (BleakError, TimeoutError) as err:
            self._state_request_failures += 1
            if self._state_request_failures < _STATE_REQUEST_ATTEMPTS:
                _LOGGER.debug(
                    "%s state request failed (%d/%d): %s",
                    self.address,
                    self._state_request_failures,
                    _STATE_REQUEST_ATTEMPTS,
                    err,
                )
                return
            self._state_request_muted_until = monotonic() + _STATE_REQUEST_COOLDOWN
            self._state_request_failures = 0
            _LOGGER.warning(
                "%s (model %s, firmware %s) rejected the state request %d times "
                "in a row, most recently: %s. Pausing it for %d minutes - "
                "commands still work, but properties the connect-time read does "
                "not carry stay unknown until then. If this repeats on a lamp "
                "with a good signal, please report this model",
                self.address,
                self.model_id or "unknown",
                self.sw_version or "unknown",
                _STATE_REQUEST_ATTEMPTS,
                err,
                int(_STATE_REQUEST_COOLDOWN // 60),
            )
        else:
            self._state_request_failures = 0

    def _log_trailing_bytes(self, data: bytes, count: int) -> None:
        """Report a frame rejected for trailing bytes: once loudly, then quietly.

        Notifications arrive continuously, so an unconditional warning would
        flood the log; one per session is enough to surface the problem while
        the hex dump below gives whoever reports it everything needed to decode
        the frame by hand.
        """
        if self._trailing_warned:
            _LOGGER.debug(
                "%s: frame %s again carries %d trailing bytes",
                self.address,
                data.hex(),
                count,
            )
            return
        self._trailing_warned = True
        _LOGGER.warning(
            "%s (model %s, firmware %s) sent a frame with %d trailing bytes "
            "and it was dropped: %s. The frame declared less than it carried, "
            "so accepting the remainder could mean acting on a corrupt state. "
            "Please report this frame - it is exactly the hex dump needed",
            self.address,
            self.model_id or "unknown",
            self.sw_version or "unknown",
            count,
            data.hex(),
        )

    def _ingest(self, data: bytes) -> bool:
        """Merge a CBOR property map from the device into the state mirror.

        Returns True if any property was taken from ``data``. Shared by the
        notify callback and the connect-time read so both handle a split map,
        the remembered ramp and listener notification identically.
        """
        try:
            decoded, short = cbor.decode_frame(data)
        except cbor.TrailingBytesError as err:
            # Reported apart from a merely malformed frame, and loudly the first
            # time: rejecting these is what changed in #5, and on a model whose
            # frames were always fully consumed before, this is the regression
            # that change risks. Buried in "Undecodable frame" at debug level it
            # would never be noticed.
            self._log_trailing_bytes(data, err.count)
            return False
        except (ValueError, IndexError) as err:
            _LOGGER.debug("Undecodable frame %s: %s", data.hex(), err)
            return False
        if not isinstance(decoded, dict) or not decoded:
            return False
        if short:
            _LOGGER.debug(
                "%s: property map split across frames; kept %d of them",
                self.address,
                len(decoded),
            )
        self.state.update(decoded)
        self._state_reported.set()
        # Seed the remembered ramp from the device the first time we see it, so
        # it survives an HA restart (the device persists its own ramp). Guard on
        # truthiness, not "is not None": an empty ramp would otherwise latch and
        # block re-seeding forever, as protocol.ramp_minutes already assumes.
        if self._desired_ramp is None:
            ramp = self.state.get(KEY_RAMP)
            if ramp and isinstance(ramp, (bytes, bytearray)):
                self._desired_ramp = bytes(ramp)
        self._async_notify_listeners()
        return True

    @callback
    def _async_on_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        _LOGGER.debug("%s disconnected", self.address)
        self._client = None
        self._async_notify_listeners()

    @callback
    def _on_notify(self, _characteristic: Any, data: bytearray) -> None:
        self._ingest(bytes(data))

    async def _write_raw(self, payload: dict[int, Any]) -> None:
        """Write one command frame to the connected device.

        The caller must hold ``_lock`` and have ensured a connection: the
        connect path uses this for the bring-up sequence, and the command path
        (``_async_write``) wraps it with the lock and a retry.
        """
        if self._client is None:
            raise BleakError("write attempted while disconnected")
        await self._client.write_gatt_char(
            WRITE_UUID, cbor.encode(payload), response=True
        )
        # Optimistic local echo; the device also notifies its new state.
        self.state.update(payload)

    async def _async_write(self, payload: dict[int, Any]) -> None:
        """Serialize a command under the connection lock, with one reconnect.

        The write runs inside ``_lock`` so it cannot race the ~30-60 min GATT
        churn the device performs; if it still fails (the link dropped
        mid-command) the connection is rebuilt once and the write retried.

        The whole attempt - including the wait for ``_lock``, which a
        background reconnect may be holding - is capped by
        ``_COMMAND_TIMEOUT``, and every failure is reported as a
        ``HomeAssistantError`` so the user gets a readable message rather than
        a stack trace after a long hang.
        """
        try:
            async with asyncio.timeout(_COMMAND_TIMEOUT), self._lock:
                for attempt in range(1, _WRITE_ATTEMPTS + 1):
                    try:
                        await self._connect_locked(prime=False)
                        await self._write_raw(payload)
                        break
                    except (BleakError, TimeoutError) as err:
                        self._client = None
                        if attempt == _WRITE_ATTEMPTS:
                            raise
                        _LOGGER.debug(
                            "Write to %s failed (%s); reconnecting and retrying",
                            self.address,
                            err,
                        )
        except (BleakError, TimeoutError) as err:
            if await self._async_device_confirms(payload):
                _LOGGER.debug(
                    "Command to %s reported %s, but the device reports the state "
                    "it asked for - treating it as delivered",
                    self.address,
                    err,
                )
            else:
                _LOGGER.debug("Command to %s failed: %s", self.address, err)
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="cannot_connect",
                    translation_placeholders={"name": self.name},
                ) from err
        self._async_notify_listeners()

    async def _async_device_confirms(self, payload: dict[int, Any]) -> bool:
        """Return True if the device reports the state ``payload`` asked for.

        A write-with-response can reach the lamp, be acted on, and still fail:
        on a weak link the acknowledgement is what goes missing, so bleak raises
        while the lamp does exactly as it was told and notifies its new state.
        Reporting that as a failure told the user the command had not worked
        while they watched the light change.

        The notification path is an independent channel, so it settles the
        question the acknowledgement could not. Only keys in ``STATE_KEYS`` are
        compared - a mode command also carries fixed parameters (0x2c, 0x32) the
        device never reports back, and requiring those to match would mean no
        mode command could ever confirm.

        This says the device's reported state now matches the request, which is
        not quite the same as the write having landed: a command that asks for
        the state the lamp is already in confirms immediately. That is a benign
        confusion - the lamp ends up as the user asked either way - and it is
        the reason this only runs once a write has already failed.
        """
        tracked = {key: value for key, value in payload.items() if key in STATE_KEYS}
        if not tracked:
            return False
        try:
            async with asyncio.timeout(_CONFIRM_TIMEOUT):
                while True:
                    # Cleared before the check, never after: a report arriving
                    # between the two would otherwise be waited past.
                    self._state_reported.clear()
                    if all(self.state.get(k) == v for k, v in tracked.items()):
                        return True
                    await self._state_reported.wait()
        except TimeoutError:
            return False

    async def _async_activate_if_needed(self) -> None:
        """Bring the device up once if it reports as not yet activated (0x14).

        Runs inside the connection lock, right after the initial state request.
        """
        if self._activation_checked:
            return
        if self._state_request_muted:
            # This device is not reporting its properties, so 0x14 can never
            # arrive. Waiting for it on every connect - and _connect_locked
            # runs from the command path - is pure latency, and a device whose
            # activation flag cannot be read must never be activated blind.
            self._activation_checked = True
            return
        # Wait (briefly) for the initial state - including 0x14 - to arrive.
        for _ in range(12):
            if KEY_ACTIVATED in self.state or not self._is_connected:
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

        Note the asymmetry, which is deliberate and can look like a regression:
        an unread ramp falls back to a default, an unread mode refuses. On a lamp
        that has not reported 0x2b - and some models never do - switching to
        Circadian or setting the ramp therefore raises rather than quietly
        writing index 1. See the comment on ramp_value below for why.
        """
        mode_value = (
            mode
            if mode is not None
            else self._require_read(
                self.state.get(KEY_LIGHTING_MODE), "lighting_mode_not_read"
            )
        )
        # Ramp keeps its default. Setting a mode rewrites ramp on the device
        # regardless, so there is no "leave it alone" option here and falling
        # back is deliberate. The mode above is different: defaulting it there
        # silently *changes* a setting the caller never asked to touch.
        ramp_value = (
            ramp
            if ramp is not None
            else self._desired_ramp or self.state.get(KEY_RAMP) or RAMP_DEFAULT
        )
        return {
            KEY_LIGHTING_MODE: mode_value,
            0x2C: MODE_PARAM_2C,
            KEY_RAMP: ramp_value,
            0x32: MODE_PARAM_32,
        }

    async def async_set_lighting_mode(self, index: int) -> None:
        """Select a circadian lighting mode by its index."""
        await self._async_write(self._mode_payload(mode=index))

    async def async_set_ramp(self, minutes: int) -> None:
        """Set the circadian ramp time in minutes (0 = Sun Sync auto)."""
        self._desired_ramp = protocol.be2_minutes_to_bytes(minutes)
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

    async def async_set_timer_start(self, hour: int, minute: int) -> None:
        """Set the schedule start time."""
        slot = self._require_read(
            protocol.editable_timer_slot(self.state), "schedule_not_read"
        )
        slot[TIMER_START_H], slot[TIMER_START_M] = hour, minute
        await self._async_write({KEY_TIMER: bytes(slot)})

    async def async_set_timer_end(self, hour: int, minute: int) -> None:
        """Set the schedule end time."""
        slot = self._require_read(
            protocol.editable_timer_slot(self.state), "schedule_not_read"
        )
        slot[TIMER_END_H], slot[TIMER_END_M] = hour, minute
        await self._async_write({KEY_TIMER: bytes(slot)})

    async def async_set_timer_brightness(self, value: int) -> None:
        """Set the schedule brightness (0..100)."""
        slot = self._require_read(
            protocol.editable_timer_slot(self.state), "schedule_not_read"
        )
        slot[TIMER_BRIGHTNESS] = max(0, min(100, value))
        await self._async_write({KEY_TIMER: bytes(slot)})

    async def async_set_timer_gradual(self, minutes: int) -> None:
        """Set the schedule gradual on/off fade duration in minutes."""
        slot = self._require_read(
            protocol.editable_timer_slot(self.state), "schedule_not_read"
        )
        slot[TIMER_GRADUAL : TIMER_GRADUAL + 2] = protocol.be2_minutes_to_bytes(minutes)
        await self._async_write({KEY_TIMER: bytes(slot)})
