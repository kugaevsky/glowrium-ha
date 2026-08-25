"""Base entity for the Glowrium integration."""

from __future__ import annotations

from datetime import time

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import GlowriumCoordinator


class GlowriumEntity(Entity):
    """Common wiring for Glowrium entities: device info, availability, updates."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: GlowriumCoordinator) -> None:
        """Attach the entity to the coordinator's device."""
        self._coordinator = coordinator
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, coordinator.address)},
            name=coordinator.name,
            manufacturer="INLEDCO",
            model=coordinator.model.name,
            model_id=coordinator.model_id,
            sw_version=coordinator.sw_version,
            serial_number=coordinator.serial_number,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates."""
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return True while the device is present (advertising) or connected."""
        return self._coordinator.available


class GlowriumSettingEntity(GlowriumEntity, RestoreEntity):
    """A settings entity that falls back to its last known value.

    Entities here are views over the coordinator's state mirror, and the mirror
    starts empty: nothing is claimed that has not been read from the lamp. That
    is right for the light - reporting a confident "off" for a lamp that is
    actually on is what this integration used to do, and automations reasoned
    from it - but it leaves the device page unusable while a lamp is out of
    reach, every control an empty `unknown`.

    The device's *settings* are a different case. Lighting mode, ramp, DST, the
    indicator and the schedule change when someone changes them, not on their
    own and not while Home Assistant is down, so the last value read is a far
    better answer than none. The fallback is display-only: it is never merged
    into the coordinator's mirror, because the write paths use that mirror to
    decide whether a field is safe to modify, and a remembered value would let
    them rewrite a schedule slot from data that may be days old.
    """

    _restored: str | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to the coordinator and recover the last known value."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            self._restored = last.state

    def _restored_bool(self) -> bool | None:
        """Return the remembered value as a boolean, if there is one."""
        if self._restored is None:
            return None
        return self._restored == "on"

    def _restored_number(self) -> float | None:
        """Return the remembered value as a number, if it still parses as one."""
        if self._restored is None:
            return None
        try:
            return float(self._restored)
        except ValueError:
            return None

    def _restored_time(self) -> time | None:
        """Return the remembered value as a time, if it still parses as one."""
        if self._restored is None:
            return None
        try:
            return time.fromisoformat(self._restored)
        except ValueError:
            return None
