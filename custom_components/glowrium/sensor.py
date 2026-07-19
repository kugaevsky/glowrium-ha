"""Sensor platform for the Glowrium integration (diagnostic coordinates)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GlowriumConfigEntry
from .const import KEY_LATITUDE, KEY_LONGITUDE
from .entity import GlowriumEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GlowriumConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Glowrium diagnostic sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            GlowriumCoordinateSensor(coordinator, KEY_LATITUDE, "latitude"),
            GlowriumCoordinateSensor(coordinator, KEY_LONGITUDE, "longitude"),
        ]
    )


class GlowriumCoordinateSensor(GlowriumEntity, SensorEntity):
    """The latitude/longitude the device uses to compute its circadian curve."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Any, key: int, translation_key: str) -> None:
        """Initialize a coordinate sensor."""
        super().__init__(coordinator)
        self._key = key
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{coordinator.address}_{translation_key}"

    @property
    def native_value(self) -> float | None:
        """Return the stored coordinate in degrees."""
        value = self._coordinator.state.get(self._key)
        return round(value, 5) if isinstance(value, float) else None
