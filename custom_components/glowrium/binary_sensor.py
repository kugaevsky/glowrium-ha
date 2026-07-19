"""Binary sensor platform for the Glowrium integration (activation status)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GlowriumConfigEntry
from .entity import GlowriumEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GlowriumConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Glowrium activation-status sensor."""
    async_add_entities([GlowriumActivatedSensor(entry.runtime_data)])


class GlowriumActivatedSensor(GlowriumEntity, BinarySensorEntity):
    """Whether the device has completed its pairing/bring-up (key 0x14).

    Off on a factory-reset device (front-panel LEDs blink, light output
    disabled); the coordinator brings it up automatically on connect.
    """

    _attr_translation_key = "activated"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Any) -> None:
        """Initialize the activation-status sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_activated"

    @property
    def is_on(self) -> bool | None:
        """Return True when the device is activated/paired."""
        return self._coordinator.activated
