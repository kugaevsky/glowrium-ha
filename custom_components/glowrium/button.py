"""Button platform for the Glowrium integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GlowriumConfigEntry
from .entity import GlowriumEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GlowriumConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Glowrium buttons."""
    async_add_entities([GlowriumSyncLocationButton(entry.runtime_data)])


class GlowriumSyncLocationButton(GlowriumEntity, ButtonEntity):
    """Push Home Assistant's home coordinates to the device (keys 0x0a/0x0b).

    The device then recomputes its circadian sunrise/sunset curve itself.
    """

    _attr_translation_key = "sync_location"

    def __init__(self, coordinator: Any) -> None:
        """Initialize the sync-location button."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_sync_location"

    async def async_press(self) -> None:
        """Sync the home coordinates to the device."""
        await self._coordinator.async_sync_location()
