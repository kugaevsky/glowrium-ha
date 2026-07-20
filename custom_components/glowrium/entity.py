"""Base entity for the Glowrium integration."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

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
