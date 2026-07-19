"""Light platform for the Glowrium integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GlowriumConfigEntry
from .const import KEY_BRIGHTNESS, KEY_POWER
from .entity import GlowriumEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GlowriumConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Glowrium light."""
    async_add_entities([GlowriumLight(entry.runtime_data)])


class GlowriumLight(GlowriumEntity, LightEntity):
    """The main lamp: on/off (key 6) and brightness (key 8, 0-100%)."""

    _attr_name = None
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, coordinator: Any) -> None:
        """Initialize the light."""
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.address
        self._attr_icon = coordinator.model.icon

    @property
    def is_on(self) -> bool:
        """Return whether the lamp is on."""
        return bool(self._coordinator.state.get(KEY_POWER))

    @property
    def brightness(self) -> int | None:
        """Return brightness on HA's 0-255 scale (device uses 0-100)."""
        value = self._coordinator.state.get(KEY_BRIGHTNESS)
        if value is None:
            return None
        return round(value * 255 / 100)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the lamp on, optionally at a given brightness."""
        await self._coordinator.async_set_power(True)
        if ATTR_BRIGHTNESS in kwargs:
            await self._coordinator.async_set_brightness(
                round(kwargs[ATTR_BRIGHTNESS] * 100 / 255)
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the lamp off."""
        await self._coordinator.async_set_power(False)
